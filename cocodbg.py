import socket
import threading
import queue
import traceback
import contextlib
import sys
import os
import time
import subprocess
from typing import Tuple
import ast

# =======================
# Server & helper classes
# =======================

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

    def start(self):
        if self._srv_thread and self._srv_thread.is_alive():
            return
        self._srv_thread = threading.Thread(target=self._serve, daemon=True)
        self._srv_thread.start()

    def stop(self):
        self._stop_evt.set()

    def _serve(self):
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(5)
            while not self._stop_evt.is_set():
                try:
                    s.settimeout(0.5)
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()

    def _handle_client(self, conn: socket.socket, addr):
        conn_file = conn.makefile(mode="rwb", buffering=0)
        try:
            # Single short banner to avoid client/server desync
            try:
                self._send_line(conn_file, b"# cocotb-dbg ready. Single-line mode: press Enter to run. Use :paste / :end for blocks. :quit to exit.")
            except Exception:
                # If the peer closed immediately, just stop handling.
                return

            buf: list[bytes] = []
            while not self._stop_evt.is_set():
                line = conn_file.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if line == b":quit":
                    break
                if line == b"":  # blank line => submit accumulated buffer
                    code = b"\n".join(buf).decode("utf-8", "replace")
                    buf.clear()
                    reply_q: "queue.Queue[str]" = queue.Queue()
                    self.cmd_q.put((code, reply_q, addr))
                    reply = reply_q.get()  # wait for executor to respond

                    # frame the reply
                    try:
                        self._send_line(conn_file, b"<<BEGIN>>")
                        for ln in reply.splitlines():
                            self._send_line(conn_file, ln.encode("utf-8", "replace"))
                        self._send_line(conn_file, b"<<END>>")
                    except Exception:
                        break  # peer likely closed; exit handler
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
            # Let caller decide whether to continue/exit; avoid noisy tracebacks.
            raise


def wrap_code_as_async(code: str) -> str:
    """
    Wrap arbitrary user code into an async function __user_main__().
    - If the last statement is an expression (incl. `await ...`), return it so the REPL shows its value.
    - Empty lines become 'pass' to keep indentation valid.
    """
    try:
        tree = ast.parse(code, mode="exec")
        if tree.body:
            last = tree.body[-1]
            if isinstance(last, ast.Expr) and hasattr(last, "lineno"):
                lines = code.splitlines()
                start = last.lineno - 1
                end = getattr(last, "end_lineno", last.lineno) - 1
                expr_src = ast.get_source_segment(code, last.value)
                if not expr_src:
                    expr_src = "\n".join(lines[start:end + 1]).strip()
                kept = lines[:start] + lines[end + 1:]
                indented = [("    " + ln) if ln.strip() else "    pass" for ln in kept]
                indented.append(f"    return ({expr_src})")
                body = "\n".join(indented) or "    pass"
                return f"async def __user_main__():\n{body}\n"
    except Exception:
        pass

    indented = [("    " + ln) if ln.strip() else "    pass" for ln in code.splitlines()]
    body = "\n".join(indented) or "    pass"
    return f"async def __user_main__():\n{body}\n"


def format_exception() -> str:
    return "".join(traceback.format_exception(*sys.exc_info()))

# =======================
# Launcher / Client (CLI)
# =======================

BANNER = "[cocotb-dbg] listening on"
HOST = "127.0.0.1"
PORT = 9999

def _tail_proc(proc: subprocess.Popen):
    """
    Continuously forward the simulation's stdout/stderr to our console.
    """
    def pump(stream, prefix=""):
        for raw in iter(stream.readline, b""):
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:
                line = str(raw)
            print(line)
    t1 = threading.Thread(target=pump, args=(proc.stdout, ""), daemon=True)
    t2 = threading.Thread(target=pump, args=(proc.stderr, ""), daemon=True)
    t1.start(); t2.start()
    return (t1, t2)

def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """
    Poll a TCP port until it accepts connections.
    """
    end = time.time() + timeout
    while time.time() < end:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False

def _client_loop():
    print(f"Connecting to {HOST}:{PORT} ...")
    with socket.create_connection((HOST, PORT)) as s, s.makefile("rwb", buffering=0) as f:
        # read single-line banner from server
        banner = f.readline().decode().rstrip()
        print(banner)
        print("# Single-line mode: each Enter executes. Use :paste for multi-line, end with :end. :quit to exit.") # TODO: auto detect multi line with ':' at the end of line (ex if (dut.a.value < 5:))
        while True:
            try:
                line = input(">>> ")
            except EOFError:
                line = ":quit"

            if line.strip() == ":quit":
                f.write(b":quit\n")
                break

            if line.strip() == ":paste":
                print("# Paste your code. Finish with a line containing only :end")
                buf = []
                while True:
                    try:
                        pl = input("... ")
                    except EOFError:
                        pl = ":end"
                    if pl.strip() == ":end":
                        break
                    buf.append(pl)
                # send the multi-line block followed by a blank line
                for ln in buf:
                    f.write(ln.encode() + b"\n")
                f.write(b"\n")
            else:
                # immediate single-line execution: send line + blank line
                f.write(line.encode() + b"\n")
                f.write(b"\n")

            # read framed reply
            start = f.readline().decode().rstrip()
            if start != "<<BEGIN>>":
                print("# Protocol error (no BEGIN)")
                continue
            payload_lines = []
            while True:
                ln = f.readline().decode().rstrip()
                if ln == "<<END>>":
                    break
                payload_lines.append(ln)
            print("\n".join(payload_lines))

def start_sim(sim_dir):
    """
    Start the simulation and connect to the REPL server.
    """
    env = os.environ.copy()
    extra_py = os.path.abspath(".") + os.pathsep + os.path.abspath(sim_dir)
    env["PYTHONPATH"] = extra_py + os.pathsep + env.get("PYTHONPATH", "")

    print("starting simulation...")
    sim_proc = subprocess.Popen(
        ["make", "-C", sim_dir, "sim"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    _tail_proc(sim_proc)

    print(f"waiting for {HOST}:{PORT} ...")
    if not _wait_for_port(HOST, PORT, timeout=60.0):
        print("Error: debugger port did not open in time.")
        with contextlib.suppress(Exception):
            sim_proc.terminate()
        sys.exit(1)

    print("connected.\n")
    try:
        _client_loop()
    finally:
        print("shutting down simulation ...")
        with contextlib.suppress(Exception):
            sim_proc.terminate()
        try:
            sim_proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                sim_proc.kill()

if __name__ == "__main__":
    start_sim("tests")
