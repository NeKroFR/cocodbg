from cocodbg import CocotbDebugConfig

CocotbDebugConfig(
    toplevel="adder",
    sources=["adder.vhd"],
    sim="ghdl",
    lang="vhdl",
    clk_signal="clk",
    clk_period_ns=10.0,
    extra_args={"GHDL_ARGS": "--std=08"},
).run()
