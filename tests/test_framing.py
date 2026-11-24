import socket
import threading
import time

import pytest

from cocodbg._framing import END, BEGIN, escape_line, unescape_line
from cocodbg._server import CocotbDebugger


@pytest.mark.parametrize("line", [
    "plain", "", END, BEGIN, "<<x", "\\", "\\<<END>>", "a\\b", "  <<END>>",
])
def test_roundtrip(line):
    assert unescape_line(escape_line(line)) == line


@pytest.mark.parametrize("line", [END, BEGIN, "<<anything", "\\x"])
def test_escaped_line_is_never_a_marker(line):
    assert escape_line(line) not in (BEGIN, END)


def test_server_reply_with_marker_line_does_not_end_early():
    dbg = CocotbDebugger("127.0.0.1", 0)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    dbg.port = port
    srv.close()
    dbg.start()

    def answer():
        code, reply_q, _ = dbg.cmd_q.get(timeout=5)
        reply_q.put("[OK] a\n" + END + "\nb")

    threading.Thread(target=answer, daemon=True).start()
    for _ in range(50):
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    f = c.makefile("rwb", buffering=0)
    f.readline()
    f.write(b"1\n\n")
    lines = []
    while True:
        ln = f.readline().decode().rstrip("\r\n")
        lines.append(ln)
        if ln == END:
            break
    dbg.stop()
    assert [unescape_line(x) for x in lines[1:-1]] == ["[OK] a", END, "b"]
