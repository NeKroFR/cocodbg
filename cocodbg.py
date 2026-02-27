import socket
import threading
import queue
import traceback
import contextlib
import sys
import os
import time
import subprocess
import shutil
from typing import Tuple
import ast
import readline
import signal
import re
import select


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
                if line == b":cancel":
                    self.cancel_evt.set()
                    with contextlib.suppress(Exception):
                        self._send_line(conn_file, b"# cancel requested")
                    continue

                if line == b"":  # blank line => submit accumulated buffer
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
    - If the last statement is an expression, return it so the REPL shows its value.
    - Empty/comments/docstring-only bodies -> 'pass'.
    - Names assigned at the user's top level are declared 'global' so values persist.
    """
    def _assigned_at_toplevel_safe(src: str) -> set[str]:
        try:
            tree = ast.parse(src, mode="exec")
            return _assigned_at_toplevel(tree)
        except Exception:
            return set()

    def _is_only_comments_or_blank(src: str) -> bool:
        for ln in src.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            return False
        return True

    def _is_only_docstrings(src: str) -> bool:
        # True if the module body contains only Expr(Constant(str)) nodes (docstrings)
        try:
            tree = ast.parse(src, mode="exec")
        except Exception:
            return False
        if not tree.body:
            return False
        for node in tree.body:
            if not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
                    and isinstance(node.value.value, str)):
                return False
        return True

    globals_line = ""
    assigned = sorted(_assigned_at_toplevel_safe(code))
    if assigned:
        globals_line = "    global " + ", ".join(assigned) + "\n"

    # If input is only comments/blank or pure docstrings: body is 'pass'
    if _is_only_comments_or_blank(code) or _is_only_docstrings(code):
        body = (globals_line + "    pass") if globals_line else "    pass"
        return f"async def __user_main__():\n{body}\n"

    # Try to auto-return the last expression
    try:
        tree = ast.parse(code, mode="exec")
        if tree.body and isinstance(tree.body[-1], ast.Expr) and hasattr(tree.body[-1], "lineno"):
            last = tree.body[-1]
            lines = code.splitlines()
            start = last.lineno - 1
            end = getattr(last, "end_lineno", last.lineno) - 1
            expr_src = ast.get_source_segment(code, last.value)
            if not expr_src:
                expr_src = "\n".join(lines[start:end + 1]).strip()
            kept = lines[:start] + lines[end + 1:]
            # If kept part is only comments/blank/docstrings, ensure at least one 'pass'
            kept_src = "\n".join(kept)
            if _is_only_comments_or_blank(kept_src) or _is_only_docstrings(kept_src):
                indented = ["    pass"]
            else:
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

_C_OK    = "\033[92m"
_C_ERR   = "\033[91m"
_C_RESET = "\033[0m"

_BUILTIN_NAMES = frozenset({
    "dut", "clk", "cocotb", "Timer", "RisingEdge", "FallingEdge", "Clock",
    "BinaryValue", "start_clock", "stop_clock", "set_clock_rate", "step",
    "run_for", "cycles", "as_uint", "as_sint", "start_vcd", "stop_vcd",
    "gen_vcd", "view_vcd", "gtkwave_live_start", "gtkwave_live_stop",
    "wait_until", "show_sigs", "show_vars", "list_signals",
    "register_clock", "stop_all_clocks", "show_clocks",
})

_COLON_CMDS = [
    ":quit", ":q", ":clear", ":cls", ":stepback", ":rewind",
    ":n", ":c", ":p", ":b", ":r", ":vars", ":sig",
]


def _fmt_reply(payload: str) -> str:
    if payload.startswith("[OK]"):
        lines = payload.splitlines()
        lines[0] = _C_OK + lines[0] + _C_RESET
        return "\n".join(lines)
    if payload.startswith("[ERROR]"):
        return _C_ERR + payload + _C_RESET
    return payload


def _make_completer(user_names: set[str]):
    def completer(text, state):
        if text.startswith(":"):
            options = [c for c in _COLON_CMDS if c.startswith(text)]
        else:
            options = sorted(n for n in (_BUILTIN_NAMES | user_names) if n.startswith(text))
        return options[state] if state < len(options) else None
    return completer

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

def _preflight_cleanup(host: str, port: int):
    """
    Before launching a new sim, try to clean up any leftovers.
    It kills by PID from pidfile (group-aware) and then waits for TCP port to be fully closed.
    """
    pid = _read_pidfile()
    if pid:
        _kill_process_group_by_pid(pid)
        _remove_pidfile()
    _wait_for_port_closed(host, port, timeout=5.0)

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

def _terminate_sim(sim_proc: subprocess.Popen, host: str = "127.0.0.1", port: int = 9999):
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
        _wait_for_port_closed(host, port, timeout=10.0)
        _remove_pidfile()

def _client_loop(sim_dir: str, host: str = "127.0.0.1", port: int = 9999):
    """
    Manage the whole interactive session, including restarts and step_back().
    Groups user input into one snippet when:
      - A block starts with ':' and ends on blank line (original behavior), OR
      - Parentheses/brackets/braces are open across lines, OR
      - Inside a triple-quoted string ('''...''' or \"\"\"...\"\"\").
    """
    submissions: list[str] = []   # snippets actually sent to the server
    last_connect_info = None      # (socket, file, banner)
    sim_proc: subprocess.Popen | None = None

    def _connect_new_sim():
        nonlocal sim_proc, last_connect_info
        _preflight_cleanup(host, port)

        sim_proc, _ = _launch_sim(sim_dir)

        print(f"waiting for {host}:{port} ...")
        if not _wait_for_port_open(host, port, timeout=60.0):
            print("Error: debugger port did not open in time.")
            if sim_proc is not None:
                _terminate_sim(sim_proc, host, port)
            sys.exit(1)

        s, f, banner = _open_connection(host, port)
        last_connect_info = (s, f, banner)
        print(banner)
        print("# Auto-detect blocks: lines ending with ':' start a block; finish with a blank line.")
        print("# Multi-line calls with (), [], {} are grouped automatically; they send when brackets balance. :quit to exit.")

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
        nonlocal sim_proc
        if sim_proc is not None:
            _terminate_sim(sim_proc, host, port)
            sim_proc = None
        time.sleep(0.05)
        _connect_new_sim()

    def _send_code_and_read_reply(code: str, *, print_reply: bool = True) -> str:
        nonlocal last_connect_info
        assert last_connect_info is not None
        s, f, _ = last_connect_info
        for ln in code.splitlines():
            f.write(ln.encode() + b"\n")
        f.write(b"\n")

        try:
            start = f.readline().decode().rstrip()
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                f.write(b":cancel\n"); f.flush()
            print("# ^C: cancel requested; reconnecting to debugger without stopping the simulation...")
            _close_conn_only()
            s2, f2, banner2 = _open_connection(HOST, PORT)
            last_connect_info = (s2, f2, banner2)
            print(banner2)
            return "[OK] # cancelled by user"

        if start != "<<BEGIN>>":
            msg = "# Protocol error (no BEGIN)"
            if print_reply: print(msg)
            return msg

        payload_lines = []
        while True:
            try:
                ln = f.readline().decode().rstrip()
            except KeyboardInterrupt:
                with contextlib.suppress(Exception):
                    f.write(b":cancel\n"); f.flush()
                print("# ^C: cancel requested; reconnecting to debugger without stopping the simulation...")
                _close_conn_only()
                s2, f2, banner2 = _open_connection(HOST, PORT)
                last_connect_info = (s2, f2, banner2)
                print(banner2)
                return "[OK] # cancelled by user"
            if ln == "<<END>>": break
            payload_lines.append(ln)
        payload = "\n".join(payload_lines)
        if print_reply:
            print(_fmt_reply(payload) if sys.stdout.isatty() else payload)
        return payload

    def _replay_upto(count: int):
        nonlocal submissions
        print(f"# [step_back] restarting and replaying {count} of {len(submissions)} submissions ...")
        _close_conn_only()
        _restart_and_reconnect()
        for i, code in enumerate(submissions[:count], 1):
            payload = _send_code_and_read_reply(code, print_reply=False)
            if payload.startswith("[ERROR]"):
                print(f"# [replay {i}] ERROR while applying a past snippet:\n{payload}")
        print("# [step_back] replay complete.")

    print(f"Connecting to {host}:{port} ...")
    _connect_new_sim()
    _history_init()

    _user_names: set[str] = set()
    try:
        _sig_raw = _send_code_and_read_reply("list_signals()", print_reply=False)
        if _sig_raw.startswith("[OK] "):
            _user_names.update(f"dut.{n}" for n in ast.literal_eval(_sig_raw[5:].strip()))
    except Exception:
        pass
    readline.set_completer(_make_completer(_user_names))
    readline.parse_and_bind("tab: complete")

    _tty = sys.stdout.isatty()
    _PROMPT      = "\001\033[96m\002>>>\001\033[0m\002 " if _tty else ">>> "
    _PROMPT_CONT = "\001\033[2m\002...\001\033[0m\002 " if _tty else "... "

    buf: list[str] = []
    indent_levels = 0
    bracket_depth = 0
    colon_mode = False         # True when a block was started by ':' line
    in_triple = False          # True when inside '''...''' or """..."""
    triple_delim = ""          # either "'''" or '"""'

    re_stepback_call = re.compile(r"^\s*step_back\s*\(\s*(\d+)\s*\)\s*$")
    re_colon_stepback = re.compile(r"^\s*:\s*(?:stepback|rewind)\s+(\d+)\s*$")

    def _set_default_indent(level: int):
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

    def _bracket_delta(s: str) -> int:
        # naive but effective: counts (), [], {}
        return (s.count("(") - s.count(")")) + (s.count("[") - s.count("]")) + (s.count("{") - s.count("}"))

    def _strip_comment(line: str) -> str:
        """Strip trailing # comment, respecting single-quoted strings."""
        in_str = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\" :
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == "#":
                return line[:i].rstrip()
            i += 1
        return line.rstrip()

    def _triple_toggle(line: str):
        """
        Toggle triple-quote state if an odd number of the active delimiter appears in the line.
        If not currently in triple mode, detect the first occurrence of ''' or \"\"\".
        """
        nonlocal in_triple, triple_delim
        if in_triple:
            cnt = line.count(triple_delim)
            if cnt % 2 == 1:
                in_triple = False
                triple_delim = ""
        else:
            i1 = line.find("'''")
            i2 = line.find('"""')
            if i1 == -1 and i2 == -1:
                return
            if i1 != -1 and (i2 == -1 or i1 < i2):
                triple_delim = "'''"
            else:
                triple_delim = '"""'
            # if same delimiter also closes on the same line (e.g. '''x'''), count toggles
            cnt = line.count(triple_delim)
            in_triple = (cnt % 2 == 1)

    try:
        while True:
            prompt = _PROMPT if not buf else _PROMPT_CONT
            if buf or indent_levels > 0:
                _set_default_indent(indent_levels)
            try:
                line = input(prompt)
            except EOFError:
                line = ":quit"

            # local ':' commands when not collecting a block
            m = re_colon_stepback.match(line)
            if m and not buf and not in_triple:
                n = int(m.group(1))
                keep = max(0, len(submissions) - n)
                _replay_upto(keep)
                indent_levels = 0
                continue

            if line.strip() in (":quit", ":q"):
                _close_conn_only()
                if sim_proc is not None:
                    _terminate_sim(sim_proc, host, port)
                break

            if not buf and not in_triple and line.strip() in (":clear", ":cls"):
                print("\033[2J\033[H", end="", flush=True)  # ANSI clear screen + move cursor home
                continue

            if not buf and not in_triple and line.strip() == "":
                continue

            if not buf and not in_triple:
                _s = line.strip()
                if _s == ":n":
                    line = "await step()"
                elif _s == ":r":
                    _replay_upto(0)
                    indent_levels = 0
                    continue
                elif _s == ":vars":
                    line = "show_vars()"
                elif _s == ":sig":
                    line = "show_sigs()"
                elif _s.startswith(":p "):
                    line = _s[3:].strip()
                elif _s.startswith(":c"):
                    parts = _s[2:].strip().split()
                    if not parts:
                        line = "await cycles(1)"
                    elif len(parts) == 1:
                        line = f"await cycles({parts[0]})"
                    else:
                        line = f'await run_for({parts[0]}, "{parts[1]}")'
                elif _s.startswith(":b "):
                    line = f"await wait_until(lambda: {_s[3:].strip()})"

            # compute grouping signals
            delta = 0 if in_triple else _bracket_delta(line)
            starts_colon_block = (not in_triple) and _strip_comment(line).endswith(":")

            # Stepback single-line shortcut (only if not inside a block)
            if not buf and not in_triple:
                m = re_stepback_call.match(line)
                if m:
                    n = int(m.group(1))
                    keep = max(0, len(submissions) - n)
                    _replay_upto(keep)
                    indent_levels = 0
                    continue

            # Triple-quote state update must happen after checks above
            _triple_toggle(line)

            # SINGLE-LINE or START BLOCK
            if not buf:
                if starts_colon_block:
                    buf.append(line)
                    colon_mode = True
                    bracket_depth = delta
                    indent_levels = (len(line) - len(line.lstrip(" "))) // 4 + 1
                    continue

                if delta > 0 or in_triple:
                    # start implicit paren block or triple-quoted block
                    buf.append(line)
                    colon_mode = False
                    bracket_depth = delta
                    indent_levels = 0
                    continue

                # plain single-line
                _history_add_line(line)
                payload = _send_code_and_read_reply(line)
                submissions.append(line)
                if payload.startswith("[OK]"):
                    try:
                        _user_names.update(_assigned_at_toplevel(ast.parse(line)))
                    except Exception:
                        pass
                indent_levels = 0
                continue

            # We are inside a block (colon or implicit or triple)
            if colon_mode:
                if not in_triple and line.strip() == "":
                    code = "\n".join(buf)
                    buf.clear()
                    colon_mode = False
                    bracket_depth = 0

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
                    if payload.startswith("[OK]"):
                        try:
                            _user_names.update(_assigned_at_toplevel(ast.parse(code)))
                        except Exception:
                            pass
                    indent_levels = 0
                    continue

                # still inside colon block
                buf.append(line)
                bracket_depth += (0 if in_triple else _bracket_delta(line))
                indent_levels = (len(line) - len(line.lstrip(" "))) // 4 + (1 if (not in_triple and line.rstrip().endswith(":")) else 0)
                continue

            # implicit paren/triple block
            buf.append(line)
            if not in_triple:
                bracket_depth += delta

            # closing condition: either brackets balanced (<=0) AND not in triple; or triple closed with balanced brackets
            if (not in_triple and bracket_depth <= 0):
                code = "\n".join(buf)
                buf.clear()
                bracket_depth = 0

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
                if payload.startswith("[OK]"):
                    try:
                        _user_names.update(_assigned_at_toplevel(ast.parse(code)))
                    except Exception:
                        pass
                indent_levels = 0
                continue

            # still collecting implicit/triple block
            indent_levels = 0

    except KeyboardInterrupt:
        print("\n# Caught KeyboardInterrupt, cleaning up...")
        _close_conn_only()
        if sim_proc is not None:
            _terminate_sim(sim_proc, host, port)
        return

def start_sim(sim_dir: str, host: str = "127.0.0.1", port: int = 9999):
    """
    Start/drive the interactive client session.
    """
    try:
        _client_loop(sim_dir, host, port)
    finally:
        pid = _read_pidfile()
        if pid:
            _kill_process_group_by_pid(pid)
            _remove_pidfile()
        _wait_for_port_closed(host, port, timeout=5.0)

class CocotbDebugConfig:
    """
    Configuration for a cocodbg session.

    Generates a hidden .cocodbg/ directory with a Makefile and executor test file,
    launches the simulation, then cleans everything up on exit.

    Usage::

        from cocodbg import CocotbDebugConfig

        CocotbDebugConfig(
            toplevel="adder",
            sources=["adder.vhd"],
            sim="ghdl",
            lang="vhdl",
            clk_signal="clk",
            clk_period_ns=10.0,
        ).run()
    """

    _COCODBG_DIR = ".cocodbg"

    def __init__(
        self,
        toplevel: str,
        sources: list[str],
        *,
        sim: str = "ghdl",
        lang: str = "vhdl",
        clk_signal: str = "clk",
        clk_period_ns: float = 10.0,
        port: int = 9999,
        host: str = "127.0.0.1",
    ):
        self.toplevel = toplevel
        self.sources = [os.path.abspath(s) for s in sources]
        self.sim = sim
        self.lang = lang.lower()
        self.clk_signal = clk_signal
        self.clk_period_ns = float(clk_period_ns)
        self.port = port
        self.host = host

    def _target_dir(self) -> str:
        return os.path.abspath(self._COCODBG_DIR)

    def _write_makefile(self, target_dir: str):
        if self.lang == "vhdl":
            src_var = "VHDL_SOURCES"
        else:
            src_var = "VERILOG_SOURCES"

        sources_str = " ".join(self.sources)
        toplevel_lang = "verilog" if self.lang in ("verilog", "sv", "systemverilog") else self.lang

        content = (
            f"SIM = {self.sim}\n"
            f"TOPLEVEL_LANG = {toplevel_lang}\n"
            f"TOPLEVEL = {self.toplevel}\n"
            f"MODULE = _cocodbg_test\n"
            f"{src_var} = {sources_str}\n"
            f"\n"
            f"export PYTHONPATH := $(abspath .):$(abspath ..):$(PYTHONPATH)\n"
            f"include $(shell cocotb-config --makefiles)/Makefile.sim\n"
        )
        with open(os.path.join(target_dir, "Makefile"), "w") as f:
            f.write(content)

    def _write_executor(self, target_dir: str):
        executor_src = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tests", "test_cocodbg.py"
        )
        if not os.path.exists(executor_src):
            raise FileNotFoundError(
                f"Executor template not found: {executor_src}\n"
                "Make sure tests/test_cocodbg.py exists alongside cocodbg.py."
            )
        shutil.copy2(executor_src, os.path.join(target_dir, "_cocodbg_test.py"))

    def run(self):
        """Generate .cocodbg/, run the simulation, clean up on exit."""
        target_dir = self._target_dir()

        # Clean up any leftover directory from a previous crash
        if os.path.isdir(target_dir):
            if os.path.exists(os.path.join(target_dir, "_cocodbg_test.py")):
                print("# Found leftover .cocodbg/ from a previous session, cleaning up...")
                shutil.rmtree(target_dir, ignore_errors=True)

        os.makedirs(target_dir, exist_ok=True)
        self._write_makefile(target_dir)
        self._write_executor(target_dir)

        # Pass config to the executor via env vars
        os.environ["COCODBG_HOST"] = self.host
        os.environ["COCODBG_PORT"] = str(self.port)
        os.environ["COCODBG_CLK_SIGNAL"] = self.clk_signal
        os.environ["COCODBG_CLK_PERIOD_NS"] = str(self.clk_period_ns)

        try:
            start_sim(target_dir, host=self.host, port=self.port)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)


if __name__ == "__main__":
    start_sim("tests")
