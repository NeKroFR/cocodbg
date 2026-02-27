import os
import cocotb
from cocotb.triggers import Timer, RisingEdge, FallingEdge, Event, Edge
from cocotb.clock import Clock
from cocotb.binary import BinaryValue
from cocotb.handle import ModifiableObject, SimHandleBase
from cocotb.utils import get_sim_time

from cocodbg import CocotbDebugger, wrap_code_as_async, format_exception

import queue
import io
import contextlib
import inspect
import operator
import subprocess
import fcntl
from typing import Any, Awaitable, Callable

DBG_HOST = os.environ.get("COCODBG_HOST", "127.0.0.1")
DBG_PORT = int(os.environ.get("COCODBG_PORT", "9999"))
_CLK_SIGNAL = os.environ.get("COCODBG_CLK_SIGNAL", "clk")
_CLK_PERIOD_NS = float(os.environ.get("COCODBG_CLK_PERIOD_NS", "10.0"))

def format_user_input(code: str) -> str:
    """
    Workaround for terminals that sometimes paste a stray leading backslash.
    """
    out_lines = []
    for ln in code.splitlines():
        stripped = ln.lstrip()
        indent = ln[: len(ln) - len(stripped)]
        if stripped.startswith("\\") and len(stripped) >= 2 and (stripped[1].isalpha() or stripped[1] == "_"):
            stripped = stripped[1:]
        out_lines.append(indent + stripped)
    return "\n".join(out_lines)

