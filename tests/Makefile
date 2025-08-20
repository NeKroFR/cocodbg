# Hardcoded for now but will be automatically generated in future versions
SIM = ghdl
TOPLEVEL_LANG = vhdl
TOPLEVEL = adder
MODULE = test_cocodbg
VHDL_SOURCES = $(shell pwd)/adder.vhd

export PYTHONPATH := $(abspath .):$(abspath ..):$(PYTHONPATH)
include $(shell cocotb-config --makefiles)/Makefile.sim
