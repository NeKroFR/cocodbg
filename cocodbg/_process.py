import socket
import threading
import contextlib
import os
import time
import subprocess
import signal


def _state_dir() -> str:
    d = os.path.abspath("./.cocodbg")
    os.makedirs(d, exist_ok=True)
    return d


def _pidfile_path(port: int | None = None) -> str:
    """One pidfile per debugger port, so two sessions in one directory do not kill each other."""
    name = "sim.pid" if port is None else f"sim-{port}.pid"
    return os.path.join(_state_dir(), name)


def _proc_start_time(pid: int) -> str | None:
    """Start time of pid in clock ticks since boot (field 22 of /proc/pid/stat), or None."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read()
        # comm may contain spaces and parentheses, so split after the last ')'.
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _write_pidfile(pid: int, port: int | None = None):
    start = _proc_start_time(pid)
    if start is None:
        return
    try:
        with open(_pidfile_path(port), "w") as f:
            f.write(f"{pid} {start}")
    except Exception:
        pass


def _read_pidfile(port: int | None = None) -> int | None:
    """Return the recorded pid only if that exact process is still alive.

    A pid that exited and was reused by an unrelated process has a different start time.
    Files without a start time are ignored rather than trusted.
    """
    try:
        with open(_pidfile_path(port), "r") as f:
            parts = f.read().split()
        if len(parts) != 2:
            return None
        pid = int(parts[0])
    except Exception:
        return None
    if _proc_start_time(pid) != parts[1]:
        return None
    return pid


def _remove_pidfile(port: int | None = None):
    with contextlib.suppress(Exception):
        os.remove(_pidfile_path(port))


def _tail_proc(proc: subprocess.Popen):
    def pump(stream):
        for raw in iter(stream.readline, b""):
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:
                line = str(raw)
            print(line)

    t1 = threading.Thread(target=pump, args=(proc.stdout,), daemon=True)
    t2 = threading.Thread(target=pump, args=(proc.stderr,), daemon=True)
    t1.start(); t2.start()
    return (t1, t2)


def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _wait_for_port_open(host: str, port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _can_connect(host, port):
            return True
        time.sleep(0.25)
    return False


def _wait_for_port_closed(host: str, port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _can_connect(host, port):
            return True
        time.sleep(0.2)
    return not _can_connect(host, port)


def _open_connection(host: str, port: int):
    s = socket.create_connection((host, port))
    f = s.makefile("rwb", buffering=0)
    banner = f.readline().decode().rstrip()
    return s, f, banner


def _kill_process_group_by_pid(pid: int):
    if pid <= 0:
        return
    with contextlib.suppress(Exception):
        os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGTERM)
    time.sleep(0.3)
    with contextlib.suppress(Exception):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGKILL)


def _preflight_cleanup(host: str, port: int):
    pid = _read_pidfile(port)
    if pid:
        _kill_process_group_by_pid(pid)
    _remove_pidfile(port)
    _wait_for_port_closed(host, port, timeout=5.0)


def _launch_sim(sim_dir: str, port: int | None = None) -> tuple[subprocess.Popen, tuple[threading.Thread, threading.Thread]]:
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
        start_new_session=True,
    )
    _write_pidfile(sim_proc.pid, port)
    tails = _tail_proc(sim_proc)
    return sim_proc, tails


def _terminate_sim(sim_proc: subprocess.Popen, host: str = "127.0.0.1", port: int = 9999):
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
                with contextlib.suppress(Exception):
                    os.killpg(sim_proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    sim_proc.kill()
                with contextlib.suppress(Exception):
                    sim_proc.wait(timeout=3)
    finally:
        _wait_for_port_closed(host, port, timeout=10.0)
        _remove_pidfile(port)
