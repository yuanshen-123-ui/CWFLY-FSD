"""Core FSD Server - manages clients, servers, certificates, and protocols."""

import asyncio
import logging
import time
from typing import Optional

from config import Config
from protocol import *
from certificate import CertificateStore, Certificate, generate_challenge
from client_handler import ClientHandler, Client, FlightPlan
from server_link import ServerLinkManager
from management import ManagementConsole
from weather import WeatherManager
from whazzup import WhazzupGenerator
import db

logger = logging.getLogger("fsd")

# 飞行计划周期补发间隔（秒）
PLAN_DISPATCH_INTERVAL = 10
# 保活探测：每 30s 扫描一次，客户端静默超过 45s 就主动发 $PI 探测
KEEPALIVE_INTERVAL = 30
KEEPALIVE_IDLE = 45
KEEPALIVE_WARN = 120
# ★ 服务端心跳：每 N 秒向所有在线客户端广播 #DL（风速扰动）
# 移植自 SimpleFSD HeartbeatSender：既模拟风的变化，也让链路保持活跃、及早暴露死连接
HEARTBEAT_INTERVAL = 60
# ★ 会话重连窗口（移植自 SimpleFSD session_clean_time，默认 40s）：
# 客户端异常断开后保留会话，期间同呼号同 CID 重连可恢复原会话
RECONNECT_GRACE = 40
# 可恢复的断开原因（对端 FIN/RST、读超时、客户端主动 #DA/#DP 等）；
# 管理口 kill、服务端停机、缓冲区攻击等不进入重连窗口，必须立即下线。
RECONNECTABLE_REASONS = {
    "peer_closed", "read_timeout", "connection_reset",
    "client_da_dp", "closed_by_server_or_client", "unknown",
}


