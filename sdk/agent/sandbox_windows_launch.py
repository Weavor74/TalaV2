"""
Windows launcher: run argv inside an AppContainer.

Invoked as ``python sandbox_windows_launch.py --workdir ... --grant ... -- cmd.exe /c ...``
so the wrap path does not import the rest of the SDK (sdk/__init__.py pulls lxml).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import sandbox_windows_appcontainer as ac  # noqa: E402


def _write_status(path: str, payload: dict) -> None:
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tala Windows AppContainer launcher")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--grant", action="append", default=[])
    parser.add_argument("--grant-read", action="append", default=[])
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.diagnose:
        try:
            return ac._diagnose_createprocess()
        except Exception as extra:
            sys.stdout.write(f"TALA_AC_DIAG: UNCAUGHT {type(extra).__name__}: {extra}\n")
            sys.stdout.flush()
            return 0
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
    if not workdir:
        print("TALA_SANDBOX: launcher needs --workdir", file=sys.stderr)
        return 2
    try:
        if not ac.appcontainer_available():
            _write_status(args.status_file, {
                "mode": "job-object-only",
                "reason": "CreateAppContainerProfile missing",
            })
            result = ac.guarded_run(command, workdir, "CreateAppContainerProfile missing")
        else:
            _write_status(args.status_file, {"mode": "appcontainer"})
            result = ac.spawn_appcontainer(
                command, workdir, args.grant, allow_network=args.allow_network,
                read_paths=args.grant_read,
            )
    except ac.AccessDenied as extra:
        _write_status(args.status_file, {
            "mode": "access-denied", "path": extra.path, "reason": str(extra),
        })
        sys.stderr.write(f"{ac.ACCESS_DENIED_MARK} path={extra.path}\n{extra}\n")
        sys.stderr.flush()
        return 5
    except Exception as extra:
        # Missing APIs already took the guarded path above. CreateProcess /
        # ACL failures must not silently run unsandboxed.
        _write_status(args.status_file, {"mode": "spawn-failed", "reason": str(extra)})
        sys.stderr.write(f"TALA_SANDBOX: spawn-failed {extra}\n")
        sys.stderr.flush()
        return 1
    sys.stdout.buffer.write(result.stdout or b"")
    sys.stderr.buffer.write(result.stderr or b"")
    return int(result.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
