"""Server-to-server linking protocol (port 3011).

Handles FSD server interconnection for distributed network operation.
"""

import asyncio
import logging
import time
from typing import Optional

from protocol import *
from client_handler import Client

logger = logging.getLogger("fsd.serverlink")


class RemoteClient:
    """Wrapper for a client connected to a linked server, mimics ClientHandler interface."""
    def __init__(self, client: Client):
        self.client = client


class ServerLink:
    """A connection to another FSD server."""

    def __init__(self, server, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.ident = ""
        self._buffer = b""
        self._closed = False

    async def run(self):
        """Handle server link protocol."""
        # 安全(2026-10-25 审计 P1-5): allowfrom 白名单(此前配置读了从未生效,
        # 仅靠默认绑 127.0.0.1 兜底; 一旦对外组网, 任意对端可注入证书/踢人)
        peer_ip = self.writer.get_extra_info("peername", ("", 0))[0]
        allowed = self.server.config.getlist("connections", "allowfrom") or []
        if allowed:
            import ipaddress
            try:
                if not any(ipaddress.ip_address(peer_ip) in ipaddress.ip_network(c, strict=False)
                           for c in allowed if c):
                    logger.warning(f"Server link rejected from {peer_ip} (not in allowfrom)")
                    self.writer.close()
                    return
            except ValueError:
                logger.warning(f"Server link allowfrom misconfigured, rejecting {peer_ip}")
                self.writer.close()
                return
        try:
            while not self._closed:
                data = await asyncio.wait_for(
                    self.reader.read(4096), timeout=SERVER_TIMEOUT)
                if not data:
                    break
                # 字节级缓冲：完整行再解码，避免中文跨分片乱码
                self._buffer += data
                # 安全(2026-10-25 审计 P1-2): 缓冲区上限, 防慢速攻击耗尽内存
                if len(self._buffer) > 65536 and b"\r\n" not in self._buffer:
                    break

                while b"\r\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\r\n", 1)
                    await self._process(
                        line.decode("utf-8", errors="replace").strip())
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            await self.disconnect()

    async def send(self, data: str):
        """Send data to linked server."""
        try:
            self.writer.write(data.encode())
            await self.writer.drain()
        except Exception:
            self._closed = True

    async def disconnect(self):
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    async def _process(self, line: str):
        """Process a server link protocol line."""
        if not line:
            return

        parts = line.split(":")
        if len(parts) < 6:
            return

        cmd_str, dest, source, flags, pc_str, hc_str = parts[:6]
        data = ":".join(parts[6:]) if len(parts) > 6 else ""

        try:
            cmd = CMD_NAMES.index(cmd_str)
        except ValueError:
            return

        # Track server identity from NOTIFY
        if cmd == CMD_NOTIFY and len(parts) >= 8:
            self.ident = parts[2] if len(parts) > 2 else ""

        # Route commands
        handlers = {
            CMD_NOTIFY: self._handle_notify,
            CMD_PING: self._handle_ping,
            CMD_PONG: self._handle_pong,
            CMD_SYNC: self._handle_sync,
            CMD_ADDCLIENT: self._handle_addclient,
            CMD_RMCLIENT: self._handle_rmclient,
            CMD_CERT: self._handle_cert,
            CMD_KILL: self._handle_kill,
            CMD_RESET: self._handle_reset,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler(parts, data)

    async def _handle_notify(self, parts: list, data: str):
        """Handle NOTIFY - server announcement."""
        if len(parts) >= 8:
            self.ident = parts[2]
            logger.info(f"Server {self.ident} announced")

    async def _handle_ping(self, parts: list, data: str):
        """Handle PING - respond with PONG."""
        if len(parts) >= 4:
            # Forward as PONG
            reply = f"PONG:{parts[2]}:{self.server.ident}:0:0:" + data
            await self.send(reply + "\r\n")

    async def _handle_pong(self, parts: list, data: str):
        """Handle PONG."""
        pass

    async def _handle_sync(self, parts: list, data: str):
        """Handle SYNC - state synchronization request."""
        if self.ident:
            self.server.server_links.send_reset()

    async def _handle_addclient(self, parts: list, data: str):
        """Handle ADDCLIENT - a client connected to another server.
        
        ADDCLIENT format (parsed from parts):
          parts[4] = clientID (CID)
          parts[5] = server (origin server ident)
          parts[6] = callsign
          parts[7] = type (1=PILOT, 2=ATC)
          parts[8] = rating
          parts[9] = protocol revision
          parts[10] = realname
          parts[11] = simtype (optional)
        """
        if len(parts) < 11:
            return
        callsign = parts[6]
        if not callsign or not parts[4]:
            return
        # Skip if already tracked locally
        if callsign in self.server.clients:
            return
        client_type = CLIENT_ATC if parts[7] == '2' else CLIENT_PILOT
        cid = parts[4]
        rating = int(parts[8]) if parts[8].isdigit() else 0
        realname = parts[10] if len(parts) > 10 else ''
        protocol = parts[9] if len(parts) > 9 else ''
        simtype = int(parts[11]) if len(parts) > 11 and parts[11].isdigit() else 0
        client = Client(cid, callsign, client_type, rating, realname, protocol, simtype)
        self.server.clients[callsign] = RemoteClient(client)
        self.server.client_list.append(client)
        logger.info(f"[Link] Remote client added: {callsign} ({cid}) type={'ATC' if client_type==CLIENT_ATC else 'PILOT'} server={parts[5]}")

    async def _handle_rmclient(self, parts: list, data: str):
        """Handle RMCLIENT - a client disconnected from another server."""
        if len(parts) < 7:
            return
        callsign = parts[6]
        if callsign and callsign in self.server.clients:
            handler = self.server.clients.pop(callsign, None)
            if handler and handler.client in self.server.client_list:
                self.server.client_list.remove(handler.client)
            logger.info(f"[Link] Remote client removed: {callsign}")

    async def _handle_cert(self, parts: list, data: str):
        """Handle ADDCERT - certificate synchronization.

        Format: cmd:cid:password_hash:level:creation:origin
        Password is already SHA-256 hashed for security.
        """
        if len(parts) >= 6:
            from certificate import Certificate
            cmd_type = int(parts[0]) if parts[0].isdigit() else CERT_ADD
            cid = parts[1]
            pwd_hash = parts[2]
            level = int(parts[3]) if parts[3].isdigit() else 0
            creation = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            origin = parts[5] if len(parts) > 5 else "remote"

            if cmd_type == CERT_DELETE:
                self.server.certs.remove(cid)
                logger.info(f"Cert removed (sync): {cid}")
            else:
                cert = Certificate(cid, pwd_hash, level, creation, origin)
                self.server.certs.add(cert)
                logger.info(f"Cert synced: {cid} level={level} from {origin}")

    async def _handle_kill(self, parts: list, data: str):
        """Handle KILL - disconnect a client remotely."""
        if len(parts) >= 2:
            callsign = parts[1]
            handler = self.server.get_client(callsign)
            if handler:
                handler._closed = True
                logger.info(f"Remote kill: {callsign}")

    async def _handle_reset(self, parts: list, data: str):
        """Handle RESET - reset packet counter."""
        pass


class ServerLinkManager:
    """Manages all server-to-server connections."""

    def __init__(self, server):
        self.server = server
        self.links: list[ServerLink] = []
        self.packet_count = 0

    async def start(self):
        """Connect to configured servers."""
        for target in self.server.config.connect_to:
            parts = target.split(":")
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 3011
            await self._connect_to(host, port)

    async def stop(self):
        for link in self.links:
            await link.disconnect()

    async def _connect_to(self, host: str, port: int):
        """Connect to an upstream FSD server."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10)
            link = ServerLink(self.server, reader, writer)
            self.links.append(link)
            asyncio.create_task(link.run())
            logger.info(f"Connected to server {host}:{port}")
        except Exception as e:
            logger.error(f"Failed to connect to {host}:{port}: {e}")

    async def add(self, link: ServerLink):
        """Add an incoming server link."""
        self.links.append(link)
        asyncio.create_task(link.run())

    def broadcast(self, data: str, exclude: Optional[ServerLink] = None):
        """Broadcast data to all linked servers."""
        for link in self.links:
            if link != exclude and not link._closed:
                asyncio.create_task(link.send(data))

    def send_reset(self):
        """Send RESET to all linked servers."""
        data = f"RESET:*:{self.server.ident}:U{self.packet_count}:0:\r\n"
        self.broadcast(data)

    def send_cert_sync(self, cert, cmd_type: int = CERT_ADD):
        """Sync a certificate to all linked servers.

        Sends the SHA-256 hash, not the plaintext password.
        """
        data = (f"ADDCERT:{cmd_type}:{cert.cid}:{cert.password_hash}:"
                f"{cert.level}:{cert.creation}:{cert.origin}")
        self.broadcast(data + "\r\n")
