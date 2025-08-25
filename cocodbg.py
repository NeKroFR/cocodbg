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
import readline
import signal
import re


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
            # Single short banner to avoid client/server desync
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

def _history_init():
    """
    Enable in-memory arrow-key history to navigate through snippets.
    """
    readline.set_auto_history(False) # Fix pressing arrow twice issue
    readline.clear_history()

def _history_add_line(line: str):
    """
    Add a line to in-memory history unless it would duplicate the last item.

    NOTE: We don't append commands yet (lines starting with ":")
    """
    if not line.strip():
        return
    if line.lstrip().startswith(":"):
        return
    n = readline.get_current_history_length()
    last = readline.get_history_item(n) if n > 0 else None
    if last != line:
        readline.add_history(line)

#### Helpers for persistent variables via AST ####

# Names that already exist in the REPL environment
# (avoid forcing these through 'global' unless the user explicitly rebinds them).
_RESERVED_GLOBALS = {
    "dut", "clk", "cocotb", "Timer", "RisingEdge", "FallingEdge",
    "Clock", "BinaryValue",
    "start_clock", "stop_clock", "set_clock_rate", "step", "run_for", "cycles",
}

def _collect_store_names(node: ast.AST) -> set[str]:
    """
    Collect Name ids in Store context from an arbitrary target node.
    """
    names: set[str] = set()
    class StoreVisitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name):
            if isinstance(n.ctx, ast.Store):
                names.add(n.id)
        def visit_Attribute(self, n: ast.Attribute):
            # Attribute targets (obj.attr) don't introduce new names in scope
            self.generic_visit(n.value)
        def visit_List(self, n: ast.List):
            for elt in n.elts:
                self.visit(elt)
        def visit_Tuple(self, n: ast.Tuple):
            for elt in n.elts:
                self.visit(elt)
    StoreVisitor().visit(node)
    return names

def _assigned_at_toplevel(tree: ast.Module) -> set[str]:
    """
    Return names that are assigned/bound at the user's top level (not inside
    nested function/class/lambda scopes).
    Includes: Assign/AugAssign/AnnAssign targets, For/AsyncFor targets,
    With/AsyncWith 'as' vars, Try except aliases, NamedExpr (walrus),
    Import/ImportFrom aliases, and names of def/class statements.
    """
    assigned: set[str] = set()

    def visit_stmt(node: ast.stmt, nested_scope: bool):
        # New scope creators (don't traverse into body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not nested_scope:
                assigned.add(node.name)
            return
        if isinstance(node, ast.ClassDef):
            if not nested_scope:
                assigned.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return

        # Imports
        if not nested_scope and isinstance(node, ast.Import):
            for alias in node.names:
                assigned.add(alias.asname or alias.name.split(".", 1)[0])
        if not nested_scope and isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assigned.add(alias.asname or alias.name)

        # Assignments
        if not nested_scope and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for t in targets:
                assigned.update(_collect_store_names(t))

        # For/AsyncFor targets
        if not nested_scope and isinstance(node, (ast.For, ast.AsyncFor)):
            assigned.update(_collect_store_names(node.target))

        # With/AsyncWith 'as' vars
        if not nested_scope and isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    assigned.update(_collect_store_names(item.optional_vars))

        # Try/Except alias "as e"
        if not nested_scope and isinstance(node, ast.Try):
            for h in node.handlers:
                if h.name:
                    assigned.add(h.name)

        # Walrus operator at top level
        if not nested_scope:
            class WalrusVisitor(ast.NodeVisitor):
                def visit_NamedExpr(self, n: ast.NamedExpr):
                    assigned.update(_collect_store_names(n.target))
                    self.generic_visit(n.value)
            WalrusVisitor().visit(node)

        # Recurse into non-scope child statements
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.stmt):
                        visit_stmt(child, nested_scope)
            elif isinstance(value, ast.stmt):
                visit_stmt(value, nested_scope)

    for top in tree.body:
        visit_stmt(top, nested_scope=False)

    return {n for n in assigned if n not in _RESERVED_GLOBALS}