@cocotb.test()
async def cocodbg(dut):
    """
    REPL server so where we can inject code into the simulation from cocodbg.
    """
    dbg = CocotbDebugger(DBG_HOST, DBG_PORT)
    dbg.start()
    dut._log.info(f"[cocotb-dbg] listening on {DBG_HOST}:{DBG_PORT}")

    clk_handle = getattr(dut, _CLK_SIGNAL, None)

    # Clock domains: name -> {handle, task, period_ns}
    CLOCKS = {
        "default": {"handle": clk_handle, "task": None, "period_ns": _CLK_PERIOD_NS},
    }

    def _get_clk(clk=None):
        if clk is None:
            return CLOCKS["default"]
        if isinstance(clk, str):
            if clk not in CLOCKS:
                raise ValueError(f"Unknown clock domain: '{clk}'. Register with register_clock().")
            return CLOCKS[clk]
        # raw handle — find matching registered domain or build a transient one
        for domain in CLOCKS.values():
            if domain["handle"] is clk:
                return domain
        return {"handle": clk, "task": None, "period_ns": CLOCKS["default"]["period_ns"]}

    # helpers: time conversion
    def _to_ns(value, units: str) -> float:
        units = units.lower()
        if units == "fs": return float(value) * 1e-6
        if units == "ps": return float(value) * 1e-3
        if units == "ns": return float(value)
        if units == "us": return float(value) * 1e3
        if units == "ms": return float(value) * 1e6
        if units == "s":  return float(value) * 1e9
        raise ValueError(f"Unsupported units for period: {units}")

    def _to_total_ps(value, units: str) -> int:
        units = units.lower()
        if units == "fs": return int(round(float(value) / 1000.0))
        if units == "ps": return int(round(float(value)))
        if units == "ns": return int(round(float(value) * 1_000.0))
        if units == "us": return int(round(float(value) * 1_000_000.0))
        if units == "ms": return int(round(float(value) * 1_000_000_000.0))
        if units == "s":  return int(round(float(value) * 1_000_000_000_000.0))
        raise ValueError(f"Unsupported units for time: {units}")

    async def _await_time_ps(ps: int):
        """
        Await an integer amount of time in picoseconds, chunked by s/ms/us/ns/ps.
        Avoids float drift AND avoids huge loops for long durations.
        """
        if ps <= 0:
            return
        for unit, factor in (("s", 1_000_000_000_000),
                             ("ms",    1_000_000_000),
                             ("us",        1_000_000),
                             ("ns",            1_000),
                             ("ps",                1)):
            count, ps = divmod(ps, factor)
            if count:
                if dbg.cancel_evt.is_set():
                    return
                await Timer(int(count), units=unit)

    async def start_clock(period_ns: float | None = None, clk=None):
        """
        Start a FREE-RUNNING clock on the given domain (default: "default").
        WARNING: Once started, time will advance autonomously until stop_clock().
        """
        domain = _get_clk(clk)
        p = float(period_ns if period_ns is not None else domain["period_ns"])
        if domain["task"] is not None:
            domain["task"].kill()
            domain["task"] = None
        domain["period_ns"] = p
        _snapshot_commit_now()
        handle = domain["handle"]
        if handle is None:
            raise RuntimeError(f"Clock handle for domain '{clk or 'default'}' is None")
        domain["task"] = cocotb.start_soon(Clock(handle, p, units="ns").start())

    async def stop_clock(freeze: str = "low", clk=None):
        """
        Stop the free-running clock on the given domain and freeze its level.
        freeze: "low" (default), "high", or "preserve"
        """
        _snapshot_commit_now()
        domain = _get_clk(clk)
        if domain["task"] is not None:
            domain["task"].kill()
            domain["task"] = None
        handle = domain["handle"]
        if handle is not None:
            if freeze == "low":
                with contextlib.suppress(Exception):
                    handle.value = 0
            elif freeze == "high":
                with contextlib.suppress(Exception):
                    handle.value = 1
        _snapshot_commit_now()

    async def set_clock_rate(period_value, units: str = "ns", clk=None):
        """
        Set the period for the given clock domain.
        Leaves the clock STOPPED (frozen low for determinism).
        """
        _snapshot_commit_now()
        await stop_clock(freeze="low", clk=clk)
        _get_clk(clk)["period_ns"] = _to_ns(period_value, units)
        _snapshot_commit_now()

    async def step():
        """Advance exactly one delta (no time advance)."""
        _snapshot_commit_now()
        await Timer(1, units="step")
        _snapshot_commit_now()

    async def cycles(n: int, period_ns: float | None = None, clk=None):
        """
        Drive EXACTLY n clock cycles *manually* on the given domain.
        - If a free-running clock is active on that domain, stop it first.
        - period_ns defaults to the domain's period_ns.
        - Uses integer picoseconds throughout, so it's drift-free.
        """
        _snapshot_commit_now()

        domain = _get_clk(clk)
        handle = domain["handle"]

        if domain["task"] is not None:
            await stop_clock(clk=clk)

        p_ns = float(domain["period_ns"] if period_ns is None else period_ns)
        period_ps = int(round(p_ns * 1_000.0)) # 1 ns = 1000 ps
        half_ps = period_ps // 2
        first_half_ps = half_ps + (period_ps - 2 * half_ps)
        second_half_ps = half_ps

        with contextlib.suppress(Exception):
            handle.value = 0
        await Timer(1, units="step")
        _emit_single_signal_now(handle)

        for _ in range(int(n)):
            if dbg.cancel_evt.is_set():
                break
            await _await_time_ps(first_half_ps)
            handle.value = 1
            await Timer(1, units="step")
            _emit_single_signal_now(handle)

            await _await_time_ps(second_half_ps)
            handle.value = 0
            await Timer(1, units="step")
            _emit_single_signal_now(handle)

        _snapshot_commit_now()

    async def _temp_free_run_for_ps(total_ps: int, freeze: str = "preserve", clk=None):
        """
        Phase-safe temporary free run on the given clock domain:
          - Starts at clk=0.
          - Manually toggles using the domain's period for at most total_ps.
          - Guarantees NO rising edge if total_ps < half-period.
          - At the end, 'freeze' controls the final level: "preserve" | "low" | "high".
        """
        _snapshot_commit_now()

        if total_ps <= 0:
            return

        domain = _get_clk(clk)
        handle = domain["handle"]
        p_ns = float(domain["period_ns"])
        period_ps = int(round(p_ns * 1_000.0)) # 1 ns = 1000 ps
        half_ps = period_ps // 2
        first_half_ps = half_ps + (period_ps - 2 * half_ps)
        second_half_ps = half_ps

        lvl = 0
        with contextlib.suppress(Exception):
            handle.value = lvl
        await Timer(1, units="step")
        _emit_single_signal_now(handle)

        rem = int(total_ps)
        while rem > 0:
            if dbg.cancel_evt.is_set():
                return
            if rem < first_half_ps:
                await _await_time_ps(rem)
                break
            await _await_time_ps(first_half_ps)
            lvl = 1
            handle.value = lvl
            await Timer(1, units="step")
            _emit_single_signal_now(handle)
            rem -= first_half_ps

            if rem < second_half_ps:
                await _await_time_ps(rem)
                break
            await _await_time_ps(second_half_ps)
            lvl = 0
            handle.value = lvl
            await Timer(1, units="step")
            _emit_single_signal_now(handle)
            rem -= second_half_ps

        if freeze == "low" and lvl != 0:
            handle.value = 0
        elif freeze == "high" and lvl != 1:
            handle.value = 1

    async def run_for(t, units: str = "ns", freeze: str = "preserve", clk=None):
        """
        Advance real time by t on the given clock domain.
          - If a free-running clock is active: just await t (edges come from it).
          - Else: run a phase-safe temporary clock for exactly t, then freeze as requested.
        freeze: "preserve" (default), "low", or "high"
        """
        _snapshot_commit_now()

        total_ps = _to_total_ps(t, units)
        domain = _get_clk(clk)

        if domain["task"] is not None:
            await _await_time_ps(total_ps)
            _snapshot_commit_now()
            return

        await _temp_free_run_for_ps(total_ps, freeze=freeze, clk=clk)

    ### Comparison helpers + auto-coercing comparisons ###
    def _bits_to_int(binstr: str, *, signed: bool = False) -> int:
        """
        Convert a cocotb BinaryValue.binstr (may contain UXZWH-) to int by
        substituting non-binary chars with '0'. If signed=True, interpret two's
        complement using the resulting bit width.
        """
        if not isinstance(binstr, str) or not binstr:
            return 0
        # normalize and substitute unknowns
        s = "".join(ch if ch in ("0", "1") else "0" for ch in binstr.lower())
        n = int(s, 2)
        if signed and s[0] == "1":
            n -= 1 << len(s)
        return n

    def as_uint(x):
        """
        Unsigned int view of a cocotb handle or BinaryValue.
        Resolves X/Z/U/W/H/- by substituting 0 (no exception).
        """
        v = getattr(x, "value", x)
        # Fast path when cocotb can already resolve
        try:
            if hasattr(v, "integer"):
                return int(v.integer)
        except Exception:
            pass
        # Fallbacks
        try:
            return int(v)  # if v is already a python int-like
        except Exception:
            pass
        try:
            return _bits_to_int(v.binstr, signed=False)
        except Exception:
            # last resort: string-ify and strip non-bits
            return _bits_to_int(str(v), signed=False)

    def as_sint(x):
        """
        Signed int view (two's complement). Unknowns resolved to 0.
        """
        v = getattr(x, "value", x)
        # If BinaryValue has signed_integer and it's resolvable, use it
        try:
            if hasattr(v, "signed_integer"):
                return int(v.signed_integer)
        except Exception:
            pass
        # Fallbacks
        try:
            return int(v)  # if already a python int-like
        except Exception:
            pass
        try:
            return _bits_to_int(v.binstr, signed=True)
        except Exception:
            return _bits_to_int(str(v), signed=True)

    # -------- Minimal VCD / Live pipeline (commit-only at time-control calls) --------
    VCD = {
        "pairs": [],          # [(hier_name, handle)]
        "ids": {},            # handle -> vcd_id
        "events": [],         # list[(time_ps, vid, change_line)]
        "last": {},           # vid -> last change_line committed
        "rec_active": True,

        "live": {
            "proc_shmidcat": None,
            "proc_gtkwave": None,
            "fd": None,
            "outbuf": bytearray(),
            "drain_task": None,
            "timescale": "ns",
            "drain_idx": 0,
            "selected_ids": None,
            "savefile": None,
            "order_vids": None,
        },
    }
    VCD_EVT = Event()

    def _collect_leaf_signals(root) -> list[tuple[str, ModifiableObject]]:
        """
        Collect (hier_name_relative_to_dut, handle) for leaf ModifiableObject
        signals under 'root', using iteration so prior attribute access is not required.
        """
        out: list[tuple[str, ModifiableObject]] = []

        def iter_children(node):
            """
            Iterate over children of a node, yielding (name, child) pairs.
            """
            for ch in node:
                name = getattr(ch, "_name", None) or getattr(ch, "name", None)
                if name is None:
                    fullname = getattr(ch, "_fullname", str(ch))
                    name = str(fullname).split(".")[-1]
                yield name, ch
        
        def rec(node, prefix: str):
            """
            Walk children to find all leaf ModifiableObject signals.
            """
            if isinstance(node, ModifiableObject):
                out.append((prefix or getattr(node, "_name", "sig"), node))
                return

            got_child = False
            for name, child in iter_children(node):
                got_child = True
                child_name = f"{prefix}.{name}" if prefix else name
                rec(child, child_name)

            if not got_child and isinstance(node, SimHandleBase):
                if isinstance(node, ModifiableObject):
                    out.append((prefix or getattr(node, "_name", "sig"), node))

        # start with empty prefix so names are relative to 'dut'
        rec(root, "")

        # Deduplicate by object identity
        unique: dict[int, tuple[str, ModifiableObject]] = {}
        for name, handle in out:
            unique.setdefault(id(handle), (name, handle))
        return list(unique.values())

    def _bv_width(val: BinaryValue) -> int:
        try:
            return int(val.n_bits)
        except Exception:
            try:
                return len(val.binstr)
            except Exception:
                return -1

    def _vcd_safe_name(name: str) -> str:
        return name.replace(" ", "_")

    def _vcd_id_generator():
        """
        Generate unique VCD IDs.
        """
        charset = [chr(c) for c in range(33, 127)]
        counter = [0]

        def next_id():
            i = counter[0]
            s = ""
            base = len(charset)
            if i == 0:
                s = charset[0]
            else:
                while i > 0:
                    s = charset[i % base] + s
                    i //= base
            counter[0] += 1
            return s

        return next_id

    def _fmt_change(sig: ModifiableObject, vcd_id: str) -> str:
        """
        Format a change event for a VCD signal.
        """
        v = sig.value
        w = _bv_width(v)

        try:
            b = v.binstr.lower()
        except Exception:
            try:
                b = format(int(v), "b")
            except Exception:
                b = "x" if w == 1 else "x" * max(1, w)
        if w <= 1 and len(b) == 1:
            return f"{b}{vcd_id}\n"
        return f"b{b} {vcd_id}\n"

    def _scale_ps(ps: int, unit: str) -> int:
        unit = unit.lower()
        if unit == "fs": return ps * 1000
        if unit == "ps": return ps
        if unit == "ns": return ps // 1_000
        if unit == "us": return ps // 1_000_000
        if unit == "ms": return ps // 1_000_000_000
        if unit == "s":  return ps // 1_000_000_000_000
        raise ValueError(f"Unsupported timescale unit: {unit}")

    def _ensure_vcd_initialized():
        """
        Ensure VCD monitors are initialized, if not initialize them.
        """ 
        if VCD["pairs"]:
            return
        pairs = _collect_leaf_signals(dut)
        next_id = _vcd_id_generator()
        ids = {sig: next_id() for _, sig in pairs}
        VCD["pairs"] = pairs
        VCD["ids"] = ids

        # Seed "last" with current values (no events yet)
        for _, sig in pairs:
            VCD["last"][ids[sig]] = _fmt_change(sig, ids[sig])

    def _emit_events(time_ps: int, vid_to_change: dict[str, str]):
        if not vid_to_change:
            return
        if not VCD["rec_active"]:
            return
        order_vids = VCD["live"]["order_vids"] or [VCD["ids"][s] for _, s in VCD["pairs"]]
        for vid in order_vids:
            ch = vid_to_change.get(vid)
            if ch is not None:
                VCD["events"].append((time_ps, vid, ch))
                VCD["last"][vid] = ch
        with contextlib.suppress(Exception):
            VCD_EVT.set()

    def _snapshot_commit_now():
        _ensure_vcd_initialized()
        t_ps = int(get_sim_time(units="ps"))
        changes = {}
        for _, sig in VCD["pairs"]:
            vid = VCD["ids"][sig]
            ch = _fmt_change(sig, vid)
            if VCD["last"].get(vid) != ch:
                changes[vid] = ch
        _emit_events(t_ps, changes)

    def _emit_single_signal_now(sig: ModifiableObject):
        _ensure_vcd_initialized()
        t_ps = int(get_sim_time(units="ps"))
        vid = VCD["ids"].get(sig)
        if not vid:
            return
        ch = _fmt_change(sig, vid)
        if VCD["last"].get(vid) != ch:
            _emit_events(t_ps, {vid: ch})

    # -------- File VCD dump --------
    async def start_vcd():
        _ensure_vcd_initialized()
        # Capture current signal state into VCD["last"], then start a fresh recording.
        for _, sig in VCD["pairs"]:
            vid = VCD["ids"][sig]
            VCD["last"][vid] = _fmt_change(sig, vid)
        VCD["events"].clear()
        if VCD["live"]["drain_task"] is not None:
            VCD["live"]["drain_idx"] = 0
        VCD["rec_active"] = True

    async def stop_vcd():
        """
        Stop recording (events after this call are not captured).
        Window end is marked at NOW.
        """
        _ensure_vcd_initialized()
        VCD["rec_active"] = False
        _snapshot_commit_now()

    def _resolve_out_path(path: str) -> str:
        base = os.environ.get("COCODBG_ROOT", os.getcwd())
        return path if os.path.isabs(path) else os.path.join(base, path)

    async def gen_vcd(path: str, *, timescale: str = "ns", signals=None):
        _ensure_vcd_initialized()
        _snapshot_commit_now()

        out_path = _resolve_out_path(path)
        _out_dir = os.path.dirname(out_path)
        if _out_dir:
            os.makedirs(_out_dir, exist_ok=True)

        pairs = VCD["pairs"]
        if signals is not None:
            sig_set = set(signals)
            selected_ids = {VCD["ids"][sig] for name, sig in pairs if name in sig_set}
        else:
            selected_ids = set(VCD["ids"][sig] for _, sig in pairs)

        if VCD["events"]:
            t_start = VCD["events"][0][0]
        else:
            t_start = int(get_sim_time(units="ps"))

        start_vals = {}
        for _, sig in pairs:
            vid = VCD["ids"][sig]
            start_vals[vid] = VCD["last"].get(vid, _fmt_change(sig, vid))

        with open(out_path, "w", buffering=1) as f:
            from datetime import datetime
            f.write(f"$date {datetime.now()} $end\n")
            f.write(f"$version cocotb-dbg vcd-recorder $end\n")
            f.write(f"$timescale 1 {timescale} $end\n")
            f.write(f"$scope module dut $end\n")
            for name, sig in pairs:
                w = _bv_width(sig.value)
                f.write(f"$var wire {max(1,w)} {VCD['ids'][sig]} {_vcd_safe_name(name)} $end\n")
            f.write("$upscope $end\n")
            f.write("$enddefinitions $end\n")

            t0 = _scale_ps(t_start, timescale)
            f.write("$dumpvars\n")
            f.write(f"#{t0}\n")
            for _, sig in pairs:
                f.write(start_vals[VCD["ids"][sig]])
            f.write("$end\n")

            current_ts = None
            for t_ps, vid, ch in VCD["events"]:
                if vid not in selected_ids:
                    continue
                ts = _scale_ps(t_ps, timescale)
                if ts != current_ts:
                    f.write(f"#{ts}\n")
                    current_ts = ts
                f.write(ch)

        dut._log.info(f"[cocotb-dbg] VCD generated: {out_path}")

    def view_vcd(path: str, *, gtkwave_path: str = "gtkwave") -> subprocess.Popen | None:
        """
        Open a VCD file in GTKWave.
        Returns the Popen handle on success, or None on failure.
        """
        out_path = _resolve_out_path(path)
        if not os.path.exists(out_path):
            dut._log.error(f"[cocotb-dbg] VCD not found: {out_path}")
            return None

        try:
            proc = subprocess.Popen(
                [gtkwave_path, out_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            dut._log.info(f"[cocotb-dbg] Opening VCD in GTKWave: {out_path}")
            return proc
        except FileNotFoundError:
            dut._log.error("[cocotb-dbg] 'gtkwave' not found in PATH.")
            return None
        except Exception as e:
            dut._log.error(f"[cocotb-dbg] Failed to launch GTKWave: {e}")
        return None



    # -------- Live GTKWave via shmidcat (commit-driven) --------
    def _live_buf_append(txt: str):
        VCD["live"]["outbuf"].extend(txt.encode("utf-8", "replace"))

    def _live_try_flush():
        fd = VCD["live"]["fd"]
        if fd is None:
            return
        buf = VCD["live"]["outbuf"]
        if not buf:
            return
        try:
            n = os.write(fd, buf)  # type: ignore
            if n > 0:
                del buf[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except BrokenPipeError:
            VCD["live"]["outbuf"].clear()
            VCD["live"]["fd"] = None

    def _live_write_header(pairs, timescale):
        from datetime import datetime
        _live_buf_append(f"$date {datetime.now()} $end\n")
        _live_buf_append("$version cocotb-dbg live-follow $end\n")
        _live_buf_append(f"$timescale 1 {timescale} $end\n")
        _live_buf_append("$scope module dut $end\n")
        for name, sig in pairs:
            w = _bv_width(sig.value)
            _live_buf_append(f"$var wire {max(1,w)} {VCD['ids'][sig]} {_vcd_safe_name(name)} $end\n")
        _live_buf_append("$upscope $end\n")
        _live_buf_append("$enddefinitions $end\n")

    def _live_write_initial_dump(pairs, timescale):
        t0_ps = int(get_sim_time(units="ps"))
        t0 = _scale_ps(t0_ps, timescale)
        _live_buf_append("$dumpvars\n")
        _live_buf_append(f"#{t0}\n")
        for _, sig in pairs:
            _live_buf_append(_fmt_change(sig, VCD["ids"][sig]))
        _live_buf_append("$end\n")

    async def _live_drain_loop():
        live = VCD["live"]
        selected_ids = live["selected_ids"]
        idx = live["drain_idx"]

        while VCD["live"]["fd"] is not None:
            await VCD_EVT.wait()
            VCD_EVT.clear()

            if (live["proc_shmidcat"] and live["proc_shmidcat"].poll() is not None) or \
               (live["proc_gtkwave"] and live["proc_gtkwave"].poll() is not None):
                VCD["live"]["fd"] = None
                VCD["live"]["outbuf"].clear()
                break

            last_ts = None
            while idx < len(VCD["events"]):
                t_ps, vid, ch = VCD["events"][idx]
                idx += 1
                if selected_ids is not None and vid not in selected_ids:
                    continue
                ts = _scale_ps(t_ps, live["timescale"])
                if ts != last_ts:
                    _live_buf_append(f"#{ts}\n")
                    last_ts = ts
                _live_buf_append(ch)

            live["drain_idx"] = idx
            _live_try_flush()

    async def gtkwave_live_start(
        *,
        signals=None,
        timescale: str = "ns",
        savefile: str | None = None,
        gtkwave_path: str = "gtkwave",
        shmidcat_path: str = "shmidcat",
        kill_existing: bool = True,
    ):
        _ensure_vcd_initialized()
        _snapshot_commit_now()  # sync VCD["last"] before writing initial dump

        if kill_existing and VCD["live"]["fd"] is not None:
            await gtkwave_live_stop(kill_viewer=True)

        pairs = VCD["pairs"]
        if signals is not None:
            sig_set = set(signals)
            selected_ids = {VCD["ids"][sig] for name, sig in pairs if name in sig_set}
        else:
            selected_ids = None
        VCD["rec_active"] = True

        try:
            sh_proc = subprocess.Popen(
                [shmidcat_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )
        except FileNotFoundError:
            dut._log.error("[cocotb-dbg] 'shmidcat' not found in PATH.")
            return

        fd = None
        if sh_proc.stdin is not None:
            try:
                fd = sh_proc.stdin.fileno()
                if fcntl is not None:
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            except Exception:
                pass

        args = [gtkwave_path, "-v", "-I"]
        if savefile:
            args.append(_resolve_out_path(savefile))
        try:
            gw_proc = subprocess.Popen(
                args,
                stdin=sh_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            dut._log.error("[cocotb-dbg] 'gtkwave' not found in PATH.")
            with contextlib.suppress(Exception):
                sh_proc.terminate()
            return

        initial_pairs = pairs
        order_vids = [VCD["ids"][s] for _, s in initial_pairs]

        VCD["live"].update({
            "proc_shmidcat": sh_proc,
            "proc_gtkwave": gw_proc,
            "fd": fd,
            "outbuf": bytearray(),
            "drain_task": None,
            "timescale": timescale,
            "drain_idx": len(VCD["events"]),
            "selected_ids": selected_ids,
            "savefile": savefile,
            "order_vids": order_vids,
        })

        _live_write_header(initial_pairs, timescale)
        _live_write_initial_dump(initial_pairs, timescale)
        _live_try_flush()

        VCD["live"]["drain_task"] = cocotb.start_soon(_live_drain_loop())
        dut._log.info("[cocotb-dbg] GTKWave live streaming started (commit-only at time-control).")

    async def gtkwave_live_stop(*, kill_viewer: bool = False):
        _snapshot_commit_now()

        live = VCD["live"]

        task = live["drain_task"]
        if task is not None:
            with contextlib.suppress(Exception):
                task.kill()
        live["drain_task"] = None

        _live_try_flush()
        if live["proc_shmidcat"] is not None and live["proc_shmidcat"].stdin is not None:
            with contextlib.suppress(Exception):
                live["proc_shmidcat"].stdin.close()
        live["fd"] = None
        live["outbuf"].clear()

        if live["proc_shmidcat"] is not None:
            with contextlib.suppress(Exception):
                live["proc_shmidcat"].terminate()
        live["proc_shmidcat"] = None

        if kill_viewer and live["proc_gtkwave"] is not None:
            with contextlib.suppress(Exception):
                live["proc_gtkwave"].terminate()
        live["proc_gtkwave"] = None

        VCD["rec_active"] = False
        dut._log.info("[cocotb-dbg] GTKWave live streaming stopped.")


    def _resolve_handle(path: str):
        """
        Resolve a 'dut.foo.bar' style path to a cocotb handle.
        If 'path' is already a handle, return it as-is.
        """
        if not isinstance(path, str):
            return path
        obj = dut
        parts = path.split(".")
        if parts and parts[0] == "dut":
            parts = parts[1:]
        for p in parts:
            if not p:
                continue
            obj = getattr(obj, p)
        return obj

    def _bitpat_match(bin_value: str, pattern: str) -> bool:
        """
        Compare a binary string to a pattern with don't-cares (x/z).
        Lengths are normalized by left-padding the shorter with zeros.
        """
        b = bin_value.lower()
        p = pattern.lower()
        if len(b) < len(p):
            b = b.rjust(len(p), "0")
        elif len(p) < len(b):
            p = p.rjust(len(b), "0")
        for ch_val, ch_pat in zip(b, p):
            if ch_pat in ("x", "z"):
                continue
            if ch_val != ch_pat:
                return False
        return True

    async def _maybe_await(x):
        if inspect.isawaitable(x):
            return await x
        return x


    def _compile_update_snippet(snippet: str) -> Callable[[], Awaitable[Any]]:
        """
        Compile a string snippet into an async callable using the same wrapper
        used by the REPL so that 'await' is allowed inside.
        """
        wrapper = wrap_code_as_async(snippet)
        locs: dict[str, Any] = {}
        # Use the REPL globals so user variables persist across calls
        exec(wrapper, exec_globals, locs)
        async def _runner():
            res = locs["__user_main__"]()
            return await _maybe_await(res)
        return _runner

    async def _advance_once(granularity, clk_param=None):
        """
        Advance time by one unit according to granularity.
        Returns the number of *cycles* consumed (0/1) for accounting.
        """
        if granularity == "delta":
            await step()
            return 0

        if isinstance(granularity, tuple) and len(granularity) == 3 and granularity[0] == "time":
            _, val, units = granularity
            await run_for(val, units, freeze="preserve", clk=clk_param)
            return 0

        domain = _get_clk(clk_param)

        if granularity == "edge":
            clkh = domain["handle"]
            if clkh is None:
                half_ps = int(round(float(domain["period_ns"]) * 1_000.0 / 2.0))
                await _temp_free_run_for_ps(half_ps, freeze="preserve", clk=clk_param)
                return 0
            await RisingEdge(clkh)
            return 0

        # default: "cycle"
        if domain["task"] is not None:
            clkx = domain["handle"]
            if clkx is None:
                await _temp_free_run_for_ps(int(round(float(domain["period_ns"]) * 1_000.0)), freeze="preserve", clk=clk_param)
                return 1
            await RisingEdge(clkx)
            await FallingEdge(clkx)
            return 1

        await cycles(1, clk=clk_param)
        return 1

    async def _eval_state_spec_one(spec) -> bool:
        """
        Evaluate a single state specification:
        - callable -> truthy
        - dict -> all pairs match (value, pattern or masked)
        """
        if callable(spec):
            val = spec()
            return bool(await _maybe_await(val))

        if isinstance(spec, dict):
            for k, want in spec.items():
                sig = _resolve_handle(k)
                if isinstance(want, tuple) and len(want) == 3 and str(want[1]).lower() == "mask":
                    val = as_uint(sig)
                    masked = val & int(want[2])
                    if masked != (int(want[0]) & int(want[2])):
                        return False
                elif isinstance(want, str):
                    try:
                        binstr = sig.value.binstr
                    except Exception:
                        binstr = format(as_uint(sig), "b")
                    if not _bitpat_match(binstr, want):
                        return False
                else:
                    if as_uint(sig) != int(want):
                        return False
            return True

        raise TypeError(f"Unsupported state spec type: {type(spec)!r}")

    async def _eval_states(states, mode: str) -> tuple[bool, int | None]:
        """
        Evaluate one or many states according to mode='any'/'all'.
        Returns (matched, which_index_or_None).
        """
        if isinstance(states, (list, tuple)):
            if mode == "all":
                for st in states:
                    ok = await _eval_state_spec_one(st)
                    if not ok:
                        return False, None
                return True, None
            else:
                for i, st in enumerate(states):
                    ok = await _eval_state_spec_one(st)
                    if ok:
                        return True, i
                return False, None
        else:
            ok = await _eval_state_spec_one(states)
            return bool(ok), (0 if ok else None)

    async def wait_until(
        states,
        update=None,
        *,
        mode: str = "any",
        granularity: object = "cycle",
        clk=None,
        timeout: tuple[object, str] | None = None,
        max_cycles: int | None = None,
        max_iters: int | None = None,
    ):
        """
        Wait until 'states' is satisfied. Optionally run 'update' each iteration.
        Ctrl-C on the client sends ':cancel' which sets dbg.cancel_evt; we detect it here and exit cleanly.
        """
        if isinstance(update, str):
            update_fn = _compile_update_snippet(update)
        elif update is None:
            update_fn = None
        elif callable(update):
            if inspect.iscoroutinefunction(update):
                async def _afn():
                    return await update()
                update_fn = _afn
            else:
                async def _sfn():
                    return update()
                update_fn = _sfn
        else:
            raise TypeError("update must be None, a callable, or a string snippet")

        with contextlib.suppress(Exception):
            dbg.cancel_evt.clear()

        deadline_ps = None
        if timeout is not None:
            tval, tunits = timeout
            deadline_ps = int(get_sim_time(units="ps")) + _to_total_ps(tval, tunits)

        iter_count = 0
        cycle_count = 0

        matched, which = await _eval_states(states, mode)
        if matched:
            return {
                "hit": True,
                "which": which,
                "iters": iter_count,
                "cycles": cycle_count,
                "time_ps": int(get_sim_time(units="ps")),
            }

        while True:
            if getattr(dbg, "cancel_evt", None) is not None and dbg.cancel_evt.is_set():
                dbg.cancel_evt.clear()
                return {
                    "hit": False,
                    "which": None,
                    "iters": iter_count,
                    "cycles": cycle_count,
                    "time_ps": int(get_sim_time(units="ps")),
                    "cancelled": True,
                }

            iter_count += 1
            if max_iters is not None and iter_count > max_iters:
                return {
                    "hit": False,
                    "which": None,
                    "iters": iter_count - 1,
                    "cycles": cycle_count,
                    "time_ps": int(get_sim_time(units="ps")),
                }

            cycle_count += await _advance_once(granularity, clk)

            if update_fn is not None:
                await update_fn()

            matched, which = await _eval_states(states, mode)
            if matched:
                return {
                    "hit": True,
                    "which": which,
                    "iters": iter_count,
                    "cycles": cycle_count,
                    "time_ps": int(get_sim_time(units="ps")),
                }

            # Limits
            if max_cycles is not None and cycle_count >= max_cycles:
                return {
                    "hit": False,
                    "which": None,
                    "iters": iter_count,
                    "cycles": cycle_count,
                    "time_ps": int(get_sim_time(units="ps")),
                }
            if deadline_ps is not None and int(get_sim_time(units="ps")) >= deadline_ps:
                return {
                    "hit": False,
                    "which": None,
                    "iters": iter_count,
                    "cycles": cycle_count,
                    "time_ps": int(get_sim_time(units="ps")),
                }

    # -------- REPL env --------
    exec_globals = {
        # DUT + cocotb primitives
        "dut": dut,
        "clk": clk_handle,
        "cocotb": cocotb,
        "Timer": Timer,
        "RisingEdge": RisingEdge,
        "FallingEdge": FallingEdge,
        "Clock": Clock,
        "BinaryValue": BinaryValue,

        # time/clock control
        "start_clock": start_clock,
        "stop_clock": stop_clock,
        "set_clock_rate": set_clock_rate,
        "step": step,
        "run_for": run_for,
        "cycles": cycles,

        # comparison helpers
        "as_uint": as_uint,
        "as_sint": as_sint,

        # VCD control
        "start_vcd": start_vcd,
        "stop_vcd": stop_vcd,
        "gen_vcd": gen_vcd,
        "view_vcd": view_vcd,

        "gtkwave_live_start": gtkwave_live_start,
        "gtkwave_live_stop": gtkwave_live_stop,

        # wait-until features
        "wait_until": wait_until,
    }

    def register_clock(name: str, signal, period_ns: float | None = None):
        """Register a named clock domain. signal can be a DUT handle or attribute name string."""
        if isinstance(signal, str):
            signal = getattr(dut, signal, None)
        CLOCKS[name] = {
            "handle": signal,
            "task": None,
            "period_ns": float(period_ns) if period_ns is not None else _CLK_PERIOD_NS,
        }

    def show_clocks():
        """Print all registered clock domains and their current state."""
        for name, domain in CLOCKS.items():
            h = domain["handle"]
            sig_name = getattr(h, "_name", "?") if h is not None else "None"
            running = domain["task"] is not None
            print(f"  {name!r}: signal={sig_name}, period={domain['period_ns']}ns, running={running}")

    async def stop_all_clocks(freeze: str = "low"):
        """Stop all running clock domains."""
        for name in list(CLOCKS):
            await stop_clock(freeze=freeze, clk=name)

    def sim_time(units: str = "ns"):
        """Return current simulation time."""
        return get_sim_time(units=units)

    def show_sigs():
        _ensure_vcd_initialized()
        for name, sig in VCD["pairs"]:
            try:
                print(f"  {name} = {sig.value}")
            except Exception:
                print(f"  {name} = <unreadable>")

    def show_vars():
        user = {k: v for k, v in exec_globals.items()
                if k not in _initial_exec_names and not k.startswith("_")}
        if not user:
            print("  (none)")
        else:
            for k, v in sorted(user.items()):
                print(f"  {k} = {repr(v)}")

    def list_signals():
        _ensure_vcd_initialized()
        return [name for name, _ in VCD["pairs"]]

    exec_globals.update({
        "show_sigs": show_sigs, "show_vars": show_vars, "list_signals": list_signals,
        "register_clock": register_clock, "stop_all_clocks": stop_all_clocks, "show_clocks": show_clocks,
        "sim_time": sim_time,
    })
    _initial_exec_names = frozenset(exec_globals)

    def _cmpwrap(op):
        def _impl(self, other):
            try:
                return op(as_uint(self), as_uint(other))
            except Exception:
                return NotImplemented
        return _impl

    for _cls in (c for c in (BinaryValue, ModifiableObject) if c is not None):
        _cls.__lt__ = _cmpwrap(operator.lt)
        _cls.__le__ = _cmpwrap(operator.le)
        _cls.__gt__ = _cmpwrap(operator.gt)
        _cls.__ge__ = _cmpwrap(operator.ge)
        # keep existing eq/ne if they exist; else provide them
        if getattr(_cls, "__eq__", object.__eq__) is object.__eq__:
            _cls.__eq__ = _cmpwrap(operator.eq)
        if getattr(_cls, "__ne__", object.__ne__) is object.__ne__:
            _cls.__ne__ = _cmpwrap(operator.ne)

    async def executor_loop():
        while True:
            try:
                code, reply_q, addr = dbg.cmd_q.get_nowait()
            except queue.Empty:
                await Timer(1, units="step")
                continue
            code = format_user_input(code)
            wrapper = wrap_code_as_async(code)
            exec_locals = {}
            out = io.StringIO()
            try:
                exec(wrapper, exec_globals, exec_locals)
                with contextlib.suppress(Exception):
                    dbg.cancel_evt.clear()
                coro = exec_locals["__user_main__"]()
                with contextlib.redirect_stdout(out):
                    result = await coro
                
                # If last expression produced an awaitable (coroutine/Trigger/Future), auto-await it.
                if inspect.isawaitable(result):
                    result = await result

                # settle a couple of deltas for assignment effects (no VCD write here)
                await Timer(1, units="step")
                await Timer(1, units="step")

                printed = out.getvalue()
                if printed:
                    payload = ["[OK]", "## stdout:", printed.rstrip("\n"), "## return:", _fmt_result(result)]
                else:
                    payload = [f"[OK] {_fmt_result(result)}"]
                msg = "\n".join(payload)

            except Exception:
                with contextlib.suppress(Exception):
                    await Timer(1, units="step")
                    await Timer(1, units="step")
                printed = out.getvalue()
                if printed:
                    payload = ["[ERROR]", "## stdout (partial):", printed.rstrip("\n"), "## traceback:", format_exception().rstrip("\n")]
                else:
                    payload = [f"[ERROR] {format_exception().rstrip()}"]
                msg = "\n".join(payload)

            reply_q.put(msg)

    def _fmt_result(val):
        """
        Pretty-print values for compact single-line form (no deprecation warnings).
        """
        # BinaryValue first
        if isinstance(val, BinaryValue) or hasattr(val, "binstr"):
            try:
                return val.binstr
            except Exception:
                pass
            try:
                return str(val.integer)
            except Exception:
                pass
        # Cocotb handle: use its .value
        if hasattr(val, "value"):
            try:
                v = val.value
                bs = getattr(v, "binstr", None)
                if bs is not None:
                    return bs
                try:
                    return str(v.integer)
                except Exception:
                    return str(v)
            except Exception:
                pass
        # String or fall back
        if isinstance(val, str):
            return val
        try:
            return str(val)
        except Exception:
            return repr(val)

    # Initialize VCD catalog immediately (no events yet)
    _ensure_vcd_initialized()
    cocotb.start_soon(executor_loop())

    # Keep the test alive WITHOUT advancing time (wait on an Event we never set)
    never = Event()
    await never.wait()
