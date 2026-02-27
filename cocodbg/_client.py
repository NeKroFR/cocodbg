import sys
import os
import time
import ast
import readline
import re
import contextlib

from ._ast import _assigned_at_toplevel
from ._process import (
    _preflight_cleanup, _launch_sim, _terminate_sim,
    _wait_for_port_open, _wait_for_port_closed, _open_connection,
    _kill_process_group_by_pid, _read_pidfile, _remove_pidfile,
)

_C_OK    = "\033[92m"
_C_ERR   = "\033[91m"
_C_RESET = "\033[0m"

_BUILTIN_NAMES = frozenset({
    "dut", "clk", "cocotb", "Timer", "RisingEdge", "FallingEdge", "Clock",
    "BinaryValue", "start_clock", "stop_clock", "set_clock_rate", "step",
    "run_for", "cycles", "as_uint", "as_sint", "start_vcd", "stop_vcd",
    "gen_vcd", "view_vcd", "gtkwave_live_start", "gtkwave_live_stop",
    "wait_until", "show_sigs", "show_vars", "list_signals", "sim_time",
    "register_clock", "stop_all_clocks", "show_clocks",
})

_COLON_CMDS = [
    ":quit", ":q", ":clear", ":cls", ":stepback", ":rewind",
    ":n", ":c", ":p", ":b", ":r", ":vars", ":sig", ":help", ":h",
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


_HISTORY_FILE = os.path.expanduser("~/.cocodbg_history")


def _history_init():
    readline.set_auto_history(False)
    readline.clear_history()
    with contextlib.suppress(Exception):
        readline.read_history_file(_HISTORY_FILE)


def _history_save():
    with contextlib.suppress(Exception):
        readline.write_history_file(_HISTORY_FILE)


def _print_help():
    print(
        "colon commands:\n"
        "  :q / :quit          stop simulation and exit\n"
        "  :r / :restart       restart simulation, replay all previous inputs\n"
        "  :stepback N         replay up to N submissions back\n"
        "  :n                  step one clock cycle (alias for await step())\n"
        "  :c [N] [unit]       advance N cycles or run_for N unit\n"
        "  :b <expr>           wait until lambda: <expr> is true\n"
        "  :p <expr>           print <expr>\n"
        "  :vars               show user-defined variables\n"
        "  :sig                show all DUT signals\n"
        "  :clear / :cls       clear the terminal\n"
        "  :help / :h          show this help\n"
        "\n"
        "built-in functions (use with await where needed):\n"
        "  cycles(n)                 advance n clock cycles\n"
        "  run_for(t, units)         advance t time (ns/us/ms/ps/fs/s)\n"
        "  sim_time(units)           current simulation time\n"
        "  start_clock()             start the default free-running clock\n"
        "  stop_clock()              stop the clock\n"
        "  set_clock_rate(t, units)  change clock period\n"
        "  wait_until(cond)          wait for a signal condition\n"
        "  register_clock(n, sig)    register a named clock domain\n"
        "  show_clocks()             list clock domains\n"
        "  stop_all_clocks()         stop all running clocks\n"
        "  show_sigs()               print all DUT signals\n"
        "  show_vars()               print user-defined variables\n"
        "  gen_vcd(path)             write VCD to file\n"
        "  view_vcd(path)            open VCD in GTKWave\n"
        "  start_vcd()               start a fresh VCD recording window\n"
        "  stop_vcd()                stop recording\n"
        "  gtkwave_live_start()      stream live VCD to GTKWave\n"
        "  gtkwave_live_stop()       stop live stream\n"
        "  as_uint(x) / as_sint(x)  read signal as unsigned/signed int"
    )


def _history_add_line(line: str):
    if not line.strip():
        return
    if line.lstrip().startswith(":"):
        return
    n = readline.get_current_history_length()
    last = readline.get_history_item(n) if n > 0 else None
    if last != line:
        readline.add_history(line)


def _client_loop(sim_dir: str, host: str = "127.0.0.1", port: int = 9999):
    submissions: list[str] = []
    last_connect_info = None
    sim_proc = None

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
            s2, f2, banner2 = _open_connection(host, port)
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
                s2, f2, banner2 = _open_connection(host, port)
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
    colon_mode = False
    in_triple = False
    triple_delim = ""

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
        depth = 0
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch in ('"', "'"):
                q = s[i:i+3]
                if q in ('"""', "'''"):
                    close = s.find(q, i + 3)
                    i = (close + 3) if close != -1 else n
                    continue
                i += 1
                while i < n:
                    if s[i] == "\\" :
                        i += 2
                        continue
                    if s[i] == ch:
                        i += 1
                        break
                    i += 1
                continue
            if ch == "#":
                break
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            i += 1
        return depth

    def _strip_comment(line: str) -> str:
        in_str = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == "\\":
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

            m = re_colon_stepback.match(line)
            if m and not buf and not in_triple:
                n = int(m.group(1))
                keep = max(0, len(submissions) - n)
                _replay_upto(keep)
                indent_levels = 0
                continue

            if line.strip() in (":quit", ":q"):
                _history_save()
                _close_conn_only()
                if sim_proc is not None:
                    _terminate_sim(sim_proc, host, port)
                break

            if not buf and not in_triple and line.strip() in (":help", ":h"):
                _print_help()
                continue

            if not buf and not in_triple and line.strip() in (":clear", ":cls"):
                print("\033[2J\033[H", end="", flush=True)
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

            delta = 0 if in_triple else _bracket_delta(line)
            starts_colon_block = (not in_triple) and _strip_comment(line).endswith(":")

            if not buf and not in_triple:
                m = re_stepback_call.match(line)
                if m:
                    n = int(m.group(1))
                    keep = max(0, len(submissions) - n)
                    _replay_upto(keep)
                    indent_levels = 0
                    continue

            _triple_toggle(line)

            if not buf:
                if starts_colon_block:
                    buf.append(line)
                    colon_mode = True
                    bracket_depth = delta
                    indent_levels = (len(line) - len(line.lstrip(" "))) // 4 + 1
                    continue

                if delta > 0 or in_triple:
                    buf.append(line)
                    colon_mode = False
                    bracket_depth = delta
                    indent_levels = 0
                    continue

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

                buf.append(line)
                bracket_depth += (0 if in_triple else _bracket_delta(line))
                indent_levels = (len(line) - len(line.lstrip(" "))) // 4 + (1 if (not in_triple and line.rstrip().endswith(":")) else 0)
                continue

            buf.append(line)
            if not in_triple:
                bracket_depth += delta

            if not in_triple and bracket_depth <= 0:
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

            indent_levels = 0

    except KeyboardInterrupt:
        print("\n# Caught KeyboardInterrupt, cleaning up...")
        _history_save()
        _close_conn_only()
        if sim_proc is not None:
            _terminate_sim(sim_proc, host, port)
        return


def start_sim(sim_dir: str, host: str = "127.0.0.1", port: int = 9999):
    """Start/drive the interactive client session."""
    try:
        _client_loop(sim_dir, host, port)
    finally:
        pid = _read_pidfile()
        if pid:
            _kill_process_group_by_pid(pid)
            _remove_pidfile()
        _wait_for_port_closed(host, port, timeout=5.0)
