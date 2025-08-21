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

DBG_HOST = "127.0.0.1"
DBG_PORT = 9999

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

    # Clock/run control exposed in the REPL
    CLK = {"task": None, "period_ns": 10.0}  # default 100 MHz

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
                await Timer(int(count), units=unit)

    async def start_clock(period_ns: float | None = None):
        """
        Start a FREE-RUNNING clock on dut.clk with given period (ns).
        WARNING: Once started, time will advance autonomously until stop_clock().
        """
        p = float(period_ns if period_ns is not None else CLK["period_ns"])
        if CLK["task"] is not None:
            CLK["task"].kill()
            CLK["task"] = None
        CLK["period_ns"] = p
        CLK["task"] = cocotb.start_soon(Clock(dut.clk, p, units="ns").start())

    async def stop_clock(freeze: str = "low"):
        """
        Stop the free-running clock (if running) and freeze level.
        freeze: "low" (default), "high", or "preserve"
        """
        if CLK["task"] is not None:
            CLK["task"].kill()
            CLK["task"] = None
        if freeze == "low":
            with contextlib.suppress(Exception):
                dut.clk.value = 0
        elif freeze == "high":
            with contextlib.suppress(Exception):
                dut.clk.value = 1
        # "preserve" => leave current level

    async def set_clock_rate(period_value, units: str = "ns"):
        """
        Set the default clock period used by run_for / cycles.
        Leaves the clock STOPPED (frozen low for determinism).
        """
        await stop_clock(freeze="low")
        CLK["period_ns"] = _to_ns(period_value, units)

    async def step():
        """Advance exactly one delta (no time advance)."""
        await Timer(1, units="step")

    async def cycles(n: int, period_ns: float | None = None):
        """
        Drive EXACTLY n clock cycles *manually* (no background run afterwards).
        - If a free-running clock is active, stop it first (avoid double-driving).
        - period_ns defaults to CLK["period_ns"].
        - Uses integer picoseconds throughout, so it's drift-free.
        """
        if CLK["task"] is not None:
            await stop_clock()

        p_ns = float(CLK["period_ns"] if period_ns is None else period_ns)
        period_ps = int(round(p_ns * 1_000.0))  # 1 ns = 1000 ps
        half_ps = period_ps // 2
        first_half_ps = half_ps + (period_ps - 2 * half_ps)
        second_half_ps = half_ps

        with contextlib.suppress(Exception):
            dut.clk.value = 0

        for _ in range(int(n)):
            await _await_time_ps(first_half_ps)
            dut.clk.value = 1
            await _await_time_ps(second_half_ps)
            dut.clk.value = 0

    async def _temp_free_run_for_ps(total_ps: int, freeze: str = "preserve"):
        """
        Phase-safe temporary free run:
          - Starts at clk=0.
          - Manually toggles using the configured period for at most total_ps.
          - Guarantees NO rising edge if total_ps < half-period.
          - At the end, 'freeze' controls the final level: "preserve" | "low" | "high".
        """
        if total_ps <= 0:
            return

        p_ns = float(CLK["period_ns"])
        period_ps = int(round(p_ns * 1_000.0))  # 1 ns = 1000 ps
        half_ps = period_ps // 2
        first_half_ps = half_ps + (period_ps - 2 * half_ps)
        second_half_ps = half_ps

        # start from a known phase
        lvl = 0
        with contextlib.suppress(Exception):
            dut.clk.value = lvl

        rem = int(total_ps)
        while rem > 0:
            # first half, no rising edge if we can't fit it
            if rem < first_half_ps:
                await _await_time_ps(rem)
                break
            await _await_time_ps(first_half_ps)
            lvl = 1
            dut.clk.value = lvl
            rem -= first_half_ps

            # second half, if we can't fit it, we stop high
            if rem < second_half_ps:
                await _await_time_ps(rem)
                break
            await _await_time_ps(second_half_ps)
            lvl = 0
            dut.clk.value = lvl
            rem -= second_half_ps

        # final freeze policy
        if freeze == "low" and lvl != 0:
            dut.clk.value = 0 # may create a zero-time falling edge
        elif freeze == "high" and lvl != 1:
            dut.clk.value = 1 # may create a zero-time rising edge
        # "preserve" => do nothing

    async def run_for(t, units: str = "ns", freeze: str = "preserve"):
        """
        Advance real time by t, ALWAYS updating the design with clock edges.
          - If a free-running clock is active: just await t (edges come from it).
          - Else: run a phase-safe temporary clock for exactly t, then freeze as requested.
        freeze: "preserve" (default), "low", or "high"
        """
        total_ps = _to_total_ps(t, units)

        if CLK["task"] is not None:
            await _await_time_ps(total_ps)
            return

        await _temp_free_run_for_ps(total_ps, freeze=freeze)

    ### Comparison helpers + auto-coercing comparisons ###
    def as_uint(x):
        """
        Unsigned int view of a cocotb handle or BinaryValue (raises on X/Z).
        """
        v = getattr(x, "value", x)
        if hasattr(v, "integer"):
            return int(v.integer)
        return int(v)

    def as_sint(x):
        """
        Signed int view (uses BinaryValue.signed_integer if available).
        """
        v = getattr(x, "value", x)
        if hasattr(v, "signed_integer"):
            return int(v.signed_integer)
        return int(v)

    VCD = {
        "pairs": [], # [(hier_name, handle)]
        "ids": {}, # handle -> vcd_id
        "events": [], # [(time_ps: int, vcd_id: str, change_line: str)]
        "rec_active": True,
        "mark_start_ps": None,
        "mark_stop_ps":  None,
        "selected_ids": None,
    }

    def _resolve_out_path(path: str) -> str:
        base = os.environ.get("COCODBG_ROOT", os.getcwd())
        return path if os.path.isabs(path) else os.path.join(base, path)

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

    async def _monitor_signal(sig: ModifiableObject, vcd_id: str):
        """
        Append (time_ps, id, change_line) on each edge if recording is active
        """
        while True:
            await Edge(sig)
            if not VCD["rec_active"]:
                continue
            t_ps = int(get_sim_time(units="ps"))
            VCD["events"].append((t_ps, vcd_id, _fmt_change(sig, vcd_id)))

    def _ensure_vcd_monitors_initialized():
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

        t_ps = int(get_sim_time(units="ps"))
        for _, sig in pairs:
            change = _fmt_change(sig, ids[sig])
            VCD["events"].append((t_ps, ids[sig], change))

        for _, sig in pairs:
            cocotb.start_soon(_monitor_signal(sig, ids[sig]))

    async def start_vcd(signals: list[ModifiableObject] | None = None):
        """
        Start (or restart) a VCD recording window from NOW.
        If 'signals' is provided, limit future gen_vcd() to those signals.
        """
        _ensure_vcd_monitors_initialized()
        VCD["rec_active"] = True
        VCD["mark_start_ps"] = int(get_sim_time(units="ps"))
        VCD["mark_stop_ps"] = None
        if signals is None:
            VCD["selected_ids"] = None
        else:
            # map provided handles to existing ids; unknown handles are ignored
            selected = set()
            for s in signals:
                vid = VCD["ids"].get(s)
                if vid:
                    selected.add(vid)
            VCD["selected_ids"] = selected

    async def stop_vcd():
        """
        Stop recording (events after this call are not captured).
        Window end is marked at NOW.
        """
        _ensure_vcd_monitors_initialized()
        VCD["rec_active"] = False
        VCD["mark_stop_ps"] = int(get_sim_time(units="ps"))

    async def gen_vcd(path: str, *, timescale: str = "ns"):
        """
        Generate a VCD file.
        ----
        If `start_vcd()` was called the VCD will be from that moment to `stop_vcd()` (or NOW if it was not called).
        Otherwise from the beginning (t=0) to NOW.
        """
        _ensure_vcd_monitors_initialized()
        out_path = _resolve_out_path(path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        if VCD["mark_start_ps"] is not None:
            t_start = VCD["mark_start_ps"]
        else:
            t_start = VCD["events"][0][0] if VCD["events"] else 0
        t_end = VCD["mark_stop_ps"]
        if t_end is None:
            t_end = int(get_sim_time(units="ps"))

        # Build signal list to dump
        if VCD["selected_ids"] is None:
            pairs = VCD["pairs"]
            selected_ids = set(VCD["ids"][sig] for _, sig in pairs)
        else:
            selected_ids = set(VCD["selected_ids"])
            pairs = [(n, s) for (n, s) in VCD["pairs"] if VCD["ids"][s] in selected_ids]

        # Precompute starting values at t_start
        # Walk through events up to t_start and remember last value per id
        start_vals = {}
        for t_ps, vid, ch in VCD["events"]:
            if vid not in selected_ids:
                continue
            if t_ps > t_start:
                break
            start_vals[vid] = ch

        # If any id lacks a prior event, synthesize from current value
        for _, sig in pairs:
            vid = VCD["ids"][sig]
            if vid not in start_vals:
                start_vals[vid] = _fmt_change(sig, vid)

        # Write the VCD
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

            # Initial dump at start time
            t0 = _scale_ps(t_start, timescale)
            f.write("$dumpvars\n")
            f.write(f"#{t0}\n")
            for _, sig in pairs:
                vid = VCD["ids"][sig]
                f.write(start_vals[vid])
            f.write("$end\n")

            # Changes in (t_start, t_end]
            current_time_scaled = None
            for t_ps, vid, ch in VCD["events"]:
                if vid not in selected_ids:
                    continue
                if t_ps <= t_start or t_ps > t_end:
                    continue
                ts = _scale_ps(t_ps, timescale)
                if ts != current_time_scaled:
                    f.write(f"#{ts}\n")
                    current_time_scaled = ts
                f.write(ch)

        dut._log.info(f"[cocotb-dbg] VCD generated: {out_path}")

    # REPL environment: names injected into every snippet
    exec_globals = {
        # DUT + cocotb primitives
        "dut": dut,
        "clk": dut.clk,
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
    }

    # Compare DUT elements (unsigned for now)
    # TODO: Add signed comparison
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
        """
        Process commands and run them inside the simulation.
        """
        while True:
            try:
                code, reply_q, addr = dbg.cmd_q.get_nowait()
            except queue.Empty:
                # Idle: a single delta keeps the loop responsive without moving time.
                await Timer(1, units="step")
                continue
            code = format_user_input(code)
            wrapper = wrap_code_as_async(code)
            exec_locals = {}
            out = io.StringIO()
            try:
                exec(wrapper, exec_globals, exec_locals)
                coro = exec_locals["__user_main__"]()
                with contextlib.redirect_stdout(out):
                    result = await coro
                
                # If last expression produced an awaitable (coroutine/Trigger/Future), auto-await it.
                if inspect.isawaitable(result):
                    result = await result

                printed = out.getvalue()
                if printed:
                    payload = ["[OK]", "## stdout:", printed.rstrip("\n"), "## return:", _fmt_result(result)]
                else:
                    payload = [f"[OK] {_fmt_result(result)}"]
                msg = "\n".join(payload)

            except Exception:
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

    _ensure_vcd_monitors_initialized()
    cocotb.start_soon(executor_loop())

    # Keep the test alive WITHOUT advancing time (wait on an Event we never set)
    never = Event()
    await never.wait()
