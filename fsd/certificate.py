"""Certificate management with SHA-256 password hashing and challenge-response auth.

Protocol upgrade v2: passwords stored as SHA-256 hex hashes.
Authentication uses HMAC-SHA256 challenge-response.
Backward compatible with legacy plaintext clients.
Supports loading from MySQL database (shared with main site).

安全说明(2026-11-07 审计 P0-1 / P1-12 / P2-25)：
  - 证书里的哈希**不能**直接当口令用（已删除 pass-the-hash 分支）；
    明文登录 = 服务端对明文再做 SHA-256 后比较，或改用 ``$SA`` 挑战-响应。
  - 三条加载路径（文件 / DB 首载 / DB 热载）统一走 :func:`_sanitize_cert_row`，
    避免再次出现「线上已修、仓库未回填」的漂移。
  - 文件证书只允许低等级（默认 ≤2），``0000`` 强制降为 OBSPILOT。
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fsd.protocol import LEV_OBSPILOT, LEV_SUSPENDED, LEV_MAX, CERT_LEVEL_NAMES, \
    is_valid_cid, MAX_CID_LENGTH

logger = logging.getLogger("fsd.certificate")

# 文件证书允许的最高等级(可用环境变量覆盖)。
# 安全(2026-11-07 审计 P0-1)：cert.txt 曾因权限 664 可被本机任意用户读取，
# 其中一条 level=12 的 000000 会直接变成管理员证书。文件证书一律不得出现
# 管制以上等级；需要高等级账号请走 DB(user.fsdLevel) 或管理台会话内添加。
FILE_CERT_MAX_LEVEL = int(os.environ.get("FSD_FILE_CERT_MAX_LEVEL", "2"))

# 临时迁移应急开关（默认关闭；仅故障迁移窗口用环境变量开启，事后必须关闭）
# - FSD_ALLOW_ANY_PASSWORD=1：已知 CID 忽略口令校验（任意口令可登录）
# - FSD_AUTO_ADD_UNKNOWN=1：未知 CID 自动按 OBSPILOT 注册（避免 "suspended"）
ALLOW_ANY_PASSWORD = os.environ.get("FSD_ALLOW_ANY_PASSWORD", "").strip().lower() in ("1", "true", "yes", "on")
AUTO_ADD_UNKNOWN_CIDS = os.environ.get("FSD_AUTO_ADD_UNKNOWN", "").strip().lower() in ("1", "true", "yes", "on")

# 停用标记：DB 里可用 -1/'suspended'/'banned' 等表示停用，
# 加载后按「已停用」处理(登录返回 (False, 0) → $ER ERR_CSSUSPEND)。
_SUSPENDED_MARKERS = {"-1", "suspended", "banned", "disabled", "blocked", "lock"}

# sha256("") —— 空口令等价值，一律视为不可用凭据
EMPTY_PASSWORD_SHA256 = hashlib.sha256(b"").hexdigest()

# MySQL level mapping: main site fsdLevel → FSD protocol level
_FSD_LEVEL_MAP = {
    0: 0,   # 普通用户(飞行员)，连飞时按 OBSPILOT 处理
    1: 1,   # OBS
    2: 2,   # S1
    3: 3,   # S2
    4: 4,   # S3
    5: 5,   # C1
    6: 6,   # C2
    7: 7,   # C3
    8: 8,   # I1
    9: 9,   # I2
    10: 10, # I3
    11: 11, # SUP
    12: 12, # ADM
}


def _looks_like_sha256(value: str) -> bool:
    """判断字符串是否是 64 位十六进制(SHA-256)。"""
    if not value or len(value) != 64:
        return False
    try:
        int(value, 16)
    except (TypeError, ValueError):
        return False
    return True


def _sanitize_cert_row(cid, level, password_hash: str = "",
                       bcrypt_hash: str = "", origin: str = "local",
                       source: str = "file", hashed: Optional[bool] = None
                       ) -> Optional[dict]:
    """统一清洗一条证书行（文件 / DB / 服务器同步三处共用）。

    这是 P1-12 的核心：线上曾单独修过 ``reload_from_db``（空哈希跳过 + ``0000``
    降级），但仓库里 ``load_file_extra`` / ``load`` 没有 → 按仓库部署会回滚线上
    修复。现在所有加载路径都过这一个函数。

    规则：
      - CID 非法（空/含 ``:``/超长）→ 丢弃
      - 等级无法解析（未知 + 非停用标记）→ 丢弃（不再默认降为 1）
      - 停用标记（-1 / suspended / banned ...）→ ``suspended=True``, level=0
      - 无任何可用凭据（空哈希且无 bcrypt）→ 丢弃（空口令可登录）
      - 哈希等于 ``sha256("")`` → 丢弃
      - ``source="db"`` 且声明 ``hashed=True`` 但值不是 64 位 hex → 丢弃
      - 非 DB 来源（文件 / 服务器同步）等级 > :data:`FILE_CERT_MAX_LEVEL` → 丢弃
      - ``cid == "0000"``（系统 bot）→ 强制 level=OBSPILOT（停用标记除外）
      - 等级越界 → 夹到 ``[0, LEV_MAX]``

    :returns: 清洗后的 ``dict``（cid/password_hash/bcrypt_hash/level/origin/
        suspended/hashed），不可用时返回 ``None``。
    """
    cid_s = str(cid if cid is not None else "").strip()
    if not cid_s or not is_valid_cid(cid_s) or len(cid_s) > MAX_CID_LENGTH:
        return None

    # ---- 等级解析 ----
    lvl_raw = level
    suspended = False
    if isinstance(lvl_raw, bool):
        return None
    marker = str(lvl_raw if lvl_raw is not None else "").strip().lower()
    try:
        lvl = int(marker)
    except (TypeError, ValueError):
        if marker in _SUSPENDED_MARKERS:
            lvl = LEV_SUSPENDED
            suspended = True
        else:
            # 未知等级：拒绝，而不是像以前那样默认降为 1（审计 P2-25）
            return None
    if marker in _SUSPENDED_MARKERS:
        lvl = LEV_SUSPENDED
        suspended = True

    # ---- 凭据 ----
    pw = (password_hash or "").strip() if isinstance(password_hash, str) \
        else str(password_hash or "").strip()
    bc = (bcrypt_hash or "").strip() if isinstance(bcrypt_hash, str) \
        else str(bcrypt_hash or "").strip()
    if bc and not bc.startswith("$"):
        bc = ""       # 只接受真正的 bcrypt 串($2a/$2b/$2y...)
    if not pw and not bc:
        return None   # 空哈希/空口令：一律跳过
    if pw.lower() == EMPTY_PASSWORD_SHA256:
        return None   # sha256("") 等价于空口令
    if hashed and pw and not _looks_like_sha256(pw):
        return None   # 声明为哈希来源却不是 64 位 hex → 无效
    if not pw and not bc:
        return None

    # ---- 等级策略 ----
    if cid_s == "0000" and not suspended:
        lvl = LEV_OBSPILOT
    if source != "db" and not suspended and lvl > FILE_CERT_MAX_LEVEL:
        logger.warning(
            "[Certificate] skip CID %s: file/link cert level %s > %s "
            "(文件与同步证书不得携带管制以上等级)",
            cid_s, lvl, FILE_CERT_MAX_LEVEL)
        return None
    lvl = max(LEV_SUSPENDED, min(int(lvl), LEV_MAX))

    return {
        "cid": cid_s,
        "password_hash": pw,
        "bcrypt_hash": bc,
        "level": lvl,
        "origin": origin or "local",
        "suspended": suspended,
        "hashed": hashed,
    }


def _build_cert(row: dict, creation: int = 0) -> "Certificate":
    """由 :func:`_sanitize_cert_row` 的返回值构造 :class:`Certificate`。"""
    cert = Certificate(row["cid"], row["password_hash"], row["level"],
                       creation, row["origin"],
                       bcrypt_hash=row.get("bcrypt_hash", ""),
                       hashed=row.get("hashed"))
    cert.suspended = bool(row.get("suspended", False))
    return cert


def hash_password(password: str) -> str:
    """SHA-256 hash a plaintext password, return hex string."""
    return hashlib.sha256(password.encode()).hexdigest()


def generate_challenge(length: int = 32) -> str:
    """Generate a random hex challenge token."""
    return os.urandom(length).hex()


def compute_auth_response(challenge: str, password: str) -> str:
    """Compute HMAC-SHA256(challenge, SHA256(password)) as hex.

    This is what the upgraded client sends instead of plaintext password.
    """
    pwd_hash = hashlib.sha256(password.encode()).digest()
    return hmac.new(
        challenge.encode(), pwd_hash, hashlib.sha256
    ).hexdigest()


def verify_challenge_response(challenge: str, response: str, stored_hash: str) -> bool:
    """Verify a challenge-response against stored SHA256 hash."""
    stored_bytes = bytes.fromhex(stored_hash)
    expected = hmac.new(challenge.encode(), stored_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, response)


class Certificate:
    """A single certificate entry (CID, password hash, level)."""

    def __init__(self, cid: str, password: str, level: int,
                 creation: int = 0, origin: str = "",
                 bcrypt_hash: str = "", hashed: Optional[bool] = None):
        self.cid = cid
        self.bcrypt_hash = bcrypt_hash  # fallback bcrypt hash from main site DB
        # 已停用标记：由 _sanitize_cert_row 设置，登录时直接拒绝
        self.suspended = False
        # hashed=None 表示自动判断（64 位 hex 视为已哈希），
        # DB/同步来源应显式传 hashed=True。
        if hashed is None:
            hashed = _looks_like_sha256(password) if password else False
        if not password:
            # 安全(2026-11-07 审计 P0-1)：以前这里会把空口令算成 sha256("")，
            # 于是「空哈希」证书可以被空口令登录。现在空凭据 = 不可登录，
            # 只有 bcrypt 回退仍可生效。
            self.password_hash = ""
        elif hashed:
            self.password_hash = password.lower()
        else:
            self.password_hash = hash_password(password)
        self.level = max(LEV_SUSPENDED, min(int(level), LEV_MAX))
        self.origin = origin or "local"
        self.creation = creation or int(time.time())
        self.prev_visit = 0
        self.live_check = 1

    def verify_password(self, password: str) -> bool:
        """Check if the given password matches.

        安全(2026-11-07 审计 P0-1 修复 pass-the-hash)：
        只接受**明文**口令（服务端 SHA-256 后比较）或 bcrypt 回退；
        已删除「64 位十六进制哈希可直接当口令」的分支 —— 拿到证书文件/DB Dump
        里的哈希不再能直接登录，必须破解出明文或改用 ``$SA`` 挑战-响应。
        """
        if not password:
            return False
        # 明文口令 → SHA-256 后与存储哈希比较
        if self.password_hash and hmac.compare_digest(
                self.password_hash, hash_password(password)):
            return True
        # bcrypt 回退（主站同步过来、没有 fsdPasswordHash 的用户）
        if self.bcrypt_hash and self.bcrypt_hash.startswith("$"):
            try:
                import bcrypt
                if bcrypt.checkpw(password.encode("utf-8"), self.bcrypt_hash.encode("utf-8")):
                    return True
            except Exception:
                pass
        return False

    def verify_challenge(self, challenge: str, response: str) -> bool:
        """Verify HMAC-SHA256 challenge response."""
        if self.password_hash:
            return verify_challenge_response(challenge, response, self.password_hash)
        return False

    def to_sync_line(self) -> str:
        """Format for server-to-server sync (sends hash, not plaintext)."""
        return f"{self.cid} {self.password_hash} {self.level} {self.creation} {self.origin}"

    def __repr__(self):
        return f"<Certificate {self.cid} level={self.level}>"


class CertificateStore:
    """Manages the certificate database."""

    def __init__(self):
        self._certs: dict[str, Certificate] = {}

    def load(self, filename: str) -> int:
        """Load certificates from file. Returns count loaded.

        文件格式: ``cid password|sha256 level createdAt origin``。
        安全(2026-11-07 审计 P0-1)：文件来源一律走 :func:`_sanitize_cert_row`，
        level > :data:`FILE_CERT_MAX_LEVEL` 的行被丢弃（cert.txt 不会产生 ADMIN）。
        """
        self._certs.clear()
        try:
            with open(filename, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(";") or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    cid = parts[0]
                    password = parts[1]
                    level = int(parts[2])
                    creation = int(parts[3]) if len(parts) > 3 else int(time.time())
                    origin = parts[4] if len(parts) > 4 else "local"
                    row = _sanitize_cert_row(cid, level, password,
                                             origin=origin, source="file")
                    if row is None:
                        logger.warning("[Certificate] skip file cert %r (invalid row)",
                                       cid)
                        continue
                    self._certs[row["cid"]] = _build_cert(row, creation)
            return len(self._certs)
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.error(f"[Certificate] Error loading: {e}")
            return 0

    def _query_db_rows(self, db_config: dict) -> list:
        """从 MySQL 读取证书原始行(不含清洗)。调用方负责线程池执行。"""
        import pymysql
        conn = pymysql.connect(
            host=db_config.get("host", "127.0.0.1"),
            port=int(db_config.get("port", 3306)),
            user=db_config.get("user", "root"),
            password=db_config.get("password", ""),
            database=db_config.get("name", "cwfly"),
            charset="utf8mb4",
            # 修复(2026-10-16)：此处原先没有任何超时，而 reload_from_db 的调用点
            # 曾在事件循环上同步执行 → MySQL 卡住会冻住整个 FSD 服务。
            # 现在调用点已改为线程池执行，并在此补上连接/读写超时。
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                # Fetch all users: prefer fsdPasswordHash, fall back to bcrypt password
                cur.execute(
                    "SELECT cid, fsdPasswordHash, `password`, fsdLevel, createdAt "
                    "FROM `User`"
                )
                return list(cur.fetchall())
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _apply_db_rows(self, rows: list) -> set:
        """把 DB 原始行清洗后写入 store，返回本次的 CID 集合。

        ``load_from_db`` / ``reload_from_db`` 共用（P1-12：避免两条路径行为漂移）。
        """
        db_cids = set()
        now = int(time.time())
        for row in rows:
            cid = str(row[0])
            sha256_hash = row[1] or ""
            bcrypt_hash = row[2] or ""
            fsd_level = row[3]
            try:
                created = int(row[4].timestamp()) if row[4] else now
            except Exception:
                created = now
            # 未知等级 → 拒绝(不再默认 1)；-1 等标记 → 停用
            level_raw = fsd_level
            if isinstance(fsd_level, int) and fsd_level in _FSD_LEVEL_MAP:
                level_raw = _FSD_LEVEL_MAP[fsd_level]
            row_clean = _sanitize_cert_row(
                cid, level_raw, sha256_hash, bcrypt_hash,
                origin="mysql", source="db", hashed=True)
            if row_clean is None:
                # 安全(2026-10-25 审计 / 2026-11-07 P1-12)：空哈希账号一律拒绝
                # (否则空口令即可登录)；未知等级/非法 CID 同样跳过。
                logger.warning(
                    "[Certificate] skip CID %s: unusable credential/level (%r)",
                    cid, fsd_level)
                continue
            self._certs[row_clean["cid"]] = _build_cert(row_clean, created)
            db_cids.add(row_clean["cid"])
        return db_cids

    def load_from_db(self, db_config: dict) -> int:
        """Load certificates from MySQL database (shared with main site).

        Uses fsdPasswordHash (SHA256) if available, falls back to
        the bcrypt password field for legacy user compatibility.

        db_config: { host, port, name, user, password }
        Returns count loaded.
        """
        self._certs.clear()
        try:
            rows = self._query_db_rows(db_config)
        except ImportError:
            logger.warning("[Certificate] pymysql not installed, cannot load from DB")
            return 0
        except Exception as e:
            logger.error(f"[Certificate] DB load error: {e}")
            return 0
        self._apply_db_rows(rows)
        return len(self._certs)

    def reload_from_db(self, db_config: dict) -> int:
        """Reload certificates from MySQL, preserving file-based certs.

        与 load_from_db 不同: 不清空现有证书, 只更新/新增 DB 中的账号,
        并删除 DB 中已不存在的 mysql 来源证书(文件来源的保留)。
        Returns count of certs after reload (-1 on error).
        """
        try:
            rows = self._query_db_rows(db_config)
        except ImportError:
            logger.warning("[Certificate] pymysql not installed, cannot reload from DB")
            return -1
        except Exception as e:
            logger.error(f"[Certificate] DB reload error: {e}")
            return -1

        db_cids = self._apply_db_rows(rows)

        # 删除 DB 中已不存在的 mysql 来源证书(用户被删除)
        for cid in [c for c, cert in self._certs.items()
                    if cert.origin == "mysql" and c not in db_cids]:
            del self._certs[cid]

        return len(self._certs)

    def load_file_extra(self, filename: str) -> int:
        """Load additional certificates from file (does not clear existing).
        Useful for supplementing MySQL-loaded certs with file-based ones.

        安全(2026-11-07 审计 P0-1/P1-12)：
          - 与 DB 加载共用 :func:`_sanitize_cert_row`（空哈希跳过、``0000`` 降级、
            等级上限校验）；
          - level > :data:`FILE_CERT_MAX_LEVEL` 的行直接丢弃 → cert.txt 里的
            level=12 账号（历史遗留的 ``000000``）不会再变成 ADMIN。
        """
        try:
            count = 0
            with open(filename, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(";") or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    cid = parts[0]
                    if cid in self._certs:
                        continue  # Don't overwrite DB certs
                    password = parts[1]
                    level = int(parts[2])
                    creation = int(parts[3]) if len(parts) > 3 else int(time.time())
                    origin = parts[4] if len(parts) > 4 else "file"
                    row = _sanitize_cert_row(cid, level, password,
                                             origin=origin, source="file")
                    if row is None:
                        logger.warning(
                            "[Certificate] skip extra file cert %r "
                            "(level>%s or unusable credential)",
                            cid, FILE_CERT_MAX_LEVEL)
                        continue
                    self._certs[row["cid"]] = _build_cert(row, creation)
                    count += 1
            return count
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.error(f"[Certificate] Error loading extra: {e}")
            return 0

    def save(self, filename: str, max_level: Optional[int] = None,
             include_db: bool = True) -> bool:
        """Save certificates to file (stores hashed passwords).

        安全(2026-11-07 审计 P2-24)：``cert add/del`` 与管理台默认**不再**把全量
        证书（含 DB 用户哈希、level≥3 的高等级证书）倒进 cert.txt ——
        调用方传 ``max_level``/``include_db=False`` 即可只写「文件来源的低等级
        证书」。默认参数保留旧行为，供显式导出/运维脚本使用。
        """
        try:
            written = 0
            with open(filename, "w") as f:
                for cid in sorted(self._certs.keys()):
                    cert = self._certs[cid]
                    if not include_db and cert.origin == "mysql":
                        continue
                    if max_level is not None and cert.level > max_level:
                        continue
                    f.write(f"{cid} {cert.password_hash} {cert.level} "
                           f"{cert.creation} {cert.origin}\n")
                    written += 1
            logger.info("[Certificate] saved %d certs to %s", written, filename)
            return True
        except Exception as e:
            logger.error(f"[Certificate] Error saving: {e}")
            return False

    def save_public(self, filename: str) -> bool:
        """只写「非 DB 来源 + 等级 ≤ FILE_CERT_MAX_LEVEL」的证书。

        管理台 ``cert add/del``、启动时创建默认管理员之后都应使用本方法，
        避免把 DB 全量哈希与管理员等级写进 cert.txt。
        """
        return self.save(filename, max_level=FILE_CERT_MAX_LEVEL,
                         include_db=False)

    def get(self, cid: str) -> Optional[Certificate]:
        return self._certs.get(cid)

    def add(self, cert: Certificate):
        self._certs[cert.cid] = cert

    def remove(self, cid: str):
        self._certs.pop(cid, None)

    def has(self, cid: str) -> bool:
        return cid in self._certs

    def max_level(self, cid: str, password: str) -> tuple[bool, int]:
        """Authenticate a client with plaintext password.

        Returns ``(success, max_level)``。返回值语义（审计 P2-25）：
          - 认证成功 → ``(True, cert.level)``
          - 账号不存在 / 已停用 → ``(False, LEV_SUSPENDED=0)``
            → 客户端收到 ``$ER ERR_CSSUSPEND``（此前该分支不可达）
          - 口令错误 → ``(False, LEV_OBSPILOT=1)``
            → 客户端收到 ``$ER ERR_CIDINVALID``
        """
        cert = self.get(cid)
        if not cert:
            if AUTO_ADD_UNKNOWN_CIDS:
                cert = Certificate(cid, "", LEV_OBSPILOT, origin="emergency")
                self.add(cert)
                logger.warning("[Emergency] unknown CID %s auto-registered as OBSPILOT", cid)
                cert.prev_visit = int(time.time())
                return True, cert.level
            return False, LEV_SUSPENDED
        if getattr(cert, "suspended", False):
            return False, LEV_SUSPENDED
        if ALLOW_ANY_PASSWORD:
            logger.warning("[Emergency] password check bypassed for CID %s", cid)
            cert.prev_visit = int(time.time())
            return True, cert.level
        if cert.verify_password(password):
            cert.prev_visit = int(time.time())
            return True, cert.level
        return False, LEV_OBSPILOT

    def max_level_challenge(self, cid: str, response: str,
                            challenge: str) -> tuple[bool, int]:
        """Authenticate with challenge-response.

        返回值语义同 :meth:`max_level`。
        """
        cert = self.get(cid)
        if not cert:
            if AUTO_ADD_UNKNOWN_CIDS:
                cert = Certificate(cid, "", LEV_OBSPILOT, origin="emergency")
                self.add(cert)
                logger.warning("[Emergency] unknown CID %s auto-registered as OBSPILOT", cid)
                cert.prev_visit = int(time.time())
                return True, cert.level
            return False, LEV_SUSPENDED
        if getattr(cert, "suspended", False):
            return False, LEV_SUSPENDED
        if ALLOW_ANY_PASSWORD:
            logger.warning("[Emergency] challenge-response bypassed for CID %s", cid)
            cert.prev_visit = int(time.time())
            return True, cert.level
        if cert.verify_challenge(challenge, response):
            cert.prev_visit = int(time.time())
            return True, cert.level
        return False, LEV_OBSPILOT

    @property
    def all_certs(self) -> list[Certificate]:
        return list(self._certs.values())

    def __len__(self) -> int:
        return len(self._certs)
