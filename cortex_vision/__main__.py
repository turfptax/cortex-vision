"""Entry point for `python -m cortex_vision`.

Subcommands:
    serve   — run the HTTP sidecar service (default)
    version — print the version and exit
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="cortex-vision")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="Run the HTTP sidecar service")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--log-level", default=None)

    sub.add_parser("version", help="Print the version and exit")

    args, remainder = parser.parse_known_args()

    # Default to `serve` if no subcommand given (so PyInstaller binaries can be
    # double-clicked and just start serving).
    cmd = args.cmd or "serve"

    if cmd == "version":
        from cortex_vision import __version__
        print(__version__)
        return

    if cmd == "serve":
        # Re-inject any forwarded flags for argparse in server.main()
        new_argv = [sys.argv[0]]
        if args.host is not None:
            new_argv += ["--host", args.host]
        if args.port is not None:
            new_argv += ["--port", str(args.port)]
        if args.log_level is not None:
            new_argv += ["--log-level", args.log_level]
        new_argv += remainder
        sys.argv = new_argv

        from cortex_vision.server import main as serve_main
        serve_main()
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
