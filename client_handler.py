"""FSD Client Protocol Handler - handles #AA, #AP, and all client commands.

Supports both legacy plaintext auth and challenge-response ($CH/$SA) secure auth.
"""

import asyncio
import logging
import re
import time
from typing import Optional

from protocol import *
from certificate import generate_challenge
from auth import check_login, check_login_challenge
import db

logger = logging.getLogger("fsd.client")

# 命令最小字段数守卫（移植自 SimpleFSD CommandRequirements；与各 handler 现有校验保持一致，
# 只做"字段数不足即拒绝"防止半截报文进入解析，不改变正常路径行为，也不致命断开）
COMMAND_MIN_LEN = {
    "#AA": 7,    # callsign::realname:cid:password:rating:revision
    "#AP": 8,    # callsign:type:cid:password:rating:revision:simtype:realname
    "@": 8,      # 位置上报
    "%": 7,      # 管制位置
    "'": 4,      # ATC 视程点
    "#TM": 3,    # 消息
    "$FP": 17,   # 飞行计划
    "$AM": 16,   # 管制修正计划
    "$CQ": 3,
    "$CR": 3,
    "$!!": 2,
    "#SB": 3,
}

# 地球半径(米)/1海里=1852米 —— 距离计算用(移植自 SimpleFSD distance_utils)
_EARTH_RADIUS_M = 6371000.0
_METERS_PER_NM = 1852.0


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """球面余弦定理计算两点距离(海里)。仅用 math, 无外部依赖。"""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    central = math.acos(max(-1.0, min(1.0,
        math.sin(la1) * math.sin(la2) +
        math.cos(la1) * math.cos(la2) * math.cos(lo2 - lo1))))
    return (_EARTH_RADIUS_M * central) / _METERS_PER_NM


def normalize_aircraft(raw):
    """清洗机型代码，统一为「机型/设备后缀」格式。

    FSD 客户端(如 Swift/EuroScope)的机型字段常带设备/类别前缀，
    例如 "H/A359/X"(类别 H=商用客机, 机型 A359, 设备 X)。
    处理后转为 "A359/H"(机型/类别)，便于官网解析与模型匹配。
    - 已无前缀/无斜杠的(如 "B738")保持不变
    - "H/A359/X" -> "A359/H"
    - "A359/X"    -> "A359"
    - 多段取前两段
    """
    s = (raw or '').strip()
    if not s:
        return ''
    parts = [p.strip() for p in s.split('/') if p.strip()]
    # 第一段可能是类别前缀(H/S/L/M/...)或机型；以含数字的那段为准
    model = next((p for p in parts if re.search(r'[0-9]', p)), parts[0] if parts else '')
    # 类别取第一段(若它不是机型本身且不是设备后缀)
    cat = ''
    if parts and parts[0] != model and len(parts) >= 2:
        cat = parts[0]
    result = model if not cat else f"{model}/{cat}"
    # 保留字母+数字部分(去掉多余符号)
    result = re.sub(r'[^A-Za-z0-9/]', '', result)
    return result[:12]


class FlightPlan:
    """A flight plan."""

    def __init__(self):
        self.revision = 0
        self.type = "I"
        self.aircraft = ""
        self.tascruise = 0
        self.depairport = ""
        self.deptime = 0
        self.actdeptime = 0
        self.altitude = ""
        self.destairport = ""
        self.hrsenroute = 0
        self.minenroute = 0
        self.hrsfuel = 0
        self.minfuel = 0
        self.altairport = ""
        self.remarks = ""
        self.route = ""


class Client:
    """Represents a connected client (ATC or Pilot)."""

    def __init__(self, cid: str, callsign: str, client_type: int,
                 rating: int, realname: str, protocol_ver: str,
                 simtype: int = 0):
        self.cid = cid
        self.callsign = callsign
        self.type = client_type  # CLIENT_PILOT or CLIENT_ATC
        self.rating = rating
        self.realname = realname
        self.protocol = protocol_ver
        self.simtype = simtype

        # Position data
        self.lat = 0.0
        self.lon = 0.0
        self.altitude = 0
        self.groundspeed = 0
        self.transponder = 0
        self.frequency = 0
        self.facility_type = 0
        self.visual_range = 40
        self.pbh = 0
        self.flags = 0
        self.ident_flag = ""
        # 是否已上报过有效位置(广播裁剪用: 未上报位置前 fail-open 接收全部)
        self.pos_valid = False
        # 姿态角(度), 由 pbh 解码得到
        self.heading = 0.0
        self.bank = 0.0
        self.pitch = 0.0
        # 高度修正: Correction = PressureAltitude - TrueAltitude
        self.altitude_correction = 0
        # home base 机场(从 #AP realname 末尾提取, 如 "1145 ZSPD" → "ZSPD")
        self.home = ''

        # ATIS 文本（管制员发布的航站情报）
        self.atis = ""
        # ATC 视程点(最多 3 个)：广播范围裁剪时与主位置一起取最近距离
        # (移植自 SimpleFSD AtcSubVisPoint / FindNearestDistance)
        self.vis_points: list = []
        # 航迹采样节流时间戳(whazzup 回放增强: 每 10s 写一点 FlightTrack)
        self.track_last_ts = 0.0

        # Flight plan
        self.plan: Optional[FlightPlan] = None

        # 上线日志 FlightLog 行 id(断开时更新 offlineAt)
        self.flightlog_id = 0

        # 在线快照写入节流时间戳(每 2s 一次)
        self.last_snapshot = 0

        # Timing
        self.start_time = int(time.time())
        self.last_active = time.time()


