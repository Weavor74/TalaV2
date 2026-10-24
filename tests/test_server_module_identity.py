"""
The running harness is one module, whatever name it is imported by.

The harness is started as a script (Electron runs ``python/harness/server.py``) or with ``-m python.harness.server``, so the
file is ``__main__``. Everything else in the code base does ``from python.harness.server import broadcast_notification`` and
friends. Without an alias, that import loads a *second copy* of the server module with its own, empty ``_active_connections``,
and every broadcast reaches nobody: a ``voice.turn`` or ``voice.audio_chunk`` goes nowhere, and so does anything else sent
that way. Found by the live voice smoke: the harness moved to "thinking" and the UI never heard about it.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "python" / "harness" / "server.py"
SNIPPET = re.compile(r'(?m)^if __name__ == "__main__":\n    sys\.modules\.setdefault\("python\.harness\.server", sys\.modules\[__name__\]\)\n')


def test_the_server_registers_itself_under_its_package_name_when_run_as_a_script():
    source = SERVER.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert SNIPPET.search(source), "server.py must alias __main__ to python.harness.server"


def test_the_alias_comes_before_any_import_of_the_rest_of_the_harness():
    source = SERVER.read_text(encoding="utf-8").replace("\r\n", "\n")
    alias_at = SNIPPET.search(source).start()
    assert alias_at < source.index("from python.harness.config import settings")


def test_with_the_alias_a_package_import_is_the_running_module_and_without_it_is_not(tmp_path):
    source = SERVER.read_text(encoding="utf-8").replace("\r\n", "\n")
    snippet = SNIPPET.search(source).group(0).replace("python.harness.server", "pkg.server")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    body = textwrap.dedent('''
        import sys
        {alias}
        connections = set()

        def same_module():
            import pkg.server as again
            return again is sys.modules["__main__"] and again.connections is connections

        if __name__ == "__main__":
            print("SAME" if same_module() else "DIFFERENT")
    ''')
    for label, alias, expected in (("with", snippet, "SAME"), ("without", "", "DIFFERENT")):
        (package / "server.py").write_text(body.format(alias=alias))
        out = subprocess.run([sys.executable, str(package / "server.py")], cwd=tmp_path, capture_output=True, text=True,
                             env={"PYTHONPATH": str(tmp_path), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        assert out.stdout.strip() == expected, f"{label} the alias: {out.stdout!r} {out.stderr[-300:]!r}"
