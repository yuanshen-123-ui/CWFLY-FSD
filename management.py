"""System management console (port 3012, bound to 127.0.0.1 only).

Provides telnet-style admin interface with password protection,
rate limiting, and constant-time password comparison.
"""

import asyncio
import logging
import time
from typing import Optional

from protocol import PRODUCT, VERSION, MAXLOG, CERT_LEVEL_NAMES

logger = logging.getLogger("fsd.mgmt")


class ManagementConsole:
    """Handles a single management console connection."""

    def __init__(self, server, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.authorized = False
        self.fail_attempts = 0
        self.last_fail = 0
        self._buffer = b""
        self._closed = False

    async def run(self):
        """Main console handler."""
        try:
            await self._write(f"# {PRODUCT} system interface ready.\r\n")
            await self._write(f"# Type 'help' for help.\r\n")

            while not self._closed:
                await self._write("\r\n> ")
                line = await self._read_line(timeout=300)
                if line is None:
                    break

                line = line.strip()
                if not line:
                    continue

                parts = line.split()
                cmd = parts[0].lower()

                handlers = {
                    "help": self._cmd_help,
                    "quit": self._cmd_quit,
                    "exit": self._cmd_quit,
                    "pwd": self._cmd_pwd,
                    "stats": self._cmd_stats,
                    "clients": self._cmd_clients,
                    "servers": self._cmd_servers,
                    "say": self._cmd_say,
                    "wall": self._cmd_wall,
                    "kill": self._cmd_kill,
                    "cert": self._cmd_cert,
                    "log": self._cmd_log,
                    "info": self._cmd_info,
                    "time": self._cmd_time,
                }

                handler = handlers.get(cmd)
                if handler:
                    await handler(parts[1:])
                else:
                    await self._write(f"Unknown command: {cmd}\r\n")

        except Exception as e:
            logger.error(f"Console error: {e}")
        finally:
            self._closed = True
            try:
                self.writer.close()
            except Exception:
                pass

    async def _write(self, text: str):
        try:
            self.writer.write(text.encode())
            await self.writer.drain()
        except Exception:
            self._closed = True

    async def _read_line(self, timeout: int = 300) -> Optional[str]:
        try:
            while b"\n" not in self._buffer:
                data = await asyncio.wait_for(
                    self.reader.read(1024), timeout=timeout)
                if not data:
                    return None
                # 字节级缓冲：完整行再解码，避免中文跨分片乱码
                self._buffer += data
                # 安全(2026-10-25 审计 P1-2): 缓冲区上限
                if len(self._buffer) > 65536 and b"\n" not in self._buffer:
                    return None
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line.strip(b"\r").decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    def _const_time_cmp(self, a: str, b: str) -> bool:
        """Constant-time string comparison."""
        if len(a) != len(b):
            return False
        result = 0
        for ca, cb in zip(a, b):
            result |= ord(ca) ^ ord(cb)
        return result == 0

    # ----- Commands -----

    async def _cmd_help(self, args: list):
        """Show available commands."""
        cmds = [
            ("help", "Show this help"),
            ("pwd <password>", "Authenticate with system password"),
            ("stats", "Show server statistics"),
            ("clients", "List connected clients"),
            ("servers", "List linked servers"),
            ("say <user> <msg>", "Send message to a user"),
            ("wall <msg>", "Broadcast message to all users"),
            ("kill <callsign>", "Disconnect a client"),
            ("cert <list|add|del>", "Manage certificates"),
            ("log [level]", "Show recent log entries"),
            ("info", "Show server information"),
            ("time", "Show server time"),
            ("quit", "Disconnect"),
        ]
        for cmd, desc in cmds:
            await self._write(f"  {cmd:<25} {desc}\r\n")

    async def _cmd_quit(self, args: list):
        self._closed = True

    async def _cmd_pwd(self, args: list):
        """Authenticate with password. Rate-limited."""
        if not args:
            await self._write("Usage: pwd <password>\r\n")
            return

        # Rate limiting: 5 failures per 30 seconds
        now = time.time()
        if self.fail_attempts >= 5 and (now - self.last_fail) < 30:
            wait = int(30 - (now - self.last_fail))
            await self._write(f"Too many attempts. Wait {wait}s.\r\n")
            return
        if self.fail_attempts >= 5:
            self.fail_attempts = 0

        stored = self.server.config.system_password
        if not stored:
            await self._write("System password not configured.\r\n")
            return

        if self._const_time_cmp(stored, args[0]):
            self.authorized = True
            self.fail_attempts = 0
            await self._write("Password correct.\r\n")
        else:
            self.fail_attempts += 1
            self.last_fail = now
            await self._write("Password incorrect.\r\n")

    async def _require_auth(self) -> bool:
        """Check if user is authorized."""
        if not self.authorized:
            await self._write("Not authorized. Use 'pwd <password>' first.\r\n")
            return False
        return True

    async def _cmd_stats(self, args: list):
        if not await self._require_auth():
            return
        s = self.server.stats
        await self._write(
            f"  Connections: {s['connections']}\r\n"
            f"  Clients: {self.server.client_count}\r\n"
            f"  Servers: {len(self.server.server_links.links)}\r\n"
            f"  Certs: {len(self.server.certs)}\r\n"
            f"  Packets received: {s['packets_received']}\r\n"
            f"  Packets sent: {s['packets_sent']}\r\n"
            f"  Auth success: {s['auth_success']}\r\n"
            f"  Auth fail: {s['auth_fail']}\r\n"
            f"  Loop lag max (5min): {s.get('loop_lag_ms_max', 0)}ms\r\n"
            f"  Plan dispatch: {'on' if getattr(self.server, '_plan_dispatch_running', False) else 'off'}\r\n"
            f"  Keepalive: {'on' if getattr(self.server, '_keepalive_running', False) else 'off'}\r\n"
        )

    async def _cmd_clients(self, args: list):
        if not await self._require_auth():
            return
        if not self.server.clients:
            await self._write("  No clients connected.\r\n")
            return
        for cs, handler in sorted(self.server.clients.items()):
            c = handler.client
            if c:
                ctype = "ATC" if c.type == 2 else "PILOT"
                # 附带静默时长：排查「闪断/假在线」时一眼可见
                try:
                    idle = int(handler.idle_seconds())
                except Exception:
                    idle = -1
                await self._write(
                    f"  {cs:<12} {c.cid:<6} {ctype:<5} "
                    f"rating={c.rating} idle={idle}s\r\n")

    async def _cmd_servers(self, args: list):
        if not await self._require_auth():
            return
        if not self.server.server_links.links:
            await self._write("  No servers linked.\r\n")
            return
        for link in self.server.server_links.links:
            ident = link.ident or "unknown"
            await self._write(f"  {ident}\r\n")

    async def _cmd_say(self, args: list):
        if not await self._require_auth() or len(args) < 2:
            return
        target_cs = args[0]
        message = " ".join(args[1:])
        handler = self.server.get_client(target_cs)
        if handler:
            await handler.send_message("server", target_cs, message)
            await self._write(f"Sent to {target_cs}.\r\n")
        else:
            await self._write(f"Unknown client: {target_cs}\r\n")

    async def _cmd_wall(self, args: list):
        if not await self._require_auth() or not args:
            return
        message = " ".join(args)
        for handler in self.server.clients.values():
            await handler.send_message("server", handler.callsign, message)
        await self._write(f"Broadcast to {len(self.server.clients)} users.\r\n")

    async def _cmd_kill(self, args: list):
        if not await self._require_auth() or not args:
            return
        target_cs = args[0]
        handler = self.server.get_client(target_cs)
        if handler:
            # 记录原因：管理台踢人也必须可追溯（此前静默，无法与网络掉线区分）
            try:
                handler._close_reason = "management_kill"
            except Exception:
                pass
            logger.warning(f"Management console killed client: {target_cs}")
            handler._closed = True
            await self._write(f"Killed {target_cs}.\r\n")
        else:
            await self._write(f"Unknown client: {target_cs}\r\n")

    async def _cmd_cert(self, args: list):
        if not await self._require_auth():
            return
        if not args:
            await self._write("Usage: cert list|add <cid> <pwd> <level>|del <cid>\r\n")
            return

        sub = args[0].lower()
        if sub == "list":
            for cert in self.server.certs.all_certs:
                level_name = CERT_LEVEL_NAMES[cert.level] if cert.level < len(CERT_LEVEL_NAMES) else str(cert.level)
                await self._write(f"  {cert.cid:<6} {level_name:<15} from {cert.origin}\r\n")

        elif sub == "add" and len(args) >= 4:
            from certificate import Certificate
            from protocol import LEV_ADMINISTRATOR
            cid, pwd, level = args[1], args[2], int(args[3])
            # 安全(2026-10-25 审计 P2-4): level 合法范围
            if not (0 <= level <= LEV_ADMINISTRATOR):
                await self._write("Invalid level\r\n")
                return
            cert = Certificate(cid, pwd, level)
            self.server.certs.add(cert)
            self.server.certs.save(self.server.config.cert_file)
            await self._write(f"Certificate added: {cid}\r\n")

        elif sub == "del" and len(args) >= 2:
            cid = args[1]
            self.server.certs.remove(cid)
            self.server.certs.save(self.server.config.cert_file)
            await self._write(f"Certificate removed: {cid}\r\n")

    async def _cmd_log(self, args: list):
        if not await self._require_auth():
            return
        await self._write("  (Log viewing not implemented in this version)\r\n")

    async def _cmd_info(self, args: list):
        if not await self._require_auth():
            return
        await self._write(
            f"  Server: {self.server.ident}\r\n"
            f"  Name: {self.server.name}\r\n"
            f"  Host: {self.server.hostname}\r\n"
            f"  Location: {self.server.location}\r\n"
            f"  Version: {VERSION}\r\n"
            f"  Uptime: {int(time.time() - self.server.start_time)}s\r\n"
        )

    async def _cmd_time(self, args: list):
        if not await self._require_auth():
            return
        await self._write(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\r\n")