class FSDServer:
    """Main FSD server class. Manages all interfaces and state."""

    def __init__(self, config: Config):
        self.config = config
        self.running = False

        # Server identity
        self.ident = config.server_ident or f"FSD{os.getpid()}"
        self.name = config.server_name
        self.email = config.server_email
        self.hostname = config.server_host
        self.location = config.server_location

        # Core components
        self.certs = CertificateStore()
        self.server_links = ServerLinkManager(self)
        self.weather = WeatherManager(self)
        self.whazzup = WhazzupGenerator(self)

        # State
        self.clients: dict[str, ClientHandler] = {}  # callsign -> handler
        self.client_list: list[Client] = []  # all connected clients
        # 被管制 $AM 修正而锁定的呼号集合(移植自 SimpleFSD flight plan locking)。
        # 机组断线即解锁(与 SimpleFSD 会话级锁一致), 在 unregister_client 统一回收。
        self.locked_plans: set[str] = set()
        # ★ 会话重连窗口(移植自 SimpleFSD session_clean_time)：
        # callsign -> {handler, client, task, reason, expires_at}
        # 异常断开后不立即注销，保留 N 秒；期间同呼号同 CID 重连可恢复原会话
        # （保留飞行计划/ATIS/上线日志行/在线快照），避免闪断造成计划丢失与重复上下线广播。
        self.pending_sessions: dict[str, dict] = {}
        self.start_time = int(time.time())

        # Statistics
        self.stats = {
            "connections": 0,
            "packets_received": 0,
            "packets_sent": 0,
            "auth_success": 0,
            "auth_fail": 0,
        }

        # 初始化数据库连接(FlightLog 上线日志 + FlightPlan 落库)
        db.init_db(config)

        # Load certificates: prefer MySQL, then supplement with cert.txt
        db_host = config.get("database", "host")
        db_name = config.get("database", "name")
        if db_host and db_name:
            db_config = {
                "host": db_host,
                "port": config.get("database", "port", "3306"),
                "name": db_name,
                "user": config.get("database", "user", "root"),
                "password": config.get("database", "password", ""),
            }
            count = self.certs.load_from_db(db_config)
            if count > 0:
                logger.info(f"Loaded {count} certificates from MySQL ({db_host}/{db_name})")
            else:
                logger.warning("MySQL returned 0 certificates")
        # Supplement with file-based certs (e.g. system bots)
        cert_file = config.cert_file
        extra = self.certs.load_file_extra(cert_file)
        if extra > 0:
            logger.info(f"Supplemented {extra} certificates from {cert_file}")

        # 保存 DB 配置供证书热重载使用
        self._db_cert_config = db_config if (db_host and db_name) else None

        # Ensure default admin exists
        if not self.certs.has("000000"):
            import secrets
            admin_pw = secrets.token_urlsafe(12)
            cert = Certificate("000000", admin_pw, LEV_ADMINISTRATOR)
            self.certs.add(cert)
            self.certs.save(config.cert_file)
            # 安全(2026-10-25 审计 P1-1): 管理密码不进日志/stdout, 写一次性文件(600)
            pw_file = os.path.join(os.path.dirname(config.cert_file) or '.', '.fsd-admin-000000')
            with open(pw_file, 'w') as f:
                f.write("CID=000000\npassword=%s\n" % admin_pw)
            os.chmod(pw_file, 0o600)
            logger.info("Created default admin CID=000000, password written to " + pw_file)
        else:
            logger.info("Admin certificate exists (CID=000000)")

    async def start(self):
        """Start all server interfaces."""
        self.running = True

        # Create asyncio servers
        self._client_server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self.config.client_port)
        server_bind = self.config.get("system", "server_bind", "127.0.0.1")
        self._server_server = await asyncio.start_server(
            self._handle_server, server_bind, self.config.server_port)
        # System management on localhost only
        self._system_server = await asyncio.start_server(
            self._handle_system, "127.0.0.1", self.config.system_port)

        logger.info(f"Client port={self.config.client_port}")
        logger.info(f"Server port={self.config.server_port}")
        logger.info(f"System port={self.config.system_port} (127.0.0.1 only)")

        # Start server links
        await self.server_links.start()

        # Start weather manager
        self.weather.start()

        # Start whazzup generator
        self.whazzup.start()

        # Start certificate hot-reload loop
        self._start_cert_reload()

        # 飞行计划补发任务（修复 2026-10-16：此前管理端口通知未实现 + 周期扫描被删除，
        # 导致官网提交的计划永远进不了 FSD）
        self._start_plan_dispatch()

        # 客户端保活探测 + 事件循环卡顿监控（修复 2026-10-16）
        self._start_keepalive()
        self._start_loop_monitor()

    def _start_keepalive(self):
        """服务端主动保活：静默客户端发 $PI 探测，并记录静默告警（不主动断开）。"""
        self._keepalive_running = True
        self._keepalive_task: Optional[asyncio.Task] = asyncio.create_task(self._keepalive_loop())
        logger.info(
            f"Client keepalive started (interval={KEEPALIVE_INTERVAL}s, "
            f"idle_probe={KEEPALIVE_IDLE}s, read_timeout={CLIENT_TIMEOUT}s)")

    async def _keepalive_loop(self):
        """周期探测静默客户端 + 周期广播 #DL 心跳。

        设计取舍：探测只用于保活与诊断，**不因未回 $PO 而踢人**（避免误伤不实现
        服务端探测的客户端）；真正的失活由 CLIENT_TIMEOUT 读超时判定。
        心跳(#DL 风速扰动)移植自 SimpleFSD HeartbeatSender：每 HEARTBEAT_INTERVAL 秒
        向所有客户端广播一次，模拟风变化并让链路保持活跃。
        """
        last_heartbeat = time.time()
        while self.running and self._keepalive_running:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                # 1) 心跳广播（默认 60s）
                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                    last_heartbeat = time.time()
                    await self.broadcast_heartbeat()
                # 2) 静默客户端探测
                for callsign, handler in list(self.clients.items()):
                    try:
                        idle = handler.idle_seconds()
                    except Exception:
                        continue
                    if idle < KEEPALIVE_IDLE:
                        continue
                    await handler.send_server_ping()
                    if idle >= KEEPALIVE_WARN:
                        logger.warning(
                            f"Client silent {int(idle)}s: {callsign} cid={handler.cid} "
                            f"peer={handler.peer[0] if isinstance(handler.peer, tuple) else ''} "
                            f"(keepalive $PI sent, missed={handler.ping_missed})")
            except Exception as e:
                logger.warning(f"Keepalive loop error: {e}")

    async def broadcast_heartbeat(self):
        """向所有在线客户端广播 #DL 心跳（风速扰动 ±5kt / ±10°）。

        移植自 SimpleFSD HeartbeatSender/CMSendHeartBeat：客户端据此更新风分量，
        同时也是服务端→客户端的链路活性探测。
        """
        clients = [h for h in list(self.clients.values()) if h.client]
        if not clients:
            return
        import random as _random
        d1 = _random.randint(-5, 5)
        d2 = _random.randint(-10, 10)
        name = self.config.get("system", "servername", "CWFLY FSD").replace(":", "_")
        line = f"#DL{name}:*:{d1}:{d2}\r\n"
        for h in clients:
            try:
                asyncio.ensure_future(h.send(line))
            except Exception:
                continue
        self.stats["heartbeats_sent"] = self.stats.get("heartbeats_sent", 0) + 1

    def _start_loop_monitor(self):
        """事件循环卡顿监控（此前只能靠每 60s 的证书热加载行反推卡顿）。"""
        self._loop_monitor_running = True
        self._loop_monitor_task: Optional[asyncio.Task] = asyncio.create_task(self._loop_monitor_loop())
        logger.info("Event loop lag monitor started (interval=1s, warn>500ms)")

    async def _loop_monitor_loop(self):
        """每秒测量事件循环延迟，超过 500ms 立即告警，每 5 分钟汇总一次峰值。"""
        interval = 1.0
        window_start = time.monotonic()
        worst = 0.0
        while self.running and self._loop_monitor_running:
            t0 = time.monotonic()
            await asyncio.sleep(interval)
            lag = time.monotonic() - t0 - interval
            if lag > worst:
                worst = lag
            if lag > 0.5:
                logger.warning(f"Event loop lag: {lag * 1000:.0f}ms "
                               f"(clients={len(self.clients)})")
            now = time.monotonic()
            if now - window_start >= 300:
                self.stats["loop_lag_ms_max"] = int(worst * 1000)
                if worst > 0.2:
                    logger.info(f"Event loop lag summary: max={worst * 1000:.0f}ms over "
                                f"{int(now - window_start)}s, clients={len(self.clients)}")
                worst = 0.0
                window_start = now

    def _start_plan_dispatch(self):
        """启动飞行计划补发任务。启动时调用。"""
        self._plan_dispatch_running = True
        self._plan_dispatch_task: Optional[asyncio.Task] = asyncio.create_task(
            self._plan_dispatch_loop())
        logger.info(f"Flight plan dispatcher started (interval={PLAN_DISPATCH_INTERVAL}s)")

    async def _plan_dispatch_loop(self):
        """周期把「已提交但未送达 FSD」的计划装载到在线机组，并广播给在线管制员。

        复用既有 db.get_unsynced_plans / mark_plan_synced：
          - 呼号在线且是机组 → 注入其内存 client.plan（whazzup/地图/雷达立即可见）
          - 同时把 $FP 广播给在线管制员（EuroScope 飞行条显示）
          - 处理完即置 syncFsd=1；离线机组的计划会在其登录时装载（见
            ClientHandler._load_filed_plan_on_login），因此不会丢失
        不改变任何客户端协议行为，只新增「对在线管制员多发一条 $FP」的效果。
        """
        while self.running and self._plan_dispatch_running:
            await asyncio.sleep(PLAN_DISPATCH_INTERVAL)
            try:
                loop = asyncio.get_running_loop()
                plans = await loop.run_in_executor(None, db.get_unsynced_plans)
                if not plans:
                    continue
                for p in plans:
                    callsign = str(p.get('callsign') or '').strip()
                    plan_id = p.get('id')
                    handler = self.clients.get(callsign) if callsign else None
                    if handler and handler.client and handler.client_type == CLIENT_PILOT:
                        if handler.apply_plan_row(p):
                            logger.info(
                                f"FP dispatched: {callsign} "
                                f"{p.get('depIcao')}->{p.get('arrIcao')}")
                            asyncio.ensure_future(handler.broadcast_plan_to_atc(callsign))
                    if plan_id:
                        await loop.run_in_executor(None, db.mark_plan_synced, plan_id)
            except Exception as e:
                logger.warning(f"Plan dispatch error: {e}")

    def _start_cert_reload(self):
        """Start the certificate hot-reload task. 启动时调用."""
        self._cert_reload_task: Optional[asyncio.Task] = None
        self._cert_reload_running = False
        if not self._db_cert_config:
            logger.info("No DB config, certificate hot-reload disabled")
            return
        self._cert_reload_running = True
        self._cert_reload_task = asyncio.create_task(self._cert_reload_loop())
        logger.info("Certificate hot-reload started (interval=60s)")

    async def _cert_reload_loop(self):
        """Certificate hot-reload loop. 每 60 秒从 MySQL 重载一次证书。

        修复(2026-10-16)：原先直接在本协程里同步调用 pymysql（且 connect 无 timeout），
        这是全服唯一一处「能冻住事件循环」的 MySQL 调用 —— 一旦 DB 卡住，所有客户端
        的收发都会停顿直到超时。改为线程池执行，并配合 certificate 侧的 connect/read
        timeout（见 certificate.py CertificateStore.reload_from_db）。
        """
        while self.running and self._cert_reload_running:
            await asyncio.sleep(60)
            try:
                loop = asyncio.get_running_loop()
                count = await loop.run_in_executor(
                    None, self.certs.reload_from_db, self._db_cert_config)
                if count >= 0:
                    logger.info(f"Certificates reloaded from MySQL, total={count}")
            except Exception as e:
                logger.error(f"Certificate reload error: {e}")

    async def stop(self):
        """Gracefully shut down."""
        self.running = False
        # 关闭前取消所有重连窗口看门狗并清空待恢复会话，
        # 避免进程退出时留下 "Task was destroyed but it is pending" 噪音
        for _pending in list(self.pending_sessions.values()):
            _task = _pending.get("task")
            if _task:
                _task.cancel()
        self.pending_sessions.clear()
        self._cert_reload_running = False
        if self._cert_reload_task:
            self._cert_reload_task.cancel()
        self._plan_dispatch_running = False
        if getattr(self, "_plan_dispatch_task", None):
            self._plan_dispatch_task.cancel()
        self._keepalive_running = False
        if getattr(self, "_keepalive_task", None):
            self._keepalive_task.cancel()
        self._loop_monitor_running = False
        if getattr(self, "_loop_monitor_task", None):
            self._loop_monitor_task.cancel()
        self.whazzup.stop()
        self.weather.stop()
        await self.server_links.stop()
        self._client_server.close()
        self._server_server.close()
        self._system_server.close()
        logger.info("Server stopped")

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Handle a new client connection (port 6809)."""
        handler = ClientHandler(self, reader, writer, is_server=False)
        self.stats["connections"] += 1
        await handler.run()

    async def _handle_server(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Handle a new server connection (port 3011)."""
        from server_link import ServerLink
        link = ServerLink(self, reader, writer)
        await self.server_links.add(link)

    async def _handle_system(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Handle a system management connection (port 3012)."""
        console = ManagementConsole(self, reader, writer)
        await console.run()

    def register_client(self, client: ClientHandler):
        """Register a client by callsign, send welcome info.

        ★ 会话重连（移植自 SimpleFSD session_clean_time）：
        若该呼号正处于重连窗口内且 CID 一致，则直接恢复原会话（保留飞行计划、ATIS、
        上线日志行与在线快照），不再广播上下线，避免闪断被记成一次掉线+一次上线。
        """
        pending = self.pending_sessions.get(client.callsign)
        if pending is not None:
            old_handler = pending.get("handler")
            old_client = pending.get("client")
            task = pending.get("task")
            same_cid = (old_handler is not None and
                        str(getattr(old_handler, "cid", "")) == str(client.cid))
            if same_cid and old_client is not None:
                if task:
                    task.cancel()
                self.pending_sessions.pop(client.callsign, None)
                # 恢复：把旧会话的数据对象交给新连接
                client.client = old_client
                client.client.callsign = client.callsign
                client.client_type = old_client.type
                client.cid = str(getattr(old_handler, "cid", client.cid))
                client.flightlog_id = getattr(old_client, "flightlog_id", 0)
                client.peer = getattr(client, "peer", None)
                self.clients[client.callsign] = client
                logger.info(
                    f"Session resumed: {client.callsign} cid={client.cid} "
                    f"(reason was {pending.get('reason')}, plan kept="
                    f"{bool(getattr(old_client, 'plan', None))})")
                self._send_client_list(client)
                return
            # CID 不一致（换人使用同呼号）：旧会话立即终结，按正常流程重新注册
            if task:
                task.cancel()
            self._finalize_pending_session(client.callsign)
        # 修复(2026-10-16)：同名呼号重复登录此前只是覆盖 dict 并 append，
        # 旧会话仍在运行，且旧会话退出时会把**新**会话从 dict 中移除（幽灵会话）。
        # 现在先礼貌关闭旧会话再注册新会话。
        old = self.clients.get(client.callsign)
        if old is not None and old is not client:
            logger.warning(
                f"Duplicate callsign login: {client.callsign} — closing previous session")
            try:
                asyncio.ensure_future(old.send_error(ERR_CSINUSE))
            except Exception:
                pass
            try:
                old._closed = True
                if old.writer and not old.writer.is_closing():
                    old.writer.close()
            except Exception:
                pass
        self.clients[client.callsign] = client
        self.client_list.append(client.client)
        # Broadcast ADDCLIENT to linked servers
        self._broadcast_addclient(client)
        logger.info(f"Client registered: {client.callsign} ({client.cid})")

        # Broadcast add to all other connected clients so they see new arrivals
        try:
            if client.client_type == CLIENT_ATC:
                add_line = (f"#AA{client.callsign}::{client.client.realname}:"
                            f"{client.cid}:{client.rating}:{client.client.protocol}")
            else:
                add_line = (f"#AP{client.callsign}:{client.client_type}:{client.cid}:"
                            f"{client.rating}:{client.client.protocol}:{client.client.simtype}:{client.client.realname}")
            for cs, h in self.clients.items():
                if cs != client.callsign:
                    asyncio.ensure_future(h.send(add_line + "\r\n"))
        except Exception:
            pass

        # Send welcome / server info to the newly connected client
        try:
            name = self.config.get("system", "servername", "CWFLY FSD")
            # MOTD / welcome message
            msg = (
                f"Welcome to {name}!"
                f"\n"
                f"\nCWFLY FSD Server"
                f"\n"
                f"\nOnline: {len(self.clients) - 1}"
            )
            asyncio.ensure_future(client.send_message("SERVER", client.callsign, msg))

            # Send existing clients to the new one (so EuroScope can show them)
            for cs, h in self.clients.items():
                if cs == client.callsign:
                    continue
                if h.client_type == CLIENT_ATC:
                    asyncio.ensure_future(client.send(
                        f"#AA{cs}::{h.client.realname}:{h.cid}:{h.rating}:{h.client.protocol}\r\n"
                    ))
                elif h.client_type == CLIENT_PILOT:
                    asyncio.ensure_future(client.send(
                        f"#AP{cs}:{h.client_type}:{h.cid}:{h.rating}:{h.client.protocol}:{h.client.simtype}:{h.client.realname}\r\n"
                    ))
                # 一并下发该客户端的 ATIS（若有），让 bot/其他客户端能显示
                if h.client_type == CLIENT_ATC and h.client.atis:
                    atis_data = h.client.atis.replace("\n", "^").replace("\r", "")
                    asyncio.ensure_future(
                        client.send(f"$AX{cs}:{client.callsign}:{atis_data}\r\n"))
        except Exception:
            pass

    # ================= 会话重连窗口（移植自 SimpleFSD） =================

    def mark_for_reconnect(self, handler: ClientHandler, reason: str) -> bool:
        """把异常断开的会话放入重连窗口（不立即注销）。

        返回 True = 已进入窗口（调用方不要执行下线清理）；
        返回 False = 该断开不可恢复（管理口 kill / 服务端停机 / 被顶替等），按普通下线处理。
        """
        callsign = getattr(handler, "callsign", "")
        client = getattr(handler, "client", None)
        if not callsign or client is None:
            return False
        # 服务端正在停机时不进重连窗口（停机导致的断线必须立即下线）
        if not getattr(self, "running", True):
            return False
        if reason not in RECONNECTABLE_REASONS:
            return False
        # 同呼号已有更新的在线会话（被顶替）→ 不进入窗口
        current = self.clients.get(callsign)
        if current is not None and current is not handler:
            return False
        # 已在窗口中（重复调用）→ 视为已处理
        if self.pending_sessions.get(callsign, {}).get("handler") is handler:
            return True
        # 从在线表摘除，但保留 client_list 与 pending，等重连或超时再正式注销
        self.clients.pop(callsign, None)
        task = asyncio.ensure_future(self._reconnect_watchdog(callsign))
        self.pending_sessions[callsign] = {
            "handler": handler,
            "client": client,
            "task": task,
            "reason": reason,
            "expires_at": time.time() + RECONNECT_GRACE,
        }
        logger.info(
            f"Session kept for reconnect: {callsign} cid={getattr(handler, 'cid', '')} "
            f"reason={reason} window={RECONNECT_GRACE}s "
            f"(plan kept={bool(getattr(client, 'plan', None))})")
        return True

    async def _reconnect_watchdog(self, callsign: str):
        """等待重连窗口超时；期间被重连则任务被 cancel。"""
        try:
            await asyncio.sleep(RECONNECT_GRACE)
        except asyncio.CancelledError:
            return
        self._finalize_pending_session(callsign)

    def _finalize_pending_session(self, callsign: str):
        """重连窗口超时/被顶替：真正终结会话（写下线时间、删快照、广播移除）。"""
        pending = self.pending_sessions.pop(callsign, None)
        if pending is None:
            return
        handler = pending.get("handler")
        task = pending.get("task")
        if task:
            task.cancel()
        if handler is None:
            return
        try:
            logger.info(f"Session expired after reconnect window: {callsign} "
                        f"cid={getattr(handler, 'cid', '')} reason={pending.get('reason')}")
            # 与 _disconnect 的下线清理保持一致（写库放线程池，避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            client = pending.get("client")
            if client is not None and getattr(client, "flightlog_id", 0):
                loop.run_in_executor(None, db.update_offline, client.flightlog_id)
            if client is not None:
                loop.run_in_executor(None, db.delete_online, callsign)
            # ★ 在线时长结算 + 计划解锁（会话真正终结时才结算）
            try:
                handler_cid = str(getattr(handler, "cid", "") or "")
                started = getattr(client, "start_time", None) if client is not None else None
                seconds = int(time.time() - started) if started else 0
                if client is not None and seconds > 0 and handler_cid and handler_cid != "0000":
                    loop.run_in_executor(
                        None, db.add_online_time, handler_cid,
                        getattr(client, "type", None) == CLIENT_ATC, seconds)
                    logger.info(f"Online time settled: {callsign} cid={handler_cid} {seconds}s")
            except Exception as e:
                logger.warning(f"Settle online time error: {e}")
            if callsign in self.locked_plans:
                self.locked_plans.discard(callsign)
                try:
                    loop.run_in_executor(None, db.set_plan_locked, callsign, False)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Finalize pending session cleanup error: {e}")
        try:
            self.unregister_client(handler)
        except Exception as e:
            logger.warning(f"Finalize pending session unregister error: {e}")

    def _send_client_list(self, client: ClientHandler):
        """把当前在线客户端列表下发给某个客户端（登录/会话恢复后调用）。"""
        try:
            for cs, h in self.clients.items():
                if cs == client.callsign:
                    continue
                if h.client_type == CLIENT_ATC:
                    asyncio.ensure_future(client.send(
                        f"#AA{cs}::{h.client.realname}:{h.cid}:{h.rating}:{h.client.protocol}\r\n"))
                elif h.client_type == CLIENT_PILOT:
                    asyncio.ensure_future(client.send(
                        f"#AP{cs}:{h.client_type}:{h.cid}:{h.rating}:{h.client.protocol}:"
                        f"{h.client.simtype}:{h.client.realname}\r\n"))
                if h.client_type == CLIENT_ATC and h.client.atis:
                    atis_data = h.client.atis.replace("\n", "^").replace("\r", "")
                    asyncio.ensure_future(
                        client.send(f"$AX{cs}:{client.callsign}:{atis_data}\r\n"))
        except Exception:
            pass

    def unregister_client(self, client: ClientHandler):
        """Unregister a client by callsign."""
        callsign = client.callsign
        # 修复(2026-10-16)：只有当 dict 里登记的还是本会话时才移除，
        # 否则同名呼号「旧会话退出」会把刚登录的新会话一起踢出注册表（幽灵会话）。
        if self.clients.get(callsign) is client:
            self.clients.pop(callsign, None)
        else:
            logger.info(f"Stale session unregister ignored: {callsign}")
        # 计划锁随下线回收(任何断开路径都会走到这里)
        self.locked_plans.discard(callsign)
        if client.client in self.client_list:
            self.client_list.remove(client.client)
        # Broadcast RMCLIENT to linked servers
        self._broadcast_rmclient(client)
        logger.info(f"Client unregistered: {callsign}")

        # Broadcast removal to all other clients
        if client.client_type == CLIENT_ATC:
            prefix = "#DA"
        else:
            prefix = "#DP"
        for cs, h in self.clients.items():
            if cs != callsign:
                asyncio.ensure_future(h.send(f"{prefix}{callsign}:{cs}\r\n"))

    def get_client(self, callsign: str) -> Optional[ClientHandler]:
        return self.clients.get(callsign)

    def get_client_by_cid(self, cid: str) -> Optional[ClientHandler]:
        for h in self.clients.values():
            if h.cid == cid:
                return h
        return None

    @property
    def client_count(self) -> int:
        return len(self.clients)

    @property
    def atc_clients(self) -> list[ClientHandler]:
        return [h for h in self.clients.values() if h.client_type == CLIENT_ATC]

    @property
    def pilot_clients(self) -> list[ClientHandler]:
        return [h for h in self.clients.values() if h.client_type == CLIENT_PILOT]

    def _broadcast_addclient(self, client: ClientHandler):
        """Notify linked servers about a new local client."""
        if not self.server_links.links:
            return
        c = client.client
        client_type = '2' if c.type == CLIENT_ATC else '1'
        # ADDCLIENT:<dest>:<src>:<flags>:<pc>:<hc>:<cid>:<server>:<callsign>:<type>:<rating>:<protocol>:<realname>:<simtype>
        data = (f"ADDCLIENT:*:{self.ident}:U0:0:0:"
                f"{c.cid}:{self.ident}:{c.callsign}:{client_type}:{c.rating}:{c.protocol}:{c.realname}:{c.simtype}")
        self.server_links.broadcast(data + "\r\n")

    def _broadcast_rmclient(self, client: ClientHandler):
        """Notify linked servers about a disconnected local client."""
        if not self.server_links.links:
            return
        data = f"RMCLIENT:*:{self.ident}:U0:0:0:{client.callsign}"
        self.server_links.broadcast(data + "\r\n")


import os  # noqa: E402 - needed for getpid in __init__
