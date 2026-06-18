from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve generated toolpath reports over localhost.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "output" / "toolpaths",
    )
    parser.add_argument("--report", default="reference_operations_gcode_validation.html")
    args = parser.parse_args()

    directory = args.directory.resolve()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as server:
        url = f"http://127.0.0.1:{args.port}/{args.report}"
        print(f"Serving {directory}")
        print(f"Open {url}")
        server.serve_forever()


if __name__ == "__main__":
    main()
