#####################################################################################
# TODO: FIX clk incrementing when we send commands (seems to be 1 step per command) #
#####################################################################################
import cocotb
from cocotb.triggers import Timer, RisingEdge, FallingEdge, Event
from cocotb.clock import Clock
from cocotb.binary import BinaryValue

from cocodbg import CocotbDebugger, wrap_code_as_async, format_exception

import queue
import io
import contextlib

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

    # ---- Clock control & run-control (exposed to REPL) ----
    CLK = {"task": None, "period_ns": 10.0}  # default 100 MHz

    async def start_clock(period_ns: float | None = None):
        """
        Start a free-running clock on dut.clk with given period (ns).
        """
        p = float(period_ns if period_ns is not None else CLK["period_ns"])
        if CLK["task"] is not None:
            CLK["task"].kill()
            CLK["task"] = None
        CLK["period_ns"] = p
        CLK["task"] = cocotb.start_soon(Clock(dut.clk, p, units="ns").start())

    async def stop_clock():
        """
        Stop the free-running clock.
        """
        if CLK["task"] is not None:
            CLK["task"].kill()
            CLK["task"] = None

    async def step():
        """
        Advance exactly one delta cycle (no time advance).
        """
        await Timer(1, units="step")

    async def run_for(t, units: str = "ns", step_ns: float | None = None):
        """
        Advance time by t in given units (default: ns).
        """
        if units == "ns":
            chunk = 0.1 if step_ns is None else float(step_ns)
            remaining = float(t)
            while remaining > 0:
                dt = chunk if remaining >= chunk else remaining
                await Timer(dt, units="ns")
                remaining -= dt
        else:
            await Timer(t, units=units)

    async def cycles(n: int):
        """
        Advance exactly n clock cycles (requires a running clock).
        """
        for _ in range(int(n)):
            await RisingEdge(dut.clk)

    # Start a default clock so RisingEdge(dut.clk) can be used when YOU let time advance.
    # With no auto-idle progression, the clock will not toggle unless time is moved by commands.
    cocotb.start_soon(Clock(dut.clk, CLK["period_ns"], units="ns").start())

    # REPL environment: names injected into every command.
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

        # time control
        "start_clock": start_clock,
        "stop_clock": stop_clock,
        "step": step,
        "run_for": run_for,
        "cycles": cycles,
    }

    async def executor_loop():
        """
        Process commands and run them inside the simulation.
        """
        while True:
            try:
                code, reply_q, addr = dbg.cmd_q.get_nowait()
            except queue.Empty:
                # DO NOT SCHEDULE DELTAS HERE, just yield once and loop (no time advance, no starvation)
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

                printed = out.getvalue()
                if printed:
                    payload = ["[OK]"]
                    payload.append("## stdout:")
                    payload.append(printed.rstrip("\n"))
                    payload.append("## return:")
                    payload.append(_fmt_result(result))
                else:
                    payload = [f"[OK] {_fmt_result(result)}"]

                msg = "\n".join(payload)

            except Exception:
                printed = out.getvalue()
                if printed:
                    payload = ["[ERROR]"]
                    payload.append("## stdout (partial):")
                    payload.append(printed.rstrip("\n"))
                    payload.append("## traceback:")
                    payload.append(format_exception().rstrip("\n"))
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

    cocotb.start_soon(executor_loop())

    # Keep the test alive WITHOUT advancing time or scheduling endless deltas.
    # It waits on an Event that we never set.
    # This avoids starving timed triggers while ensuring the test never returns.
    never = Event()
    await never.wait()
