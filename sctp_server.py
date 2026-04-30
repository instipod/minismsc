import asyncio
import logging
import socket

from sgsap.handler import handle_message
from sgsap.parser import parse_message

log = logging.getLogger(__name__)

IPPROTO_SCTP = 132


class _MmeTransport:
    """
    Write-side transport for one MME connection.
    Implements the write / is_closing / get_extra_info interface expected by
    handler.py and the SMS retry task, without depending on asyncio.Protocol.
    """

    def __init__(self, sock: socket.socket, ip: str, port: int) -> None:
        self._sock = sock
        self._ip = ip
        self._port = port
        self._closing = False

    def write(self, data: bytes) -> None:
        asyncio.ensure_future(self._async_write(data))

    async def _async_write(self, data: bytes) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.sock_sendall(self._sock, data)
        except OSError as exc:
            log.error("Send to MME %s failed: %s", self._ip, exc)
            self._closing = True

    def is_closing(self) -> bool:
        return self._closing or self._sock.fileno() == -1

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return (self._ip, self._port)
        return default


class SgsapServer:
    """
    SCTP accept-loop server.

    Uses loop.sock_accept() / loop.sock_recv() instead of asyncio.Protocol so
    that the raw SCTP socket is handled purely at the file-descriptor level.
    """

    def __init__(self, sock: socket.socket, app_state) -> None:
        self._sock = sock
        self._app_state = app_state
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._accept_loop())

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        try:
            self._sock.close()
        except OSError:
            pass

    async def wait_closed(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _accept_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                conn, addr = await loop.sock_accept(self._sock)
                conn.setblocking(False)
                asyncio.create_task(self._handle_connection(conn, addr))
            except asyncio.CancelledError:
                break
            except OSError as exc:
                log.error("Accept error: %s", exc)
                await asyncio.sleep(1)

    async def _handle_connection(self, conn: socket.socket, addr: tuple) -> None:
        mme_ip, mme_port = addr[0], addr[1]
        log.info("MME connected: %s:%d", mme_ip, mme_port)
        transport = _MmeTransport(conn, mme_ip, mme_port)
        await self._app_state.register_mme(mme_ip, mme_port, transport)
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.sock_recv(conn, 65536)
                if not data:
                    break
                asyncio.create_task(self._dispatch(bytes(data), mme_ip, transport))
        except OSError as exc:
            log.info("MME %s read error: %s", mme_ip, exc)
        finally:
            transport._closing = True
            conn.close()
            await self._app_state.deregister_mme(mme_ip)
            log.info("MME disconnected: %s", mme_ip)

    async def _dispatch(self, data: bytes, mme_ip: str, transport: _MmeTransport) -> None:
        try:
            msg = parse_message(data)
        except ValueError as exc:
            log.error(
                "Malformed SGsAP PDU from %s: %s — hex: %s", mme_ip, exc, data.hex()
            )
            return
        await handle_message(msg, mme_ip, transport, self._app_state)


def create_sctp_socket(host: str, port: int) -> socket.socket:
    """
    Create a bound, listening SCTP one-to-one socket.
    Requires Linux with the SCTP kernel module loaded (modprobe sctp).
    Raises OSError on Windows or if SCTP is unavailable.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, IPPROTO_SCTP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(16)
    sock.setblocking(False)
    return sock


async def start_sctp_server(
    loop: asyncio.AbstractEventLoop,
    app_state,
    host: str,
    port: int,
) -> SgsapServer:
    sock = create_sctp_socket(host, port)
    server = SgsapServer(sock, app_state)
    server.start()
    return server
