import os
import subprocess
import sys

import pytest

from cocodbg import _process as proc


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_live_process_is_returned():
    proc._write_pidfile(os.getpid(), 9001)
    assert proc._read_pidfile(9001) == os.getpid()


def test_wrong_start_time_is_rejected_as_pid_reuse():
    with open(proc._pidfile_path(9001), "w") as f:
        f.write(f"{os.getpid()} 1")
    assert proc._read_pidfile(9001) is None


def test_exited_process_is_rejected():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    proc._write_pidfile(child.pid, 9001)
    child.wait()
    assert proc._read_pidfile(9001) is None


def test_legacy_pid_only_file_is_not_trusted():
    with open(proc._pidfile_path(9001), "w") as f:
        f.write(str(os.getpid()))
    assert proc._read_pidfile(9001) is None


def test_ports_do_not_share_a_pidfile():
    proc._write_pidfile(os.getpid(), 9001)
    assert proc._read_pidfile(9002) is None
    proc._remove_pidfile(9001)
    assert proc._read_pidfile(9001) is None


def test_missing_file_returns_none():
    assert proc._read_pidfile(9003) is None
