"""Certificate management with SHA-256 password hashing and challenge-response auth.

Protocol upgrade v2: passwords stored as SHA-256 hex hashes.
Authentication uses HMAC-SHA256 challenge-response.
Backward compatible with legacy plaintext clients.
Supports loading from MySQL database (shared with main site).
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from protocol import LEV_OBSPILOT, LEV_MAX, CERT_LEVEL_NAMES

logger = logging.getLogger("fsd.certificate")

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
                 bcrypt_hash: str = ""):
        self.cid = cid
        self.bcrypt_hash = bcrypt_hash  # fallback bcrypt hash from main site DB
        # Hash password unless already a 64-char hex hash
        if len(password) == 64 and all(c in "0123456789abcdef" for c in password.lower()):
            self.password_hash = password.lower()
        else:
            self.password_hash = hash_password(password)
        self.level = min(level, LEV_MAX)
        self.origin = origin or "local"
        self.creation = creation or int(time.time())
        self.prev_visit = 0
        self.live_check = 1

    def verify_password(self, password: str) -> bool:
        """Check if the given password (plaintext or hash) matches.

        Tries: SHA256 direct → SHA256 hashed → bcrypt fallback
        """
        # Direct hash comparison
        if self.password_hash and hmac.compare_digest(self.password_hash, password.lower()):
            return True
        # SHA256 hash plaintext and compare
        if self.password_hash and len(password) != 64:
            if hmac.compare_digest(self.password_hash, hash_password(password)):
                return True
        # bcrypt fallback (for users synced from main site DB without fsdPasswordHash)
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
        """Load certificates from file. Returns count loaded."""
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
                    cert = Certificate(cid, password, level, creation, origin)
                    self._certs[cid] = cert
            return len(self._certs)
        except FileNotFoundError:
            return 0
        except Exception as e:
            print(f"[Certificate] Error loading: {e}")
            return 0

    def load_from_db(self, db_config: dict) -> int:
        """Load certificates from MySQL database (shared with main site).

        Uses fsdPasswordHash (SHA256) if available, falls back to
        the bcrypt password field for legacy user compatibility.

        db_config: { host, port, name, user, password }
        Returns count loaded.
        """
        self._certs.clear()
        try:
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
            with conn.cursor() as cur:
                # Fetch all users: prefer fsdPasswordHash, fall back to bcrypt password
                cur.execute(
                    "SELECT cid, fsdPasswordHash, `password`, fsdLevel, createdAt "
                    "FROM `User`"
                )
                empty_sha = hash_password("")  # e3b0c4... = sha256("")
                for row in cur.fetchall():
                    cid = str(row[0])
                    sha256_hash = row[1] or ""
                    bcrypt_hash = row[2] or ""
                    fsd_level = row[3]
                    created = int(row[4].timestamp()) if row[4] else int(time.time())
                    level = _FSD_LEVEL_MAP.get(fsd_level, 1)
                    # 安全(2026-10-25 审计): 空哈希账号一律拒绝(空密码即可登录)
                    if sha256_hash == empty_sha:
                        print(f"[Certificate] skip CID {cid}: empty fsdPasswordHash")
                        continue
                    # 安全(2026-10-25 审计 P0-4): 系统账号 0000 是 bot 专用,
                    # 从 DB 侧加载时强制降为 OBSPILOT, 防止 level-12 管理权经公网 6809 登录
                    if cid == "0000":
                        level = 1
                    # Use SHA256 if available, otherwise mark as bcrypt-only
                    pwd = sha256_hash if sha256_hash else ""
                    cert = Certificate(cid, pwd, level, created, "mysql",
                                       bcrypt_hash=bcrypt_hash)
                    self._certs[cid] = cert
            conn.close()
            return len(self._certs)
        except ImportError:
            print("[Certificate] pymysql not installed, cannot load from DB")
            return 0
        except Exception as e:
            print(f"[Certificate] DB load error: {e}")
            return 0

    def reload_from_db(self, db_config: dict) -> int:
        """Reload certificates from MySQL, preserving file-based certs.

        与 load_from_db 不同: 不清空现有证书, 只更新/新增 DB 中的账号,
        并删除 DB 中已不存在的 mysql 来源证书(文件来源的保留)。
        Returns count of certs after reload.
        """
        try:
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
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cid, fsdPasswordHash, `password`, fsdLevel, createdAt "
                    "FROM `User`"
                )
                rows = cur.fetchall()
            conn.close()

            db_cids = set()
            for row in rows:
                cid = str(row[0])
                sha256_hash = row[1] or ""
                bcrypt_hash = row[2] or ""
                fsd_level = row[3]
                created = int(row[4].timestamp()) if row[4] else int(time.time())
                level = _FSD_LEVEL_MAP.get(fsd_level, 1)
                pwd = sha256_hash if sha256_hash else ""
                cert = Certificate(cid, pwd, level, created, "mysql",
                                   bcrypt_hash=bcrypt_hash)
                self._certs[cid] = cert
                db_cids.add(cid)

            # 删除 DB 中已不存在的 mysql 来源证书(用户被删除)
            for cid in [c for c, cert in self._certs.items()
                        if cert.origin == "mysql" and c not in db_cids]:
                del self._certs[cid]

            return len(self._certs)
        except ImportError:
            logger.warning("[Certificate] pymysql not installed, cannot reload from DB")
            return -1
        except Exception as e:
            logger.error(f"[Certificate] DB reload error: {e}")
            return -1

    def load_file_extra(self, filename: str) -> int:
        """Load additional certificates from file (does not clear existing).
        Useful for supplementing MySQL-loaded certs with file-based ones.
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
                    cert = Certificate(cid, password, level, creation, origin)
                    self._certs[cid] = cert
                    count += 1
            return count
        except FileNotFoundError:
            return 0
        except Exception as e:
            print(f"[Certificate] Error loading extra: {e}")
            return 0

    def save(self, filename: str) -> bool:
        """Save certificates to file (stores hashed passwords)."""
        try:
            with open(filename, "w") as f:
                for cid in sorted(self._certs.keys()):
                    cert = self._certs[cid]
                    f.write(f"{cid} {cert.password_hash} {cert.level} "
                           f"{cert.creation} {cert.origin}\n")
            return True
        except Exception as e:
            print(f"[Certificate] Error saving: {e}")
            return False

    def get(self, cid: str) -> Optional[Certificate]:
        return self._certs.get(cid)

    def add(self, cert: Certificate):
        self._certs[cert.cid] = cert

    def remove(self, cid: str):
        self._certs.pop(cid, None)

    def has(self, cid: str) -> bool:
        return cid in self._certs

    def max_level(self, cid: str, password: str) -> tuple[bool, int]:
        """Authenticate a client with password (legacy or pre-hashed).

        Returns (success, max_level).
        """
        cert = self.get(cid)
        if not cert:
            return False, LEV_OBSPILOT
        if cert.verify_password(password):
            cert.prev_visit = int(time.time())
            return True, cert.level
        return False, LEV_OBSPILOT

    def max_level_challenge(self, cid: str, response: str,
                            challenge: str) -> tuple[bool, int]:
        """Authenticate with challenge-response."""
        cert = self.get(cid)
        if not cert:
            return False, LEV_OBSPILOT
        if cert.verify_challenge(challenge, response):
            cert.prev_visit = int(time.time())
            return True, cert.level
        return False, LEV_OBSPILOT

    @property
    def all_certs(self) -> list[Certificate]:
        return list(self._certs.values())

    def __len__(self) -> int:
        return len(self._certs)
