import argparse
from ._client import start_sim


def main():
    parser = argparse.ArgumentParser(description="cocodbg, interactive cocotb debugger")
    parser.add_argument(
        "dir", nargs="?", default="tests",
        help="simulation directory containing a Makefile (default: tests)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="debugger host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9999, help="debugger port (default: 9999)")
    args = parser.parse_args()
    start_sim(args.dir, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
