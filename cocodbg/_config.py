import os
import shutil

from ._client import start_sim


class CocotbDebugConfig:
    """
    Configuration for a cocodbg session.

    Generates a hidden .cocodbg/ directory with a Makefile, launches the
    simulation, then cleans everything up on exit.

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
            f"MODULE = cocodbg.executor\n"
            f"{src_var} = {sources_str}\n"
            f"\n"
            f"export PYTHONPATH := $(abspath .):$(abspath ..):$(PYTHONPATH)\n"
            f"include $(shell cocotb-config --makefiles)/Makefile.sim\n"
        )
        with open(os.path.join(target_dir, "Makefile"), "w") as f:
            f.write(content)

    def run(self):
        """Generate .cocodbg/, run the simulation, clean up on exit."""
        target_dir = self._target_dir()

        if os.path.isdir(target_dir):
            if os.path.exists(os.path.join(target_dir, "Makefile")):
                print("# Found leftover .cocodbg/ from a previous session, cleaning up...")
                shutil.rmtree(target_dir, ignore_errors=True)

        os.makedirs(target_dir, exist_ok=True)
        self._write_makefile(target_dir)

        os.environ["COCODBG_HOST"] = self.host
        os.environ["COCODBG_PORT"] = str(self.port)
        os.environ["COCODBG_CLK_SIGNAL"] = self.clk_signal
        os.environ["COCODBG_CLK_PERIOD_NS"] = str(self.clk_period_ns)

        try:
            start_sim(target_dir, host=self.host, port=self.port)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)
