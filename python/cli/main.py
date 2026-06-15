import argparse
import sys
import asyncio

def main():
    parser = argparse.ArgumentParser(description="Tala Cognitive Engine Room CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start command
    parser_start = subparsers.add_parser("start", help="Start the Tala server")
    parser_start.add_argument("--port", type=int, default=8765, help="Port to run the WebSocket server on")

    # TUI command
    parser_tui = subparsers.add_parser("tui", help="Launch the Tala Textual User Interface")
    
    args = parser.parse_args()

    if args.command == "start":
        print(f"Starting Tala server on port {args.port}...")
        # Here we would import and start the harness.server
        # from harness.server import start_server
        # asyncio.run(start_server(port=args.port))
    elif args.command == "tui":
        from cli.tui import main as tui_main
        tui_main()
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
