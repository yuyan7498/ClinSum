"""
Lightweight SSH local-port-forwarder.

Why: the remote Ollama (140.116.240.181:45001) is only reachable from inside
the tenant container, not from the open internet. We open an SSH connection
(port 45017) and forward localhost:<random> → container:45001.

Started once at Flask boot; reused for all LLM calls.
"""
from __future__ import annotations

import select
import socket
import threading
from typing import Optional

import paramiko


class _Handler(threading.Thread):
    def __init__(self, sock: socket.socket, transport: paramiko.Transport,
                 remote_host: str, remote_port: int):
        super().__init__(daemon=True)
        self.sock = sock
        self.transport = transport
        self.remote = (remote_host, remote_port)

    def run(self):
        try:
            chan = self.transport.open_channel(
                "direct-tcpip", self.remote, self.sock.getpeername()
            )
        except Exception:
            self.sock.close()
            return
        if chan is None:
            self.sock.close()
            return
        try:
            while True:
                r, _, _ = select.select([self.sock, chan], [], [])
                if self.sock in r:
                    data = self.sock.recv(8192)
                    if not data:
                        break
                    chan.send(data)
                if chan in r:
                    data = chan.recv(8192)
                    if not data:
                        break
                    self.sock.send(data)
        finally:
            try:
                chan.close()
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass


class SSHTunnel:
    """Forward 127.0.0.1:<local_port> → <ssh_host>:<remote_port> over SSH."""

    def __init__(self, ssh_host: str, ssh_port: int,
                 ssh_user: str, ssh_password: str,
                 remote_host: str, remote_port: int,
                 local_port: int = 0):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            ssh_host, port=ssh_port, username=ssh_user,
            password=ssh_password, timeout=15, banner_timeout=15,
        )
        self.transport: paramiko.Transport = self.client.get_transport()
        self.transport.set_keepalive(30)
        self.remote = (remote_host, remote_port)

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", local_port))
        self.server.listen(8)
        self.local_port: int = self.server.getsockname()[1]

        self._stop = threading.Event()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True
        )
        self._accept_thread.start()

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            _Handler(sock, self.transport, *self.remote).start()

    def close(self):
        self._stop.set()
        try:
            self.server.close()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass


_singleton: Optional[SSHTunnel] = None
_lock = threading.Lock()


def get_or_create_tunnel(ssh_host: str, ssh_port: int,
                         ssh_user: str, ssh_password: str,
                         remote_host: str, remote_port: int) -> SSHTunnel:
    """Process-wide tunnel singleton. Re-creates on transport failure."""
    global _singleton
    with _lock:
        if _singleton is not None and _singleton.transport.is_active():
            return _singleton
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
        _singleton = SSHTunnel(
            ssh_host, ssh_port, ssh_user, ssh_password,
            remote_host, remote_port,
        )
        return _singleton