class ClientHandler:
    """Handles the client-side FSD protocol for one connection."""

    def __init__(self, server, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, is_server: bool = False):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.is_server = is_server

        self.client: Optional[Client] = None
        self.callsign = ""
        self.cid = ""
        self.client_type = 0
        self.secure_auth = False
        self.challenge = generate_challenge()

        self._buffer = ""
        self._closed = False

        self.peer = writer.get_extra_info("peername", ("unknown", 0))

        # 保活监测（修复 2026-10-16）：记录最后一次收到数据的时刻，
        # 供服务端主动 $PI 探测与「静默客户端」诊断使用
        self.last_data_at = time.time()
        self.last_pong_at = 0.0
        self.ping_sent_at = 0.0
        self.ping_missed = 0

    def idle_seconds(self) -> float:
        """距上次收到该客户端数据的秒数（保活/诊断用）。"""
        return max(0.0, time.time() - getattr(self, "last_data_at", time.time()))

    async def send_server_ping(self):
        """服务端主动保活：发 $PI 探测（客户端通常回 $PO）。

        设计取舍：**即使对端不回 $PO 也不主动断开**（部分客户端不实现服务端发起的
        探测，贸然断开会造成新的闪断）。失活判定仍由读超时负责（CLIENT_TIMEOUT，
        已从 800s 下调为 300s），这里只提供保活与可观测性。
        """
        try:
            now = time.time()
            if self.ping_sent_at and (self.last_pong_at < self.ping_sent_at):
                self.ping_missed += 1
            self.ping_sent_at = now
            await self.send(f"$PI{self.callsign}\r\n")
        except Exception:
            pass

    @property
    def rating(self) -> int:
        return self.client.rating if self.client else 0

    def _client_in_range(self, to_handler: "ClientHandler") -> bool:
        """广播裁剪(移植自 SimpleFSD BroadcastToClientInRange)。

        判定 self(发送方) 的位置包是否应发给 to_handler:
        - 任一方未上报过有效位置 → True(fail-open: 新连接的客户端、
          未发 % 的 bot 不能因裁剪而看不到任何人)
        - 阈值: ATC↔ATC 取两者视程最大值; ATC↔机组取 ATC 视程;
          机组↔机组取两者视程之和
        """
        try:
            sender = self.client          # 发送方(位置包的作者)
            receiver = to_handler.client  # 接收方
            if sender is None or receiver is None:
                return True
            if not getattr(sender, "pos_valid", False) or not getattr(receiver, "pos_valid", False):
                return True
            sender_vr = min(sender.visual_range or 0, MAX_VISUAL_RANGE)
            receiver_vr = min(receiver.visual_range or 0, MAX_VISUAL_RANGE)
            sender_atc = self.client_type == CLIENT_ATC
            receiver_atc = to_handler.client_type == CLIENT_ATC
            if sender_atc and receiver_atc:
                threshold = max(sender_vr, receiver_vr)
            elif sender_atc:
                threshold = sender_vr
            elif receiver_atc:
                threshold = receiver_vr
            else:
                threshold = sender_vr + receiver_vr
            dist = self._nearest_distance_nm(sender, receiver)
            return dist <= threshold
        except Exception:
            # 裁剪判定自身绝不能影响正常转发
            return True

    @staticmethod
    def _all_positions(client) -> list:
        """返回某客户端的全部有效位置点（主位置 + ATC 视程点）。

        移植自 SimpleFSD `FindNearestDistance`：ATC 会额外上报最多 3 个视程点，
        范围裁剪应对这些点两两取最近距离，而不是只看主位置。
        """
        pts = []
        try:
            if getattr(client, "pos_valid", False):
                pts.append((float(client.lat), float(client.lon)))
            for p in (getattr(client, "vis_points", None) or []):
                try:
                    lat, lon = float(p[0]), float(p[1])
                except Exception:
                    continue
                if lat != 0 or lon != 0:
                    pts.append((lat, lon))
        except Exception:
            pass
        return pts

    @classmethod
    def _nearest_distance_nm(cls, a, b) -> float:
        """两组位置点之间的最近距离（海里）。任一方无有效点时为 0（调用方已 fail-open）。"""
        import math as _math
        pa, pb = cls._all_positions(a), cls._all_positions(b)
        if not pa or not pb:
            return 0.0
        best = _math.inf
        for (lat1, lon1) in pa:
            for (lat2, lon2) in pb:
                d = distance_nm(lat1, lon1, lat2, lon2)
                if d < best:
                    best = d
        return best

    async def run(self):
        """Main handler loop."""
        # 修复(2026-10-16)：此前断开的 4 条常见路径（对端 FIN / RST / 读超时 / 客户端 #DA,#DP）
        # 全是静默的，导致线上「闪断」无法归因（只能靠排除法）。这里统一记录原因。
        close_reason = "unknown"
        try:
            # Send challenge for secure auth
            await self.send(f"$CH{self.challenge}\r\n")

            while not self._closed:
                data = await asyncio.wait_for(
                    self.reader.read(4096), timeout=CLIENT_TIMEOUT)
                if not data:
                    close_reason = "peer_closed"   # 对端 FIN：客户端主动断开或网络掉线
                    break
                self._buffer += data.decode("utf-8", errors="replace")
                # 安全(2026-10-25 审计 P1-2): 缓冲区上限, 无行尾的慢速攻击直接断开
                if len(self._buffer) > 65536 and "\r\n" not in self._buffer:
                    logger.warning("Buffer overflow (no line ending): %s peer=%s" % (self.callsign or chr(63), self.peer))
                    close_reason = "buffer_overflow"
                    break

                while "\r\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\r\n", 1)
                    await self._process_line(line.strip())

        except asyncio.TimeoutError:
            close_reason = "read_timeout"
            logger.info(f"Client read timeout: {self.callsign or '?'} cid={self.cid or '?'} peer={self.peer}")
        except ConnectionResetError:
            close_reason = "connection_reset"      # 对端 RST
            logger.info(f"Client connection reset: {self.callsign or '?'} cid={self.cid or '?'} peer={self.peer}")
        except Exception as e:
            close_reason = f"error:{type(e).__name__}"
            logger.error(f"Client error: {e}")
        finally:
            if self._closed and close_reason == "unknown":
                close_reason = getattr(self, "_close_reason", "closed_by_server_or_client")
            await self._disconnect(close_reason)

    async def send(self, data: str):
        """Send raw data to the client."""
        try:
            self.writer.write(data.encode())
            await self.writer.drain()
            self.server.stats["packets_sent"] += 1
        except Exception:
            self._closed = True

    async def send_error(self, code: int, env: str = ""):
        """Send an error message."""
        callsign = self.client.callsign if self.client else "unknown"
        err_str = ERR_STR[code] if code < len(ERR_STR) else "Unknown error"
        await self.send(f"$ERserver:{callsign}:{code:03d}:{env}:{err_str}\r\n")

    async def _kick(self, code: int, env: str = ""):
        self._kicked = True
        """认证失败: 发送错误码后断开连接(踢掉该管制员/机组)。

        发 $ER 后短暂停留 0.15s 确保客户端收到错误提示, 再优雅关闭且不等待:
        close() 不等对端响应, 不会挂起; 0.15s 是让 $ER 送达的最小代价,
        与客户端(EuroScope)界面显示"已断开"的延迟无关。
        """
        try:
            await self.send_error(code, env)
        except Exception:
            pass
        self._closed = True
        try:
            # 确保 $ER 错误消息被客户端接收
            await asyncio.sleep(0.15)
        except Exception:
            pass
        try:
            # 优雅关闭(数据已送达), 不等待对端响应
            self.writer.close()
        except Exception:
            try:
                self.writer.transport.abort()
            except Exception:
                pass

    async def send_message(self, from_callsign: str, to_callsign: str,
                           message: str, msg_type: int = CL_MESSAGE):
        """Send a chat/generic message."""
        await self.send(
            f"{CL_CMD_NAMES[msg_type]}{from_callsign}:{to_callsign}:{message}\r\n")

    async def _process_line(self, line: str):
        """Process a single protocol line."""
        if not line:
            return

        # 抓包调试：FSD_RAW_LOG=1 时打印所有原始行（含未识别命令）
        import os as _os
        if _os.environ.get("FSD_RAW_LOG"):
            logger.info(f"[RAW] {line!r} from {self.callsign or '?'}")

        self.server.stats["packets_received"] += 1
        self.last_data_at = time.time()   # 保活监测：记录最后收包时刻

        # 命令最小字段数守卫（移植自 SimpleFSD CommandRequirements，非致命）：
        # 字段数明显不足的半截报文直接回 $ER，不再进入各 handler，避免边界异常。
        for _prefix, _minlen in COMMAND_MIN_LEN.items():
            if line.startswith(_prefix):
                _payload = line[len(_prefix):]
                if _payload and len(_payload.split(":")) < _minlen:
                    logger.warning(f"Short packet rejected: {_prefix} from {self.callsign or '?'} "
                                   f"({len(_payload.split(':'))} < {_minlen})")
                    await self.send_error(ERR_SYNTAX)
                    return
                break

        # EuroScope 等客户端用 #AR(请求ATIS) / #AT(ATIS数据)，此处单独处理
        if line.startswith("#AT"):
            try:
                await self._handle_atis_data(line[3:].split(":"))
            except Exception as e:
                logger.warning(f"ATIS handler error from {self.callsign or '?'}: {e}")
            return
        if line.startswith("#AR"):
            try:
                await self._handle_atis_req(line[3:].split(":"))
            except Exception as e:
                logger.warning(f"ATIS req error from {self.callsign or '?'}: {e}")
            return

        # ' ATC 视程点(子可见范围点)：移植自 SimpleFSD AtcSubVisPoint
        if line.startswith("'"):
            try:
                await self._handle_subvis(line[1:].split(":"))
            except Exception as e:
                logger.warning(f"SubVis handler error from {self.callsign or '?'}: {e}")
            return

        # $AM 修正飞行计划(EuroScope)
        if line.startswith("$AM"):
            try:
                await self._handle_amend_flight_plan(line[3:].split(":"))
            except Exception as e:
                logger.warning(f"AM handler error from {self.callsign or '?'}: {e}")
            return

        # Parse command prefix
        cmd = self._get_command(line)
        if cmd is None:
            return

        # Extract data after command prefix
        data = line[len(CL_CMD_NAMES[cmd]):]
        parts = data.split(":")
        self.server.stats["packets_received"] += 1

        # Route to command handler
        handlers = {
            CL_ADDATC: self._handle_add_atc,
            CL_ADDPILOT: self._handle_add_pilot,
            CL_SECUREAUTH: self._handle_secure_auth,
            CL_RMATC: self._handle_remove,
            CL_RMPILOT: self._handle_remove,
            CL_PILOTPOS: self._handle_pilot_pos,
            CL_ATCPOS: self._handle_atc_pos,
            CL_PLAN: self._handle_flight_plan,
            CL_MESSAGE: self._handle_message,
            CL_PING: self._handle_ping,
            CL_PONG: self._handle_pong,
            CL_KILL: self._handle_kill,
            CL_CQ: self._handle_cq,
            CL_CR: self._handle_cr,
            CL_WEATHER: self._handle_weather,
            CL_REQWEATHER: self._handle_req_weather,
            # ATIS: $AR=请求, $AX=发布/数据（VATSIM FSD）
            CL_REPACARS: self._handle_atis_req,   # $AR
            CL_REQACARS: self._handle_atis_data,  # $AX
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                await handler(parts)
            except Exception as e:
                # 单个报文解析失败不应断开客户端连接
                logger.warning(f"Handler error ({CL_CMD_NAMES[cmd]}) from {self.callsign or '?'}: {e}")
        elif self.client:
            # Unknown command - ignore
            pass

    def _get_command(self, line: str) -> Optional[int]:
        """Identify the command from a protocol line."""
        for idx, prefix in enumerate(CL_CMD_NAMES):
            if prefix and line.startswith(prefix):
                return idx
        return None

    def _callsign_ok(self, name: str) -> int:
        """Validate a callsign. Returns ERR_OK or error code."""
        if len(name) < 2 or len(name) > CALLSIGN_BYTES:
            return ERR_CSINVALID
        invalid_chars = set("!@#$%*:& \t")
        if any(c in invalid_chars for c in name):
            return ERR_CSINVALID
        if name in self.server.clients:
            return ERR_CSINUSE
        return ERR_OK

    async def _handle_secure_auth(self, parts: list):
        """Handle $SA - Secure authentication with challenge-response.

        Format: callsign:type:cid:hmac_response:rating:revision:realname:simtype
        """
        if self.client:
            await self.send_error(ERR_REGISTERED)
            return
        if len(parts) < 8:
            await self.send_error(ERR_SYNTAX)
            return

        callsign, client_type, cid = parts[0], parts[1], parts[2]
        hmac_response, req_rating, revision = parts[3], parts[4], parts[5]
        realname = parts[6] if len(parts) > 6 else ""
        simtype = parts[7] if len(parts) > 7 else "0"

        # Validate
        err = self._callsign_ok(callsign)
        if err:
            await self._kick(err)
            return
        if revision != str(NEED_REVISION):
            await self._kick(ERR_REVISION)
            return

        client_type = int(client_type)
        req = int(req_rating) if req_rating else 1
        # 安全(2026-10-25 审计 P2-2): 客户端类型自报必须合法; ATC 需 fsdLevel>=2
        if client_type not in (CLIENT_PILOT, CLIENT_ATC):
            await self._kick(ERR_SYNTAX)
            return

        # Authenticate
        if self.challenge:
            ok, max_lvl = check_login_challenge(
                self.server.certs, cid, hmac_response, self.challenge)
        else:
            ok, max_lvl = check_login(
                self.server.certs, cid, hmac_response)

        if not ok:
            # 账号密码不匹配: 发送错误后断开连接(踢掉)
            if max_lvl == 0:
                logger.warning(f"用户{cid}登录失败,原因: 账号不存在或已停用")
                await self._kick(ERR_CSSUSPEND)
            else:
                logger.warning(f"用户{cid}登录失败,原因: 账号或密码错误")
                await self._kick(ERR_CIDINVALID, cid)
            return

        # 权限等级 0(SUSPENDED)/1(OBSPILOT) 不做权限判断，账密正确即可登录；仅管制等级(>=2)校验请求等级
        if max_lvl > 1 and req > max_lvl:
            logger.warning(f"用户{cid}登录失败,原因: 请求等级({req})超过账号等级({max_lvl})")
            await self._kick(ERR_LEVEL)
            return

        level = min(req, max_lvl)
        if level < 1:
            level = 1
        # 安全(2026-10-25 审计 P2-2): ATC 身份需 >= S1, 防飞行员账号自报 ATC 获得修正计划/ATIS 权限
        if client_type == CLIENT_ATC and level < LEV_STUDENT1:
            logger.warning(f"Auth rejected: cid={cid} level={level} too low for ATC login")
            await self._kick(ERR_LEVEL)
            return

        self.secure_auth = True
        self.cid = cid
        self.callsign = callsign
        self.client_type = client_type
        self.client = Client(cid, callsign, client_type, level,
                             realname, revision, int(simtype))
        self.server.register_client(self)

        self.server.stats["auth_success"] += 1
        logger.info(f"Secure auth: {callsign} ({cid}) as "
                    f"{'ATC' if client_type == CLIENT_ATC else 'PILOT'} level={level}")
        await self._after_auth()

    async def _handle_add_atc(self, parts: list):
        """Handle #AA - Add ATC (legacy plaintext auth).

        Format: callsign::realname:cid:password:rating:revision
        """
        if self.client:
            await self.send_error(ERR_REGISTERED)
            return
        if len(parts) < 7:
            await self.send_error(ERR_SYNTAX)
            return

        callsign, _, realname, cid, password = parts[0], parts[1], parts[2], parts[3], parts[4]
        req_rating, revision = parts[5], parts[6]

        err = self._callsign_ok(callsign)
        if err:
            await self._kick(err)
            return
        if revision != str(NEED_REVISION):
            logger.warning(f"用户{cid}登录失败,原因: 协议版本不匹配(revision={revision}, 需要{NEED_REVISION})")
            await self._kick(ERR_REVISION)
            return

        req = int(req_rating) if req_rating else 1
        ok, max_lvl = check_login(self.server.certs, cid, password)

        if not ok:
            # 账号密码不匹配: 发送错误后断开连接(踢掉)
            if max_lvl == 0:
                logger.warning(f"用户{cid}登录失败,原因: 账号不存在或已停用")
                await self._kick(ERR_CSSUSPEND)
            else:
                logger.warning(f"用户{cid}登录失败,原因: 账号或密码错误")
                await self._kick(ERR_CIDINVALID, cid)
            return

        # 权限等级 0(SUSPENDED)/1(OBSPILOT) 不做权限判断，账密正确即可登录；仅管制等级(>=2)校验请求等级
        if max_lvl > 1 and req > max_lvl:
            logger.warning(f"用户{cid}登录失败,原因: 请求等级({req})超过账号等级({max_lvl})")
            await self._kick(ERR_LEVEL)
            return

        level = min(req, max_lvl)
        if level < 1:
            level = 1

        self.cid = cid
        self.callsign = callsign
        self.client_type = CLIENT_ATC
        self.client = Client(cid, callsign, CLIENT_ATC, level, realname, revision, -1)
        self.server.register_client(self)

        self.server.stats["auth_success"] += 1
        logger.info(f"Legacy auth: {callsign} ({cid}) as ATC level={level}")
        await self._after_auth()

    async def _handle_add_pilot(self, parts: list):
        """Handle #AP - Add Pilot (legacy plaintext auth).

        Format: callsign:type:cid:password:rating:revision:simtype:realname
        """
        if self.client:
            await self.send_error(ERR_REGISTERED)
            return
        if len(parts) < 8:
            await self.send_error(ERR_SYNTAX)
            return

        callsign, _, cid, password = parts[0], parts[1], parts[2], parts[3]
        req_rating, revision, simtype, realname = parts[4], parts[5], parts[6], parts[7]

        err = self._callsign_ok(callsign)
        if err:
            await self._kick(err)
            return
        if revision != str(NEED_REVISION):
            logger.warning(f"用户{cid}登录失败,原因: 协议版本不匹配(revision={revision}, 需要{NEED_REVISION})")
            await self._kick(ERR_REVISION)
            return

        req = int(req_rating) if req_rating else 1
        ok, max_lvl = check_login(self.server.certs, cid, password)

        if not ok:
            # 账号密码不匹配: 发送错误后断开连接(踢掉)
            if max_lvl == 0:
                logger.warning(f"用户{cid}登录失败,原因: 账号不存在或已停用")
                await self._kick(ERR_CSSUSPEND)
            else:
                logger.warning(f"用户{cid}登录失败,原因: 账号或密码错误")
                await self._kick(ERR_CIDINVALID, cid)
            return

        # 权限等级 0(SUSPENDED)/1(OBSPILOT) 不做权限判断，账密正确即可登录；仅管制等级(>=2)校验请求等级
        if max_lvl > 1 and req > max_lvl:
            logger.warning(f"用户{cid}登录失败,原因: 请求等级({req})超过账号等级({max_lvl})")
            await self._kick(ERR_LEVEL)
            return

        level = min(req, max_lvl)
        if level < 1:
            level = 1

        self.cid = cid
        self.callsign = callsign
        self.client_type = CLIENT_PILOT
        self.client = Client(cid, callsign, CLIENT_PILOT, level,
                             realname, revision, int(simtype))
        self.server.register_client(self)

        self.server.stats["auth_success"] += 1
        logger.info(f"Legacy auth: {callsign} ({cid}) as PILOT level={level}")
        await self._after_auth()

    async def _handle_remove(self, parts: list):
        """Handle #DA / #DP - Remove client."""
        if not self.client or len(parts) < 1:
            return
        callsign = parts[0]
        if callsign == self.callsign:
            # 客户端自己发的 #DA/#DP：记录原因，便于区分「服务端踢」与「客户端自离」
            self._close_reason = "client_da_dp"
            logger.info(f"Client requested disconnect (#DA/#DP): {self.callsign} cid={self.cid}")
            self._closed = True

    async def _handle_pilot_pos(self, parts: list):
        """Handle @ - Pilot position update."""
        if not self.client or len(parts) < 10:
            return
        try:
            self.client.ident_flag = parts[0]
            self.client.transponder = int(parts[2])
            self.client.lat = float(parts[4])
            self.client.lon = float(parts[5])
            self.client.altitude = int(parts[6])
            self.client.groundspeed = int(parts[7])
            self.client.pbh = int(parts[8]) if parts[8] else 0
            # parts[9] = Altitude Correction(气压高度-真实高度), 非 flags
            self.client.altitude_correction = int(parts[9]) if parts[9] else 0
            # FSD 姿态编码: pitch<<22 | bank<<12 | heading<<2, 各 10bit(0..1023 映射 0..360°)
            pbh = self.client.pbh
            self.client.heading = round(((pbh >> 2) & 0x3FF) * 360.0 / 1024.0, 2)
            self.client.bank = round(((pbh >> 12) & 0x3FF) * 360.0 / 1024.0, 2)
            self.client.pitch = round(((pbh >> 22) & 0x3FF) * 360.0 / 1024.0, 2)
            self.client.last_active = time.time()
            self.client.pos_valid = True
        except (ValueError, IndexError):
            return

        # Broadcast pilot position to clients within visual range (SimpleFSD port)
        data = (f"{self.client.ident_flag}:{self.callsign}:{self.client.transponder}:"
                f"{self.client.rating}:{self.client.lat}:{self.client.lon}:"
                f"{self.client.altitude}:{self.client.groundspeed}:{self.client.pbh}:"
                f"{self.client.flags}")
        for cs, h in self.server.clients.items():
            if cs != self.callsign and self._client_in_range(h):
                asyncio.ensure_future(h.send(f"@{data}\r\n"))

        # 在线快照: 机组位置 upsert 到 FlightPilot(节流 2s)
        if self.client and time.time() - self.client.last_snapshot > 2:
            self.client.last_snapshot = time.time()
            asyncio.ensure_future(self._sync_pilot_snapshot())

        # ★ 航迹记录(whazzup 回放增强): 每 10s 采样一点写入 FlightTrack，
        # 供地图回放/事后核查；写库在线程池执行，不阻塞协议处理。
        try:
            if self.client and time.time() - getattr(self.client, "track_last_ts", 0) >= 10:
                self.client.track_last_ts = time.time()
                asyncio.ensure_future(self._record_track_point())
        except Exception:
            pass

    async def _record_track_point(self):
        """把当前机组位置写入 FlightTrack（异步、失败仅告警）。"""
        try:
            c = self.client
            if c is None or not self.callsign:
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, db.record_track, self.callsign,
                getattr(c, "flightlog_id", 0), c.lat, c.lon,
                c.altitude, c.groundspeed, int(c.heading or 0),
                bool(getattr(c, "on_ground", False)))
        except Exception as e:
            logger.debug(f"track record skipped: {e}")

    async def _handle_atc_pos(self, parts: list):
        """Handle % - ATC position update.

        EuroScope 格式: %callsign:frequency:facilitytype:visualrange:rating:lat:lon:alt
        _process_line 已去掉 % 前缀，故 parts[0]=callsign。
        """
        if not self.client or len(parts) < 8:
            return
        if self.client_type != CLIENT_ATC:
            return
        try:
            raw_freq = int(parts[1])
            # EuroScope 频率编码(如 24550) → MHz(124.55): 前加 '1' 得 124550, 倒数第3位前插小数点 → 124.550
            fs = "1" + str(raw_freq)
            freq = float(fs[:-3] + "." + fs[-3:]) if len(fs) >= 4 else float(fs)
            facility = int(parts[2])
            # 评级×设施矩阵(移植自 SimpleFSD RatingFacilityMap):
            # 席位类型不得超过账号评级, 防止低评级账号伪装 CTR/APP。
            # 拒绝该包但不踢线(比 SimpleFSD 的 kick 更保守, 避免误伤客户端怪癖)
            min_rating = FACILITY_MIN_RATING.get(facility)
            if min_rating is None:
                logger.warning(f"Facility check: {self.callsign} sent unknown facility {facility}, rejected")
                return
            if self.client.rating < min_rating:
                logger.warning(
                    f"Facility check: {self.callsign}(cid={self.cid}, rating={self.client.rating}) "
                    f"tried facility {facility} (requires >={min_rating}), rejected")
                await self._kick(ERR_LEVEL)
                return
            self.client.frequency = freq
            self.client.facility_type = facility
            self.client.visual_range = min(int(parts[3]), MAX_VISUAL_RANGE)
            self.client.lat = float(parts[5])
            self.client.lon = float(parts[6])
            self.client.altitude = int(parts[7])
            self.client.last_active = time.time()
            self.client.pos_valid = True
        except (ValueError, IndexError):
            return

        # Broadcast ATC position to clients within visual range (SimpleFSD port)
        # Format: callsign:frequency:facilityType:visualRange:rating:lat:lon:altitude
        # 广播保留原始编码(raw_freq=24550), 避免其他 FSD 客户端解析错误
        data = (f"{self.callsign}:{raw_freq}:{self.client.facility_type}:"
                f"{self.client.visual_range}:{self.client.rating}:{self.client.lat}:"
                f"{self.client.lon}:{self.client.altitude}")
        for cs, h in self.server.clients.items():
            if cs != self.callsign and self._client_in_range(h):
                asyncio.ensure_future(h.send(f"%{data}\r\n"))

        # 在线快照: 管制位置 upsert 到 FlightController(节流 2s)
        if self.client and time.time() - self.client.last_snapshot > 2:
            self.client.last_snapshot = time.time()
            asyncio.ensure_future(self._sync_atc_snapshot())

    async def _handle_flight_plan(self, parts: list):
        """Handle $FP - Flight plan submission.

        Swift/EuroScope 等客户端 $FP 字段类型可能不同（如 'I' 出现在数字段），
        必须容错解析，任何字段解析失败都不能断开客户端连接。
        """
        if not self.client or len(parts) < 4:
            return
        try:
            # $FP 前缀已被 _process_line 去掉，parts[0]=callsign, parts[1]=revision, parts[2]=type
            # （参照 fsd-master execfp 用 array+2）
            fp = FlightPlan()
            fp.type = parts[2][0] if len(parts) > 2 and parts[2] else "I"
            fp.aircraft = normalize_aircraft(parts[3] if len(parts) > 3 else '')
            fp.tascruise = self._safe_int(parts[4]) if len(parts) > 4 else 0
            fp.depairport = parts[5] if len(parts) > 5 else ""
            fp.deptime = self._safe_int(parts[6]) if len(parts) > 6 else 0
            fp.actdeptime = self._safe_int(parts[7]) if len(parts) > 7 else 0
            fp.altitude = parts[8] if len(parts) > 8 else ""
            fp.destairport = parts[9] if len(parts) > 9 else ""
            fp.hrsenroute = self._safe_int(parts[10]) if len(parts) > 10 else 0
            fp.minenroute = self._safe_int(parts[11]) if len(parts) > 11 else 0
            fp.hrsfuel = self._safe_int(parts[12]) if len(parts) > 12 else 0
            fp.minfuel = self._safe_int(parts[13]) if len(parts) > 13 else 0
            fp.altairport = parts[14] if len(parts) > 14 else ""
            fp.remarks = parts[15] if len(parts) > 15 else ""
            fp.route = parts[16] if len(parts) > 16 else ""
            # 计划真实呼号 = parts[0]（$FP 报文里的呼号）。
            # 安全(2026-08-23): 仅允许提交自己呼号的计划，防止冒用他人呼号污染 FlightPlan 表；
            # 系统客户端(CW- 前缀/CID 0000)代官网提交时豁免。
            fp_callsign = parts[0].strip() if len(parts) > 0 and parts[0].strip() else self.callsign
            is_system_bot = self.cid == '0000'   # 安全(2026-10-25 审计 P0-2): CW- 呼号前缀可被普通用户冒用, 仅认 CID
            if not is_system_bot and fp_callsign != self.callsign:
                logger.warning(f"FP spoof blocked: {self.callsign}({self.cid}) tried to submit plan for {fp_callsign}")
                return
            # 计划锁定(移植自 SimpleFSD flight plan locking):
            # 管制员 $AM 修正过的计划在机组本次在线期间被锁定; 机组再提交时:
            #   - 起降机场未变 → 拒绝并提示联系管制员(不覆盖、不广播)
            #   - 起降机场已变 → 按 SimpleFSD 语义自动解锁后接受本次提交
            # 系统客户端(官网代发)豁免。
            if not is_system_bot and fp_callsign in self.server.locked_plans:
                old = self.client.plan
                changed = (old is not None and (
                    str(getattr(old, "depairport", "")).upper() != str(fp.depairport).upper() or
                    str(getattr(old, "destairport", "")).upper() != str(fp.destairport).upper()))
                if not changed:
                    logger.info(f"FP locked: {fp_callsign} tried to re-submit an ATC-amended plan")
                    try:
                        await self.send_message(
                            "SERVER", self.callsign,
                            "Your flight plan has been amended and locked by ATC. "
                            "Please contact the controller to change it.")
                    except Exception:
                        pass
                    return
                logger.info(f"FP lock released by dep/arr change: {fp_callsign}")
                self.server.locked_plans.discard(fp_callsign)
                asyncio.ensure_future(self._set_plan_locked_db(fp_callsign, False))
            self.client.plan = fp
            logger.info(f"FP: {fp_callsign} {fp.depairport}->{fp.destairport}")

            # 落库 + 机组主动提交触发规则广播给在线 ATC; 另 $CQ FP 请求时也可拉取
            fp.callsign = fp_callsign
            asyncio.ensure_future(self._persist_plan(fp_callsign, fp))
            asyncio.ensure_future(self._broadcast_plan_event(fp_callsign))
        except Exception as e:
            # 解析失败绝不断开连接，仅记录
            logger.warning(f"FP parse error from {self.callsign}: {e}")

    async def _handle_amend_flight_plan(self, parts: list):
        """Handle $AM - Amend flight plan(EuroScope 修正飞行计划).

        格式: from:to:callsign:rules:aircraft:tas:dep:estdep:actdep:alt:
              dest:hrsen:minen:hrsfuel:minfuel:altairport:remarks:route
        """
        if not self.client or len(parts) < 16:
            return
        # 权限(移植自 SimpleFSD handleAtcEditPlan, 并加严):
        # 1. 仅管制员客户端可修正(此前飞行员也能改任何人的计划 — 越权漏洞)
        # 2. 评级至少 S1(SimpleFSD AllowAtcFacility 不含 OBS)
        # 3. 报文源呼号(parts[0])必须与登录呼号一致, 防伪造
        # 4. 目标呼号必须是当前在线的机组(SimpleFFD 同样要求目标客户端存在,
        #    同时防止对任意呼号做 DB 污染 / 锁定)
        if self.client_type != CLIENT_ATC or self.client.rating < LEV_STUDENT1:
            logger.warning(f"AM privilege blocked: {self.callsign}({self.cid}, "
                           f"type={self.client_type}, rating={self.client.rating})")
            return
        if parts[0].strip() != self.callsign:
            logger.warning(f"AM spoof blocked: {self.callsign} tried to amend as {parts[0]}")
            return
        try:
            callsign = parts[2] if len(parts) > 2 else ""
            target = self.server.get_client(callsign) if callsign else None
            if not target or target.client_type != CLIENT_PILOT:
                logger.info(f"AM ignored: target {callsign!r} not an online pilot (from {self.callsign})")
                return
            fp = FlightPlan()
            fp.type = parts[3][0] if len(parts) > 3 and parts[3] else 'I'
            fp.aircraft = normalize_aircraft(parts[4] if len(parts) > 4 else '')
            fp.tascruise = self._safe_int(parts[5]) if len(parts) > 5 else 0
            fp.depairport = parts[6] if len(parts) > 6 else ''
            fp.deptime = self._safe_int(parts[7]) if len(parts) > 7 else 0
            fp.actdeptime = self._safe_int(parts[8]) if len(parts) > 8 else 0
            fp.altitude = parts[9] if len(parts) > 9 else ''
            fp.destairport = parts[10] if len(parts) > 10 else ''
            fp.hrsenroute = self._safe_int(parts[11]) if len(parts) > 11 else 0
            fp.minenroute = self._safe_int(parts[12]) if len(parts) > 12 else 0
            fp.hrsfuel = self._safe_int(parts[13]) if len(parts) > 13 else 0
            fp.minfuel = self._safe_int(parts[14]) if len(parts) > 14 else 0
            fp.altairport = parts[15] if len(parts) > 15 else ''
            fp.remarks = parts[16] if len(parts) > 16 else ''
            fp.route = parts[17] if len(parts) > 17 else ''
            fp.callsign = callsign
            # 预计飞行时间 H:MM
            fp.flightTime = f"{fp.hrsenroute}:{fp.minenroute:02d}"
            logger.info(f"AM: {self.callsign} amended {callsign} "
                        f"{fp.depairport}->{fp.destairport}")

            # 更新内存计划 + 落库, 成功后锁定该呼号并广播给所有在线管制员
            # (此前修正不广播, 其他 EuroScope 看到的还是旧计划 — SimpleFSD 语义)
            if target.client is not None:
                target.client.plan = fp
            await self._amend_plan_db(callsign, fp)
            self.server.locked_plans.add(callsign)
            asyncio.ensure_future(self._set_plan_locked_db(callsign, True))
            asyncio.ensure_future(self._broadcast_plan_event(callsign))
            try:
                await target.send_message(
                    "SERVER", callsign,
                    f"Your flight plan has been amended by {self.callsign} and is now locked. "
                    "Contact the controller for further changes.")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"AM parse error from {self.callsign}: {e}")

    async def _set_plan_locked_db(self, callsign, locked: bool):
        """把计划锁状态写入 FlightPlan.locked（便于后端/前端展示与重启后保持）。"""
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, db.set_plan_locked, callsign, locked)
        except Exception as e:
            logger.warning(f"Persist plan lock failed for {callsign}: {e}")

    async def _amend_plan_db(self, callsign, fp):
        """$AM 修正落库。"""
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, db.amend_flight_plan, callsign, fp)
        except Exception:
            pass

    @staticmethod
    def _safe_int(value) -> int:
        """安全转 int，失败返回 0。"""
        if value is None:
            return 0
        s = str(value).strip()
        if not s:
            return 0
        try:
            return int(s)
        except (ValueError, TypeError):
            return 0

    async def _handle_message(self, parts: list):
        """Handle #TM - Text message."""
        if not self.client or len(parts) < 3:
            return
        from_cs = parts[0]
        to_cs = parts[1]
        message = ":".join(parts[2:])

        if from_cs != self.callsign:
            logger.warning(f"TM spoof blocked: {self.callsign} tried to send as {from_cs}")
            return

        if to_cs == "server" or to_cs == "*":
            return

        # Find target client and forward
        target = self.server.get_client(to_cs)
        if target:
            await target.send_message(from_cs, to_cs, message)

    async def _handle_ping(self, parts: list):
        """Handle $PI - Ping, respond with $PO."""
        if not self.client:
            return
        await self.send(f"$PO{self.callsign}:{':'.join(parts[1:])}\r\n")

    async def _handle_pong(self, parts: list):
        """Handle $PO - Pong（服务端主动保活探测的应答）。"""
        self.last_pong_at = time.time()
        self.ping_missed = 0

    async def _handle_kill(self, parts: list):
        """Handle $!! - Kill/disconnect a client.

        格式: $!!被踢呼号:管理员呼号:原因(可选)
        """
        if not self.client or len(parts) < 3:
            return
        # 安全(2026-10-25 审计 P3-2): 校验报文源(parts[0]=管理员呼号)与登录一致, 防伪造
        if parts[0].strip() != self.callsign:
            logger.warning(f"Kill spoof blocked: {self.callsign} tried to kill as {parts[0]}")
            return
        target_cs = parts[1]
        reason = ":".join(parts[2:]) if len(parts) > 2 else ""
        if self.client.rating >= LEV_SUPERVISOR:
            target = self.server.get_client(target_cs)
            if target:
                logger.info(f"{self.callsign} killed {target_cs} reason={reason}")
                # 发 $!! KillRequest(Swift 据此显示"被踢出"并正常断开, 避免 RemoteHostClosedError)
                # 同时发 #TM 聊天提示(其他客户端/兼容); 短暂停留确保送达
                try:
                    await target.send(f"$!!{self.callsign}:{target_cs}:{reason}\r\n")
                    await target.send_message(
                        "SERVER", target_cs,
                        f"You have been kill! Reason:{reason}")
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
                target._closed = True

    async def _handle_subvis(self, parts: list):
        """Handle ' - ATC 视程点（子可见范围点），移植自 SimpleFSD AtcSubVisPoint。

        格式: '  <callsign> <index 0-2> <lat> <lon>
        最多 3 个点，参与广播范围裁剪（与主位置一起取最近距离）。
        """
        if not self.client or self.client_type != CLIENT_ATC or len(parts) < 4:
            return
        try:
            idx = int(parts[1])
            lat = float(parts[2])
            lon = float(parts[3])
        except (ValueError, TypeError):
            return
        if idx < 0 or idx > 2:
            return
        try:
            pts = self.client.vis_points
            while len(pts) < 3:
                pts.append((0.0, 0.0))
            pts[idx] = (lat, lon)
        except Exception:
            pass

    async def _handle_cr(self, parts: list):
        """Handle $CR - 客户端回复（含 ATIS 上报）。

        移植自 SimpleFSD handleClientResponse：
          $CR<cs>:SERVER:ATIS:T:<text>  → 记录该管制员的 ATIS 文本
          其它 $CR 原样转发给目标客户端
        """
        if not self.client or len(parts) < 3:
            return
        try:
            if parts[1].upper() == "SERVER" and parts[2].upper() == "ATIS" and len(parts) >= 5:
                if parts[3].upper() == "T":
                    text = ":".join(parts[4:]).strip()
                    if text:
                        self.client.atis = text
                        logger.info(f"ATIS received: {self.callsign} ({len(text)} chars)")
                return
        except Exception:
            pass
        target = parts[0]
        try:
            target_h = self.server.get_client(target)
            if target_h is not None:
                await target_h.send("$CR" + ":".join(parts) + "\r\n")
        except Exception:
            pass

    async def _handle_cq(self, parts: list):
        """Handle $CQ - CQ/chat request."""
        if not self.client or len(parts) < 3:
            return
        to_cs = parts[1]
        msg_type = parts[2]

        if msg_type == "RN" and len(parts) >= 4:
            # Real name request
            target = self.server.get_client(to_cs)
            if target and target.client:
                await self.send(
                    f"$CR{to_cs}:{self.callsign}:RN:{target.client.realname}:"
                    f"USER:{target.rating}\r\n")
            return

        if msg_type == "FP" and len(parts) >= 4:
            # EuroScope 请求某机组飞行计划: 仅发送近 30 分钟提交的, 否则空 $FP
            asyncio.ensure_future(self._reply_fp_request(parts[3]))
            return

    async def _reply_fp_request(self, target_callsign):
        """响应 $CQ FP 请求: 发送该机组近 30 分钟提交的飞行计划, 否则空 $FP。"""
        try:
            plan = await asyncio.get_running_loop().run_in_executor(
                None, db.get_recent_plan, target_callsign)
            callsign = target_callsign
            if plan:
                ft = 'I' if str(plan.get("flightType") or "").upper().startswith('I') else 'V'
                dep_time = str(plan.get("depTime") or "")[:4]
                line = (
                    f"$FP{callsign}:0:{ft}:{plan.get('aircraft') or ''}:"
                    f"{plan.get('cruiseSpeed') or ''}:{plan.get('depIcao') or ''}:{dep_time}:0:"
                    f"{plan.get('cruiseAlt') or ''}:{plan.get('arrIcao') or ''}:0:0:0:0:"
                    f"{plan.get('alternate') or ''}:{plan.get('remarks') or ''}:{plan.get('route') or ''}\r\n"
                )
            else:
                # 无近 30 分钟计划: 发送只带呼号的空 $FP
                empty = [callsign, '0', 'I', '', '0', '', '0', '', '', '', '0', '0', '0', '0', '', '', '']
                line = "$FP" + ":".join(empty) + "\r\n"
            await self.send(line)
        except Exception:
            pass

    async def _handle_weather(self, parts: list):
        """Handle #WX - Weather request."""
        pass

    async def _handle_req_weather(self, parts: list):
        """Handle #RW - Request weather."""
        pass

    async def _handle_atis_req(self, parts: list):
        """Handle $AR - Request ATIS from another ATC.

        Format: $AR:from:to   (from = requester, to = target ATC callsign)
        """
        if not self.client or len(parts) < 2:
            return
        to_cs = parts[1]
        target = self.server.get_client(to_cs)
        if target and target.client and target.client.atis:
            # 回复 ATIS 文本（多行用 ^ 分隔）
            data = target.client.atis.replace("\n", "^").replace("\r", "")
            await self.send(f"$AX{to_cs}:{self.callsign}:{data}\r\n")

    async def _handle_atis_data(self, parts: list):
        """Handle ATIS data published by an ATC.

        支持格式（来自 EuroScope #AT 或自定义 $AX）：
          #AT:from:to:data  → 去掉前导空段后 parts=[from,to,data...]
          #ATfrom:to:data   → parts=[from,to,data...]
          $AX:from:*:data   → parts=[from,*,data...]
          $AXfrom:data      → parts=[from,data...]
        data 中多行用 ^ 分隔。收到后存储并广播给其他客户端（含 bot）。
        """
        if not self.client or self.client_type != CLIENT_ATC:
            return
        if not parts:
            return

        # 去掉可能的前导空字段（#AT:from:... 会切出 parts[0]=''）
        while parts and parts[0] == "":
            parts = parts[1:]
        if not parts:
            return

        from_cs = parts[0]
        # 归一化：parts[1] 若是 '*' 或目标呼号（非数据首段），跳过它
        if len(parts) >= 2 and parts[1] in ("*", ""):
            atis_text = ":".join(parts[2:])
        elif len(parts) >= 2 and parts[1].upper() != from_cs.upper() and " " not in parts[1]:
            # 三段式 from:to:data（to 是纯呼号段）
            atis_text = ":".join(parts[2:])
        else:
            # 两段式 from:data（data 可能含冒号）
            atis_text = ":".join(parts[1:])
        atis_text = atis_text.replace("^", "\n").strip()
        # 安全(2026-10-25 审计 P2-3): ATIS 长度/频率限制, 防广播放大
        if len(atis_text) > 2048:
            logger.warning(f"ATIS too long ({len(atis_text)}) from {self.callsign}, rejected")
            return
        now = time.time()
        if now - getattr(self.client, 'last_atis_at', 0) < 1.0:
            return
        self.client.last_atis_at = now

        self.client.atis = atis_text
        logger.info(f"ATIS: {self.callsign} updated ({len(atis_text)} chars)")

        # 广播给所有其他客户端（bot 能收到并用于通知）
        data = atis_text.replace("\n", "^").replace("\r", "")
        for cs, h in self.server.clients.items():
            if cs != self.callsign:
                asyncio.ensure_future(
                    h.send(f"$AX{self.callsign}:{cs}:{data}\r\n"))

    async def _after_auth(self):
        """认证成功后: 写 FlightLog 上线日志; 管制员则从 FlightPlan 广播已有计划。"""
        if not self.client:
            return
        # 提取 home base 机场
        m = re.search(r'([A-Z]{4})$', (self.client.realname or '').strip().upper())
        if m:
            self.client.home = m.group(1)
            # 从 realname 移除末尾的 home
            self.client.realname = re.sub(r'\s+[A-Z]{4}$', '', (self.client.realname or '').strip())
        is_system_bot = self.cid == '0000'   # 安全(2026-10-25 审计 P0-2): CW- 呼号前缀可被普通用户冒用, 仅认 CID
        if not is_system_bot:
            try:
                ip = self.peer[0] if isinstance(self.peer, tuple) and self.peer else ""
                loop = asyncio.get_running_loop()
                row_id = await loop.run_in_executor(
                    None, db.write_online, self.cid, self.callsign, ip)
                if row_id:
                    self.client.flightlog_id = row_id
            except Exception:
                pass
        # 飞行计划不再于管制员上线时广播; EuroScope 通过 $CQ FP 请求获取
        # 修复(2026-10-16): 机组登录时装载其「上线前在官网提交」的计划。
        # 此前官网计划永远不会进入 FSD（管理端口 planadd 未实现、周期扫描被删除），
        # 只有机组自己在客户端里发 $FP 才可见 → 现在登录即自动装载并广播给在线管制员。
        if self.client_type == CLIENT_PILOT:
            asyncio.ensure_future(self._load_filed_plan_on_login())
        else:
            # ★ 移植自 SimpleFSD：ATC 登录后由服务端主动索取 ATIS
            # ($CQSERVER:<cs>:ATIS → 客户端回 $CR<cs>:SERVER:ATIS:T:<text>)
            try:
                asyncio.ensure_future(self.send(f"$CQSERVER:{self.callsign}:ATIS\r\n"))
            except Exception:
                pass

    async def _load_filed_plan_on_login(self):
        """登录时装载该呼号在 FlightPlan 表中的最新计划（官网提交的计划）。

        - 仅在客户端尚未通过 $FP 自行提交计划时装载（不覆盖客户端自己的计划）
        - 只注入内存 client.plan（whazzup/地图/雷达即可见），不写库（官网已入库）
        - 随后广播 $FP 给在线管制员，使 EuroScope 立刻看到计划
        """
        try:
            if not self.client or self.client.plan:
                return
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, db.get_plan_by_callsign, self.callsign)
            if not rows:
                return
            if self.apply_plan_row(rows[0]):
                logger.info(
                    f"FP loaded on login: {self.callsign} "
                    f"{rows[0].get('depIcao')}->{rows[0].get('arrIcao')}")
                # ★ 移植自 SimpleFSD handleAddPilot：官网计划呼号与登录呼号不符时提示机组
                try:
                    row_cs = str(rows[0].get("callsign") or "").strip().upper()
                    if row_cs and row_cs != (self.callsign or "").upper():
                        await self.send_message(
                            "FPlanManager", self.callsign,
                            f"官网提交的计划呼号是 {row_cs}，与当前登录呼号 "
                            f"{self.callsign} 不一致，请核对后再起飞")
                except Exception:
                    pass
                await self._broadcast_plan_event(self.callsign)
        except Exception as e:
            logger.warning(f"Load filed plan on login failed: {e}")

    def apply_plan_row(self, row) -> bool:
        """把 FlightPlan 表的一行装载为内存 client.plan（登录装载 / 周期补发复用）。"""
        if not self.client or not row:
            return False
        try:
            fp = FlightPlan()
            fp.callsign = self.callsign
            fp.type = 'I' if str(row.get('flightType') or 'IFR').upper().startswith('I') else 'V'
            fp.aircraft = normalize_aircraft(row.get('aircraft') or '')
            fp.tascruise = self._safe_int(row.get('cruiseSpeed'))
            fp.depairport = str(row.get('depIcao') or '')
            fp.deptime = self._safe_int(row.get('depTime'))
            fp.altitude = str(row.get('cruiseAlt') or '')
            fp.destairport = str(row.get('arrIcao') or '')
            fp.altairport = str(row.get('alternate') or '')
            fp.remarks = str(row.get('remarks') or '')
            fp.route = str(row.get('route') or '')
            self.client.plan = fp
            return True
        except Exception as e:
            logger.warning(f"Apply plan row failed for {self.callsign}: {e}")
            return False

    async def _broadcast_plans_from_db(self):
        """管制员上线: 广播所有飞行计划(每呼号最新一条), 让管制员看到全部机组计划。"""
        try:
            loop = asyncio.get_running_loop()
            plans = await loop.run_in_executor(None, db.get_online_flight_plans)
            for p in plans:
                callsign = p.get("callsign") or ""
                if not callsign:
                    continue
                ft = 'I' if str(p.get("flightType") or "").upper().startswith('I') else 'V'
                dep_time = str(p.get("depTime") or "")[:4]
                line = (
                    f"$FP{callsign}:0:{ft}:{p.get('aircraft') or ''}:"
                    f"{p.get('cruiseSpeed') or ''}:{p.get('depIcao') or ''}:{dep_time}:0:"
                    f"{p.get('cruiseAlt') or ''}:{p.get('arrIcao') or ''}:0:0:0:0:"
                    f"{p.get('alternate') or ''}:{p.get('remarks') or ''}:{p.get('route') or ''}\r\n"
                )
                await self.send(line)
        except Exception:
            pass

    async def _persist_plan(self, callsign, fp):
        """机组 $FP 落库 FlightPlan: userId 用当前认证客户的 cid(官网已直接入库)。"""
        try:
            user_id = self.cid
            if user_id:
                await asyncio.get_running_loop().run_in_executor(
                    None, db.write_flight_plan, user_id, callsign, fp)
        except Exception:
            pass

    async def broadcast_plan_to_atc(self, callsign=None):
        """公开包装：把指定呼号（默认自己）的最新计划广播给在线管制员。"""
        await self._broadcast_plan_event(callsign or self.callsign)

    async def _broadcast_plan_event(self, callsign):
        """机组主动提交 $FP 后触发: 广播该呼号最新计划给所有在线 ATC。"""
        try:
            loop = asyncio.get_running_loop()
            plans = await loop.run_in_executor(None, db.get_plan_by_callsign, callsign)
            if not plans:
                return
            p = plans[0]
            ft = 'I' if str(p.get("flightType") or "").upper().startswith('I') else 'V'
            dep_time = str(p.get("depTime") or "")[:4]
            line = (
                f"$FP{callsign}:0:{ft}:{p.get('aircraft') or ''}:"
                f"{p.get('cruiseSpeed') or ''}:{p.get('depIcao') or ''}:{dep_time}:0:"
                f"{p.get('cruiseAlt') or ''}:{p.get('arrIcao') or ''}:0:0:0:0:"
                f"{p.get('alternate') or ''}:{p.get('remarks') or ''}:{p.get('route') or ''}\r\n"
            )
            for h in self.server.clients.values():
                if h.client_type == CLIENT_ATC:
                    asyncio.ensure_future(h.send(line))
        except Exception:
            pass

    async def _sync_pilot_snapshot(self):
        """机组位置 upsert 到 FlightPilot 快照(含姿态角/高度修正/home)。"""
        try:
            c = self.client
            if not c:
                return
            await asyncio.get_running_loop().run_in_executor(
                None, db.upsert_pilot,
                self.cid, self.callsign, c.realname or '', c.home or '',
                c.lat, c.lon, c.altitude, c.bank, c.pitch, c.heading,
                c.altitude_correction, c.groundspeed,
                c.transponder, c.ident_flag or '')
        except Exception:
            pass

    async def _sync_atc_snapshot(self):
        """管制位置 upsert 到 FlightController 快照。"""
        try:
            c = self.client
            if not c:
                return
            await asyncio.get_running_loop().run_in_executor(
                None, db.upsert_controller,
                self.cid, self.callsign, c.realname or '',
                c.lat, c.lon, c.frequency, c.facility_type, c.visual_range)
        except Exception:
            pass

    async def _disconnect(self, reason: str = "unknown"):
        if getattr(self, "_kicked", False):
            reason = "kicked"
        """Clean up on disconnect.

        ★ 会话重连（移植自 SimpleFSD session_clean_time）：可恢复的断开先进入
        RECONNECT_GRACE 秒重连窗口，期间保留会话（飞行计划 / ATIS / 上线日志行 /
        在线快照），同呼号同 CID 重连即恢复 —— 闪断不再被记成掉线+上线两次，
        也不再丢失已装载的计划。
        """
        if self.client:
            # 记录断开原因与在线时长：此前全静默，导致「闪断」无法归因
            try:
                started = getattr(self.client, "start_time", None) or getattr(self, "connected_at", None)
                online = int(time.time() - started) if started else 0
                peer = self.peer[0] if isinstance(self.peer, tuple) and self.peer else ""
                logger.info(
                    f"Client disconnected: {self.callsign} cid={self.cid} "
                    f"peer={peer} reason={reason} online={online}s")
            except Exception:
                pass
            # ★ 可恢复的断开：进入重连窗口，跳过下面的下线清理
            try:
                if self.server.mark_for_reconnect(self, reason):
                    try:
                        self.writer.close()
                    except Exception:
                        try:
                            self.writer.transport.abort()
                        except Exception:
                            pass
                    return
            except Exception as e:
                logger.warning(f"mark_for_reconnect failed: {e}")
            # ★ 在线时长结算 + 计划解锁（移植自 SimpleFSD UpdateUser*Time / FlightPlan.Locked）
            # 注意：只在「会话真正终结」时结算；重连窗口内的短暂断开不结算，避免闪断重复计时
            try:
                _started = getattr(self.client, "start_time", None) or getattr(self, "connected_at", None)
                _seconds = int(time.time() - _started) if _started else 0
                _loop = asyncio.get_running_loop()
                if _seconds > 0 and self.cid and str(self.cid) != "0000":
                    await _loop.run_in_executor(
                        None, db.add_online_time, str(self.cid),
                        self.client_type == CLIENT_ATC, _seconds)
                    logger.info(f"Online time settled: {self.callsign} cid={self.cid} "
                                f"{_seconds}s ({'ATC' if self.client_type == CLIENT_ATC else 'PILOT'})")
                if self.callsign in self.server.locked_plans:
                    self.server.locked_plans.discard(self.callsign)
                    await _loop.run_in_executor(None, db.set_plan_locked, self.callsign, False)
            except Exception as e:
                logger.warning(f"Settle online time / unlock failed: {e}")
            # 下线: 更新 FlightLog.offlineAt
            if getattr(self.client, "flightlog_id", 0):
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, db.update_offline, self.client.flightlog_id)
                except Exception:
                    pass
            # 下线: 从在线快照表删除
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, db.delete_online, self.callsign)
            except Exception:
                pass
            self.server.unregister_client(self)
        try:
            self.writer.close()
        except Exception:
            try:
                self.writer.transport.abort()
            except Exception:
                pass