def wrap_code_as_async(code: str) -> str:
    """
    Wrap arbitrary user code into an async function __user_main__().
    - If the last statement is an expression (incl. `await ...`), return it so the REPL shows its value.
    - Empty lines become 'pass' to keep indentation valid.
    - Names assigned at the user's top level are declared 'global' so values persist.
    """
    globals_line = ""
    try:
        tree = ast.parse(code, mode="exec")
        assigned = sorted(_assigned_at_toplevel(tree))
        if assigned:
            globals_line = "    global " + ", ".join(assigned) + "\n"
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
                body = globals_line + ("\n".join(indented) + ("\n" if indented else "")) + f"    return ({expr_src})"
                return f"async def __user_main__():\n{body}\n"

    except Exception:
        pass

    indented = [("    " + ln) if ln.strip() else "    pass" for ln in code.splitlines()]
    body_core = "\n".join(indented) or "    pass"
    body = (globals_line + body_core) if globals_line else body_core
    return f"async def __user_main__():\n{body}\n"


def format_exception() -> str:
    return "".join(traceback.format_exception(*sys.exc_info()))

# =======================
# Launcher / Client (CLI)
# =======================

HOST = "127.0.0.1"
PORT = 9999

# paths / pidfile helpers
def _state_dir() -> str:
    d = os.path.abspath("./.cocodbg")
    os.makedirs(d, exist_ok=True)
    return d

def _pidfile_path() -> str:
    return os.path.join(_state_dir(), "sim.pid")

def _write_pidfile(pid: int):
    try:
        with open(_pidfile_path(), "w") as f:
            f.write(str(pid))
    except Exception:
        pass

def _read_pidfile() -> int | None:
    try:
        with open(_pidfile_path(), "r") as f:
            s = f.read().strip()
        return int(s) if s else None
    except Exception:
        return None

def _remove_pidfile():
    with contextlib.suppress(Exception):
        os.remove(_pidfile_path())

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

def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0

def _wait_for_port_open(host: str, port: int, timeout: float = 30.0) -> bool:
    """
    Poll a TCP port until it accepts connections.
    """
    end = time.time() + timeout
    while time.time() < end:
        if _can_connect(host, port):
            return True
        time.sleep(0.25)
    return False

def _wait_for_port_closed(host: str, port: int, timeout: float = 10.0) -> bool:
    """
    Poll a TCP port until it is closed (no listener).
    """
    end = time.time() + timeout
    while time.time() < end:
        if not _can_connect(host, port):
            return True
        time.sleep(0.2)
    return not _can_connect(host, port)

def _open_connection(host: str, port: int):
    """
    Open a TCP connection and return (socket, filelike). Reads and returns the banner.
    """
    s = socket.create_connection((host, port))
    f = s.makefile("rwb", buffering=0)
    banner = f.readline().decode().rstrip()
    return s, f, banner

def _kill_process_group_by_pid(pid: int):
    """
    Try to terminate/kill an entire process group given its leader PID.
    """
    if pid <= 0:
        return
    # TERM then KILL
    with contextlib.suppress(Exception):
        os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGTERM)
    time.sleep(0.3) # little delay (should be enough)
    with contextlib.suppress(Exception):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGKILL)

def _preflight_cleanup():
    """
    Before launching a new sim, try to clean up any leftovers.
    It kills by PID from pidfile (group-aware) and then waits for TCP port to be fully closed.
    """
    pid = _read_pidfile()
    if pid:
        _kill_process_group_by_pid(pid)
        _remove_pidfile()
    _wait_for_port_closed(HOST, PORT, timeout=5.0)

