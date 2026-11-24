import socket
import threading
import queue
import contextlib
import select

from ._framing import escape_line
from typing import Tuple

Command = Tuple[str, "queue.Queue[str]", Tuple[str, int]]


class CocotbDebugger:
    """
    Line-oriented TCP server that accepts Python snippets.
    Snippets are executed inside cocotb via an async executor coroutine.
    Results / exceptions are sent back to the client. Simulation never dies.
    Replies are framed between <<BEGIN>> and <<END>>.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self._srv_thread = None
        self._stop_evt = threading.Event()
        self.cmd_q: "queue.Queue[Command]" = queue.Queue()
        self.cancel_evt = threading.Event()

    def start(self):
        if self._srv_thread and self._srv_thread.is_alive():
            return
        self._srv_thread = threading.Thread(target=self._serve, daemon=True)
        self._srv_thread.start()

    def stop(self):
        self._stop_evt.set()

    def _serve(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(5)
            s.settimeout(0.5)
            while not self._stop_evt.is_set():
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
        finally:
            with contextlib.suppress(Exception):
                s.close()

    def _handle_client(self, conn: socket.socket, addr):
        conn_file = conn.makefile(mode="rwb", buffering=0)
        try:
            try:
                self._send_line(conn_file, b"# cocotb-dbg ready. :quit to exit.")
            except Exception:
                return

            buf: list[bytes] = []
            while not self._stop_evt.is_set():
                line = conn_file.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if line == b":quit":
                    break
                if line == b":cancel":
                    self.cancel_evt.set()
                    with contextlib.suppress(Exception):
                        self._send_line(conn_file, b"# cancel requested")
                    continue

                if line == b"":
                    code = b"\n".join(buf).decode("utf-8", "replace")
                    buf.clear()
                    reply_q: "queue.Queue[str]" = queue.Queue()
                    self.cmd_q.put((code, reply_q, addr))
                    reply = None
                    while reply is None:
                        try:
                            reply = reply_q.get(timeout=0.05)
                        except queue.Empty:
                            with contextlib.suppress(Exception):
                                r, _, _ = select.select([conn], [], [], 0)
                                if r:
                                    chunk = conn.recv(4096, socket.MSG_PEEK)
                                    if not chunk or b":cancel" in chunk:
                                        if chunk and b":cancel" in chunk:
                                            conn_file.readline()
                                            with contextlib.suppress(Exception):
                                                self._send_line(conn_file, b"# cancel requested")
                                        self.cancel_evt.set()
                    if reply is None:
                        break

                    try:
                        self._send_line(conn_file, b"<<BEGIN>>")
                        for ln in reply.splitlines():
                            self._send_line(conn_file, escape_line(ln).encode("utf-8", "replace"))
                        self._send_line(conn_file, b"<<END>>")
                    except Exception:
                        break
                else:
                    buf.append(line)
        finally:
            with contextlib.suppress(Exception):
                conn_file.close()
                conn.close()

    @staticmethod
    def _send_line(f, data: bytes):
        try:
            f.write(data if data.endswith(b"\n") else data + b"\n")
        except Exception:
            raise
