"""
Windows launcher: run argv inside an AppContainer.

Invoked as ``python sandbox_windows_launch.py --workdir ... --grant ... -- cmd.exe /c ...``
so the wrap path does not import the rest of the SDK (sdk/__init__.py pulls lxml).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import sandbox_windows_appcontainer as ac  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tala Windows AppContainer launcher")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--grant", action="append", default=[])
    parser.add_argument("--grant-read", action="append", default=[])
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.probe:
        if not ac.appcontainer_available():
            print("appcontainer=unavailable")
            return 2
        try:
            sid = ac.ensure_profile()
        except Exception as exc:
            print(f"appcontainer=error {exc}")
            return 2
        print(f"appcontainer=ok sid={sid}")
        return 0
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("TALA_SANDBOX: launcher needs a command after --", file=sys.stderr)
        return 2
    workdir = args.workdir
    try:
        if not ac.appcontainer_available():
            result = ac.guarded_run(command, workdir, "CreateAppContainerProfile missing")
        else:
            result = ac.spawn_appcontainer(
                command, workdir, args.grant, allow_network=args.allow_network,
                read_paths=args.grant_read,
            )
    except Exception as exc:
        result = ac.guarded_run(command, workdir, str(exc))
    sys.stdout.buffer.write(result.stdout or b"")
    sys.stderr.buffer.write(result.stderr or b"")
    return int(result.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
