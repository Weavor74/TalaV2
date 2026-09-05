"""
main.py — the `tala` console script.

One codebase, one artifact; the role is chosen at startup. A Tala started
plainly is a client. A Tala started with ``--vps`` is a connection node:
it enrols peers, publishes them to the chain directory, and relays between
installs that cannot dial each other.

Every way of starting the harness lands in ``harness.server.main`` so that
the flag is parsed in exactly one place. This module deliberately does not
reimplement the launch -- it previously *described* one, in a comment, above
a ``print`` that started nothing:

    if args.command == "start":
        print(f"Starting Tala server on port {args.port}...")
        # Here we would import and start the harness.server

which meant `tala start` reported success and left no server running.
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Tala Cognitive Engine Room CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    parser_start = subparsers.add_parser("start", help="Start the Tala server")
    parser_start.add_argument(
        "--port", type=int, default=None,
        help="Port for the WebSocket server (default: WS_PORT, else 8765)",
    )
    parser_start.add_argument(
        "--vps", action="store_true",
        help=(
            "Run as a connection node: accept first-time Tala handshakes, enrol "
            "them as contacts, publish them to the chain peer directory, and relay "
            "between peers that cannot dial each other. Off unless passed."
        ),
    )

    subparsers.add_parser("tui", help="Launch the Tala Textual User Interface")

    args = parser.parse_args()

    if args.command == "start":
        if args.port is not None:
            # settings reads the environment, so this is how a port reaches
            # the server without a second configuration path.
            os.environ["WS_PORT"] = str(args.port)

        from python.harness.server import main as harness_main

        # Hand the flag to the one parser that owns it rather than deciding
        # the role here as well. Two places that both decide what --vps means
        # is how they come to disagree.
        sys.argv = [sys.argv[0]] + (["--vps"] if args.vps else [])
        harness_main()
    elif args.command == "tui":
        from cli.tui import main as tui_main
        tui_main()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
