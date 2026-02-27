# cocodbg

Interactive debugger for [cocotb](https://github.com/cocotb/cocotb) simulations. Attach a live Python REPL to a running HDL simulation: read/write signals, advance the clock, record VCDs, all without recompiling.

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [CocotbDebugConfig reference](#cocotbdebugconfig-reference)
- [Existing tests/ workflow](#existing-tests-workflow)
- [REPL usage](#repl-usage)
  - [Colon commands](#colon-commands)
  - [Time and clock control](#time-and-clock-control)
  - [Signal access](#signal-access)
  - [Waiting for conditions](#waiting-for-conditions)
  - [VCD recording](#vcd-recording)
  - [Multiple clock domains](#multiple-clock-domains)
  - [Variables and inspection](#variables-and-inspection)
  - [Cocotb primitives](#cocotb-primitives)
  - [Step-back and replay](#step-back-and-replay)

---

## Installation

From source:

```
pip install -e .
```

---

## Quick start

Drop a script next to your HDL sources and run it:

```python
from cocodbg import CocotbDebugConfig

CocotbDebugConfig(
    toplevel="adder",
    sources=["adder.vhd"],
    sim="ghdl",
    lang="vhdl",
    clk_signal="clk",
    clk_period_ns=10.0,
).run()
```

`CocotbDebugConfig` writes the Makefile and cleans it up on exit. A REPL opens in your terminal:

```
# cocotb-dbg ready. :quit to exit.
>>>
```

---

## CocotbDebugConfig reference

```python
CocotbDebugConfig(
    toplevel,
    sources,
    *,
    sim="ghdl",
    lang="vhdl",
    clk_signal="clk",
    clk_period_ns=10.0,
    host="127.0.0.1",
    port=9999,
    extra_args=None,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `toplevel` | `str` | required | Name of the top-level HDL entity/module |
| `sources` | `list[str]` | required | Source file paths (absolute or relative to CWD) |
| `sim` | `str` | `"ghdl"` | Simulator (`"ghdl"`, `"icarus"`, `"verilator"`, etc.) |
| `lang` | `str` | `"vhdl"` | HDL language (`"vhdl"`, `"verilog"`, `"sv"`) |
| `clk_signal` | `str` | `"clk"` | Name of the default clock signal on the DUT |
| `clk_period_ns` | `float` | `10.0` | Default clock period in nanoseconds |
| `host` | `str` | `"127.0.0.1"` | Debugger host |
| `port` | `int` | `9999` | Debugger port |
| `extra_args` | `dict[str, str]` | `None` | Extra Makefile variables passed to the simulator |

`extra_args` keys and values are written directly as Makefile variables:

```python
# GHDL with VHDL 2008
CocotbDebugConfig(..., extra_args={"GHDL_ARGS": "--std=08"})

# Icarus with a define
CocotbDebugConfig(..., extra_args={"COMPILE_ARGS": "-DDEBUG=1"})

# Multiple flags
CocotbDebugConfig(..., extra_args={
    "GHDL_ARGS": "--std=08",
    "SIM_ARGS": "--vcd=out.vcd",
})
```

---

## Existing tests/ workflow

Already have a `tests/` directory with a cocotb `Makefile`? Add one line:

```makefile
MODULE = cocodbg.executor
```

Then launch with:

```
cocodbg [dir] [--host HOST] [--port PORT]
```

or:

```
python -m cocodbg [dir] [--host HOST] [--port PORT]
```

| Argument | Default | Description |
|---|---|---|
| `dir` | `tests` | Directory containing the cocotb `Makefile` |
| `--host` | `127.0.0.1` | Debugger host |
| `--port` | `9999` | Debugger port |

The executor inside the simulation reads its configuration from environment variables. Set these in your shell before running `cocodbg` if the defaults don't fit:

| Variable | Default | Description |
|---|---|---|
| `COCODBG_HOST` | `127.0.0.1` | Must match `--host` |
| `COCODBG_PORT` | `9999` | Must match `--port` |
| `COCODBG_CLK_SIGNAL` | `clk` | Name of the default clock signal on the DUT |
| `COCODBG_CLK_PERIOD_NS` | `10.0` | Default clock period in nanoseconds |

---

## REPL usage

Type Python at the `>>>` prompt. Single lines submit on Enter. Lines ending with `:` open a block; close it with a blank line. Open brackets (`(`, `[`, `{`) work the same way; the REPL waits until they close.

```python
>>> dut.a.value = 5
>>> dut.b.value = 3
>>> await cycles(2)
>>> print(dut.sum.value)
8
```

`^C` cancels the running block and drops back to the prompt. The simulation keeps going.

Tab completion works for built-in names, DUT signal paths (`dut.`), and colon commands. History saves to `~/.cocodbg_history`.

---

### Colon commands

| Command | Description |
|---|---|
| `:q` / `:quit` | Stop simulation and exit |
| `:r` | Restart simulation, replay all previous inputs |
| `:stepback N` / `:rewind N` | Restart and replay all but the last N submissions |
| `:n` | Advance one simulation delta (no time advance) |
| `:c` | Advance one clock cycle |
| `:c N` | Advance N clock cycles |
| `:c N unit` | Advance N of `unit` time (e.g. `:c 100 ns`) |
| `:b <expr>` | Wait until `lambda: <expr>` is true |
| `:p <expr>` | Evaluate and print expression |
| `:vars` | Show user-defined REPL variables |
| `:sig` | Show all DUT signals and current values |
| `:clear` / `:cls` | Clear the terminal |
| `:help` / `:h` | Show in-REPL help |

---

### Time and clock control

All functions in this table are `async` and must be awaited, except `sim_time` which is synchronous.

```python
>>> await cycles(10)          # advance 10 clock cycles
>>> await run_for(100, "ns")  # advance 100 ns
>>> await step()              # advance one simulation delta
>>> print(sim_time("ns"))     # print current simulation time
```

| Function | Description |
|---|---|
| `cycles(n, period_ns=None, clk=None)` | Drive N clock cycles manually; stops any free-running clock on that domain first. `period_ns` overrides the domain's period for this call |
| `run_for(t, units="ns", freeze="preserve", clk=None)` | Advance by real time. If a free-running clock is active, just waits; otherwise drives a temporary clock and `freeze` controls the final level (`"low"`, `"high"`, `"preserve"`) |
| `step()` | Advance one simulation delta (no time advance) |
| `sim_time(units="ns")` | Return current simulation time |
| `start_clock(period_ns=None, clk=None)` | Start a free-running clock; time advances autonomously until `stop_clock()` |
| `stop_clock(freeze="low", clk=None)` | Stop the clock (`freeze`: `"low"`, `"high"`, `"preserve"`) |
| `set_clock_rate(period, units="ns", clk=None)` | Change clock period; stops the clock and freezes it low |

---

### Signal access

```python
>>> dut.a.value = 42            # write a signal
>>> print(dut.sum.value)        # read a signal
>>> print(as_uint(dut.result))  # read as unsigned int
>>> print(as_sint(dut.result))  # read as signed int (two's complement)
```

| Function | Description |
|---|---|
| `as_uint(x)` | Unsigned integer view of a signal or BinaryValue |
| `as_sint(x)` | Signed integer view (two's complement) |

Comparison operators (`<`, `<=`, `>`, `>=`, `==`, `!=`) work directly on cocotb handles and BinaryValues.

---

### Waiting for conditions

```python
>>> await wait_until(lambda: dut.ready.value == 1)

>>> await wait_until({"dut.ready": 1, "dut.valid": 1})

>>> result = await wait_until(
...     lambda: dut.done.value == 1,
...     max_cycles=1000,
...     timeout=(500, "ns"),
... )
>>> print(result)  # {"hit": True, "cycles": 42, "time_ps": 420000, ...}

# Wait until either condition is true (mode="any"), return which one triggered
>>> result = await wait_until(
...     [lambda: dut.err.value == 1, lambda: dut.done.value == 1],
...     mode="any",
... )
>>> print(result["which"])  # 0 = err fired, 1 = done fired

# Dict spec: binary pattern with don't-cares
>>> await wait_until({"dut.state": "01xx"})   # low 2 bits are don't-care

# Dict spec: masked comparison
>>> await wait_until({"dut.flags": (0b0100, "mask", 0b0100)})  # bit 2 must be set

```

| Parameter | Description |
|---|---|
| `states` | Single condition or list of conditions. Each condition is a callable or a `{signal_path: expected_value}` dict (multiple keys are ANDed) |
| `update` | Optional callable or code snippet run each iteration |
| `mode` | `"any"` (default) or `"all"`: how a list of conditions is evaluated |
| `granularity` | `"cycle"` (default), `"edge"` (rising edge), `"delta"`, or `("time", val, unit)` |
| `clk` | Clock domain name or handle |
| `timeout` | `(value, unit)` tuple, e.g. `(500, "ns")` |
| `max_cycles` | Stop after N cycles regardless |
| `max_iters` | Stop after N iterations regardless |

Dict condition values can be:
- An integer: exact match via `as_uint`
- A binary string like `"01xx"`: pattern match, with `x`/`z` as don't-cares
- A tuple `(value, "mask", mask)`: masked comparison, `(signal & mask) == (value & mask)`

Returns a dict: `{"hit": bool, "which": int|None, "cycles": int, "iters": int, "time_ps": int}`. `which` is the index of the first condition that matched when `mode="any"`; it is always `None` for `mode="all"`. If `^C` is pressed mid-wait, the call returns early with `"hit": False` and an extra `"cancelled": True` key.

---

### VCD recording

#### File VCD

```python
>>> await start_vcd()           # start a fresh recording window (optional)
>>> await cycles(20)
>>> await stop_vcd()            # stop recording (optional)
>>> await gen_vcd("trace.vcd")  # write to file
```

The VCD records everything from simulation start by default. `start_vcd()` resets the recording window to now.

```python
# Record only specific signals
>>> await gen_vcd("trace.vcd", signals=["clk", "a", "sum"])
```

#### Live GTKWave

Requires `shmidcat` and `gtkwave` in PATH (override with `shmidcat_path`/`gtkwave_path` if needed).

```python
>>> await gtkwave_live_start()
>>> await cycles(100)
>>> await gtkwave_live_stop()

# Record only specific signals
>>> await gtkwave_live_start(signals=["clk", "a", "sum"])

# Load a GTKWave save file on open
>>> await gtkwave_live_start(savefile="my_signals.gtkw")
```

| Function | Description |
|---|---|
| `start_vcd()` | Reset the recording window to now; clear all recorded events |
| `stop_vcd()` | Stop recording |
| `gen_vcd(path, signals=None, timescale="ns")` | Write VCD to file |
| `view_vcd(path, gtkwave_path="gtkwave")` | Open a static VCD file in GTKWave |
| `gtkwave_live_start(signals=None, timescale="ns", savefile=None, gtkwave_path="gtkwave", shmidcat_path="shmidcat")` | Start live stream to GTKWave; `gtkwave_path`/`shmidcat_path` override binary locations if they aren't in PATH |
| `gtkwave_live_stop(kill_viewer=False)` | Stop live stream |

---

### Multiple clock domains

```python
>>> register_clock("fast", dut.clk_fast, period_ns=2.0)
>>> register_clock("slow", dut.clk_slow, period_ns=20.0)

>>> await start_clock(clk="fast")
>>> await start_clock(clk="slow")

>>> await cycles(4, clk="fast")
>>> await run_for(40, "ns", clk="slow")

>>> show_clocks()
  'default': signal=clk, period=10.0ns, running=False
  'fast': signal=clk_fast, period=2.0ns, running=True
  'slow': signal=clk_slow, period=20.0ns, running=True

>>> await stop_all_clocks()
```

| Function | Description |
|---|---|
| `register_clock(name, signal, period_ns=None)` | Register a named clock domain (`signal` can be a handle or attribute name string) |
| `show_clocks()` | Print all clock domains and their state |
| `stop_all_clocks(freeze="low")` | Stop all running clocks; `freeze` has the same meaning as in `stop_clock` |

The `clk=` parameter on all time-control functions accepts a domain name string, a raw DUT handle, or `None` for the default clock.

---

### Variables and inspection

```python
>>> x = 42
>>> show_vars()
  x = 42

>>> show_sigs()
  a = 00000101
  b = 00000011
  sum = 00001000

>>> list_signals()   # returns a list of signal names
['a', 'b', 'sum']
```

| Function | Description |
|---|---|
| `show_vars()` | Print user-defined REPL variables |
| `show_sigs()` | Print all DUT signals and current values |
| `list_signals()` | Return a list of signal name strings |

Variables, functions, and imports all persist across submissions.

---

### Cocotb primitives

These cocotb objects are pre-imported in the REPL:

| Name | Description |
|---|---|
| `dut` | Top-level DUT handle |
| `clk` | Default clock signal handle (same as `dut.<clk_signal>`) |
| `cocotb` | The `cocotb` module |
| `Timer` | `cocotb.triggers.Timer` |
| `RisingEdge` | `cocotb.triggers.RisingEdge` |
| `FallingEdge` | `cocotb.triggers.FallingEdge` |
| `Clock` | `cocotb.clock.Clock` |
| `BinaryValue` | `cocotb.binary.BinaryValue` |

```python
>>> await RisingEdge(dut.clk)
>>> await Timer(100, units="ns")
```

---

### Step-back and replay

cocodbg tracks every submitted block. `:r` restarts the simulation and replays them all; `:stepback N` replays all but the last N.

```python
>>> dut.a.value = 5
>>> await cycles(10)
>>> dut.b.value = 3  # made a mistake here
:stepback 1          # restart and replay everything except the last submission
```