def _launch_sim(sim_dir: str) -> tuple[subprocess.Popen, tuple[threading.Thread, threading.Thread]]:
    """
    Launch the HDL simulation via make and start tailing its output.
    """
    env = os.environ.copy()
    extra_py = os.path.abspath(".") + os.pathsep + os.path.abspath(sim_dir)
    env["PYTHONPATH"] = extra_py + os.pathsep + env.get("PYTHONPATH", "")
    env["COCODBG_ROOT"] = os.path.abspath(os.getcwd())

    print("starting simulation...")
    sim_proc = subprocess.Popen(
        ["make", "-C", sim_dir, "sim"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,  # POSIX: child is new process group leader
    )
    _write_pidfile(sim_proc.pid)
    tails = _tail_proc(sim_proc)
    return sim_proc, tails

def _terminate_sim(sim_proc: subprocess.Popen):
    """
    Terminate the simulation process safely and wait for port to close.
    Kills the entire process group to avoid straggler 'make', 'ghdl', etc.
    """
    print("shutting down simulation ...")
    try:
        if sim_proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(sim_proc.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                sim_proc.terminate()
            try:
                sim_proc.wait(timeout=5)
            except Exception:
                # Force kill
                with contextlib.suppress(Exception):
                    os.killpg(sim_proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    sim_proc.kill()
                with contextlib.suppress(Exception):
                    sim_proc.wait(timeout=3)
    finally:
        _wait_for_port_closed(HOST, PORT, timeout=10.0)
        _remove_pidfile()

def _client_loop(sim_dir: str):
    """
    Manage the whole interactive session, including restarts and step_back().
    """
    submissions: list[str] = []   # snippets that were actually sent to the server
    last_connect_info = None      # (socket, file, banner)
    sim_proc: subprocess.Popen | None = None

    def _connect_new_sim():
        nonlocal sim_proc, last_connect_info
        _preflight_cleanup()

        sim_proc, _ = _launch_sim(sim_dir)

        print(f"waiting for {HOST}:{PORT} ...")
        if not _wait_for_port_open(HOST, PORT, timeout=60.0):
            print("Error: debugger port did not open in time.")
            if sim_proc is not None:
                _terminate_sim(sim_proc)
            sys.exit(1)

        s, f, banner = _open_connection(HOST, PORT)
        last_connect_info = (s, f, banner)
        print(banner)
        print("# Auto-detect blocks: lines ending with ':' start a block; finish a block with a blank line. :quit to exit.")

    def _close_conn_only():
        nonlocal last_connect_info
        if last_connect_info is None:
            return
        s, f, _ = last_connect_info
        with contextlib.suppress(Exception):
            f.write(b":quit\n")
        with contextlib.suppress(Exception):
            f.close()
        with contextlib.suppress(Exception):
            s.close()
        last_connect_info = None

    def _restart_and_reconnect():
        """
        Stop current sim (if any), ensure port closed, start fresh, and connect.
        """
        nonlocal sim_proc
        if sim_proc is not None:
            _terminate_sim(sim_proc)
            sim_proc = None
        # tiny backoff; some platforms release the port on next scheduler tick
        time.sleep(0.05)
        _connect_new_sim()

    def _send_code_and_read_reply(code: str, *, print_reply: bool = True) -> str:
        """
        Send a snippet and read framed reply. Returns the raw payload (no BEGIN/END).
        """
        assert last_connect_info is not None
        _, f, _ = last_connect_info
        for ln in code.splitlines():
            f.write(ln.encode() + b"\n")
        f.write(b"\n")

        start = f.readline().decode().rstrip()
        if start != "<<BEGIN>>":
            msg = "# Protocol error (no BEGIN)"
            if print_reply:
                print(msg)
            return msg
        payload_lines = []
        while True:
            ln = f.readline().decode().rstrip()
            if ln == "<<END>>":
                break
            payload_lines.append(ln)
        payload = "\n".join(payload_lines)
        if print_reply:
            print(payload)
        return payload

    def _replay_upto(count: int):
        """
        Replay the first 'count' submissions into a fresh simulation.
        Suppresses normal OK output; prints errors if any occur.
        """
        nonlocal submissions
        print(f"# [step_back] restarting and replaying {count} of {len(submissions)} submissions ...")

        # drop existing connection and sim
        _close_conn_only()
        _restart_and_reconnect()

        for i, code in enumerate(submissions[:count], 1):
            payload = _send_code_and_read_reply(code, print_reply=False)
            if payload.startswith("[ERROR]"):
                print(f"# [replay {i}] ERROR while applying a past snippet:")
                print(payload)

        print("# [step_back] replay complete.")

    print(f"Connecting to {HOST}:{PORT} ...")
    _connect_new_sim()
    _history_init()
    
    buf: list[str] = []
    indent_levels = 0

    re_stepback_call = re.compile(r"^\s*step_back\s*\(\s*(\d+)\s*\)\s*$")
    re_colon_stepback = re.compile(r"^\s*:\s*(?:stepback|rewind)\s+(\d+)\s*$")

    def _set_default_indent(level: int):
        """Pre-fill the input line with indentation for a nicer UX."""
        spaces = " " * (max(level, 0) * 4)

        def hook():
            try:
                readline.insert_text(spaces)
                readline.redisplay()
            finally:
                readline.set_pre_input_hook(None)

        try:
            if sys.stdin.isatty():
                readline.set_pre_input_hook(hook)
        except Exception:
            pass

    try:
        while True:
            prompt = ">>> " if not buf else "... "

            if buf or indent_levels > 0:
                _set_default_indent(indent_levels)
            try:
                line = input(prompt)
            except EOFError:
                line = ":quit"

            # ':' commands (local) -----
            m = re_colon_stepback.match(line)
            if m and not buf:
                n = int(m.group(1))
                keep = max(0, len(submissions) - n)
                _replay_upto(keep)
                indent_levels = 0
                continue

            if line.strip() in (":quit", ":q"):
                _close_conn_only()
                if sim_proc is not None:
                    _terminate_sim(sim_proc)
                break

            if not buf and line.strip() == "":
                continue

            # SINGLE-LINE MODE
            starts_block = line.rstrip().endswith(":")
            if not buf and not starts_block:
                m = re_stepback_call.match(line)
                if m:
                    n = int(m.group(1))
                    keep = max(0, len(submissions) - n)
                    _replay_upto(keep)
                    indent_levels = 0
                    continue

                _history_add_line(line)
                payload = _send_code_and_read_reply(line)
                submissions.append(line)
                indent_levels = 0
                continue

            # BLOCK MODE
            if line.strip() == "":
                code = "\n".join(buf)
                buf.clear()

                m = re_stepback_call.match(code)
                if m:
                    n = int(m.group(1))
                    keep = max(0, len(submissions) - n)
                    _replay_upto(keep)
                    indent_levels = 0
                    continue

                _history_add_line(code)
                payload = _send_code_and_read_reply(code)
                submissions.append(code)
                indent_levels = 0
                continue

            buf.append(line)

            # Compute indentation for the next line:
            stripped = line.lstrip(" ")
            leading_spaces = len(line) - len(stripped)
            current_level = max(leading_spaces // 4, 0)
            indent_levels = current_level + (1 if stripped.endswith(":") else 0)

    except KeyboardInterrupt:
        print("\n# Caught KeyboardInterrupt, cleaning up...")
        _close_conn_only()
        if sim_proc is not None:
            _terminate_sim(sim_proc)
        return

def start_sim(sim_dir):
    """
    Start/drive the interactive client session.
    """
    try:
        _client_loop(sim_dir)
    finally:
        pid = _read_pidfile()
        if pid:
            _kill_process_group_by_pid(pid)
            _remove_pidfile()
        _wait_for_port_closed(HOST, PORT, timeout=5.0)

if __name__ == "__main__":
    start_sim("tests")
