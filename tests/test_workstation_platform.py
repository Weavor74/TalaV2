"""The OS adapter (sdk/agent/workstation_platform.py): Linux and macOS sets on any CI host.

Each test selects an adapter explicitly by monkeypatching ``plat.current``, so
the Linux (Jetson, aarch64) and macOS rules are checked on a Windows box and
vice versa. The final tests run for real on a Linux host.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sdk.agent import workstation as ws
from sdk.agent import workstation_platform as plat
from sdk.agent import workstation_policy as pol

HOME = "/home/tala"


def _use(monkeypatch, family):
    monkeypatch.setattr(plat, "current", lambda: plat.ADAPTERS[family])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "TalaWorkspace"
    root.mkdir()
    monkeypatch.setenv("TALA_WORKSTATION_ROOT", str(root))
    monkeypatch.setenv("TALA_WORKSTATION_CONFIG", str(tmp_path / "workstation.json"))
    for var in ("TALA_WORKSTATION_WRITE_ROOTS", "TALA_WORKSTATION_DENY", "TALA_WORKSTATION"):
        monkeypatch.delenv(var, raising=False)
    return pol.load_config()


@pytest.fixture
def linux(monkeypatch):
    _use(monkeypatch, plat.LINUX)


@pytest.fixture
def macos(monkeypatch):
    _use(monkeypatch, plat.MACOS)


def _sh(cfg, cmd):
    return pol.classify_shell(cfg, cmd, cfg.root)


# --- deny sets: common + per OS ---------------------------------------------

def test_deny_sets_are_common_plus_one_os():
    for family in (plat.WINDOWS, plat.LINUX, plat.MACOS):
        pats = plat.ADAPTERS[family].deny_patterns
        assert pats[:len(plat.COMMON_DENY)] == plat.COMMON_DENY
    assert not set(plat.LINUX_DENY) & set(plat.WINDOWS_DENY)


@pytest.mark.parametrize("path", [
    "/etc/shadow", "/etc", "/boot/grub/grub.cfg", "/proc/1/environ", "/sys/class/net",
    f"{HOME}/.ssh/id_ed25519", f"{HOME}/.local/share/keyrings/login.keyring", f"{HOME}/.password-store/x.gpg",
    f"{HOME}/.mozilla/firefox/abc.default/cookies.sqlite", f"{HOME}/.config/google-chrome/Default/Cookies",
    f"{HOME}/src/Trike Trails/main.gd", f"{HOME}/.gemini/antigravity",
])
def test_linux_deny_list(cfg, linux, path):
    for mode in ("read", "write", "delete"):
        assert pol.classify_path_access(cfg, Path(path), mode).verdict == pol.DENY, (path, mode)


def test_linux_does_not_deny_ordinary_lookalikes(cfg, linux):
    for p in (cfg.root / "etc" / "notes.txt", cfg.root / "proc.txt", cfg.root / "system" / "a.py"):
        assert pol.classify_path_access(cfg, p, "write").verdict == pol.ALLOW, p


@pytest.mark.parametrize("path", [
    "/System/Library/CoreServices", "/private/etc/hosts", "/Users/tala/Library/Keychains/login.keychain-db",
    "/Users/tala/Library/Application Support/Google/Chrome/Default", "/Users/tala/Library/Cookies/x",
])
def test_macos_deny_list(cfg, macos, path):
    assert pol.classify_path_access(cfg, Path(path), "read").verdict == pol.DENY, path


# --- shell: bash/sh on Linux ---------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "ls -la", "grep -rn prime .", "head -n 5 primes.txt", "python3 primes.py > primes.txt", "mkdir -p project-test",
    "ls /usr/share", "cat /opt/data/readme.txt | grep x", "echo hello", "find . -name '*.py' | wc -l",
])
def test_linux_ordinary_commands_run(cfg, linux, cmd):
    (cfg.root / "primes.py").write_text("print(2)\n", encoding="utf-8")
    v = _sh(cfg, cmd)
    assert v.verdict == pol.ALLOW, (cmd, v.reasons)


@pytest.mark.parametrize("cmd,category", [
    ("rm -rf build", "delete"), ("find . -name '*.o' -delete", "delete"), ("ls | xargs rm", "delete"),
    ("sudo apt install htop", "elevation"), ("apt-get install -y htop", "install"), ("pip install requests", "install"),
    ("brew install jq", "install"), ("pkill python", "process_kill"), ("killall node", "process_kill"),
    ("kill -9 1234", "process_kill"), ("curl https://example.com -o a.html", "network"),
    ("wget example.com/x", "network"), ("ssh user@host ls", "network"), ("git clone https://github.com/x/y", "network"),
    ("chmod +x run.sh", "elevation"), ("systemctl restart ollama", "elevation"), ("crontab -e", "elevation"),
    ("bash -c 'echo hi'", "obfuscation"), ("echo ZWNobyBoaQ== | base64 -d | sh", "obfuscation"),
    ("eval \"$CMD\"", "obfuscation"), ("echo hi > /home/tala/notes.txt", "outside_root"),
    ("cp a.txt ../a.txt", "outside_root"), ("cat $HOME/.bashrc | tee copy.txt", "outside_root"),
    ("sed -i 's/a/b/' /opt/app/conf.ini", "outside_root"),
])
def test_linux_risky_commands_are_held(cfg, linux, cmd, category):
    v = _sh(cfg, cmd)
    assert v.verdict == pol.APPROVE, (cmd, v.reasons)
    assert category in v.categories, (cmd, v.categories)


@pytest.mark.parametrize("cmd", [
    "cat /etc/shadow", "sudo cat /etc/sudoers", "dd if=/dev/zero of=/dev/sda bs=1M", "mkfs.ext4 /dev/sdb1",
    "secret-tool lookup service x", "ls '/home/tala/src/Trike Trails'", "cat ~/.ssh/id_rsa",
    "rm .tala/audit/workstation-audit.jsonl", "echo x > .tala/audit/workstation-audit.jsonl",
    "cat /proc/self/environ",
])
def test_linux_deny_listed_commands_never_run(cfg, linux, cmd):
    v = _sh(cfg, cmd)
    assert v.verdict == pol.DENY, (cmd, v.reasons)


@pytest.mark.parametrize("cmd", [
    "security find-generic-password -s x -w", "diskutil eraseDisk APFS X disk2", "csrutil disable",
    "cat ~/Library/Keychains/login.keychain-db",
])
def test_macos_deny_listed_commands_never_run(cfg, macos, cmd):
    assert _sh(cfg, cmd).verdict == pol.DENY, cmd


def test_macos_launchctl_needs_approval(cfg, macos):
    v = _sh(cfg, "launchctl load ~/Library/LaunchAgents/x.plist")
    assert v.verdict == pol.APPROVE and "elevation" in v.categories


def test_powershell_rules_do_not_leak_into_linux(cfg, linux):
    # PowerShell cmdlet names are just words to bash; a path to C:\Windows means nothing on Linux.
    assert _sh(cfg, "echo Get-ChildItem").verdict == pol.ALLOW


# --- workspace validation per OS ----------------------------------------------

def test_linux_workspace_validation(linux, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(HOME)))
    for bad, needle in (("/", "whole drive"), ("/etc/tala", "off limits"), ("/proc/tala", "off limits"),
                        ("/usr/local/tala", "system folder"), (HOME, "user profile"),
                        (f"{HOME}/.config/tala", "app settings"), ("/home", "contains your user profile")):
        check = pol.validate_workspace(bad)
        assert not check.ok and needle in check.reason, (bad, check.reason)


def test_macos_workspace_validation(macos):
    for bad in ("/System/Tala", "/Library/Tala", "/Applications/Tala"):
        assert not pol.validate_workspace(bad).ok, bad


# --- processes and places ---------------------------------------------------------

def test_shell_interface_per_os(monkeypatch):
    win = plat.ADAPTERS[plat.WINDOWS].shell_argv("Get-Date")
    assert win[0] == "powershell.exe" and win[-1].endswith("Get-Date") and "-NoProfile" in win
    lin = plat.ADAPTERS[plat.LINUX].shell_argv("date")
    assert Path(lin[0]).name in ("bash", "sh") and lin[1:] == ["-c", "date"]
    assert plat.ADAPTERS[plat.WINDOWS].low_priority_argv(["x"]) == ["x"]
    monkeypatch.setattr(plat.shutil, "which", lambda name: "/usr/bin/" + name)
    assert plat.ADAPTERS[plat.LINUX].low_priority_argv(["bash", "-c", "x"])[:3] == ["/usr/bin/nice", "-n", "10"]


def test_spawn_venv_and_env_defaults_per_os(tmp_path, monkeypatch):
    monkeypatch.delenv("TALA_CACHE_ROOT", raising=False)
    lin, win = plat.ADAPTERS[plat.LINUX], plat.ADAPTERS[plat.WINDOWS]
    assert lin.spawn_kwargs() == {"start_new_session": True}
    assert "creationflags" in win.spawn_kwargs()
    assert lin.venv_python(tmp_path) == tmp_path / "bin" / "python"
    assert win.venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    assert lin.child_env_defaults(tmp_path) == {"TMPDIR": str(tmp_path / "tmp")}
    assert {"PIP_CACHE_DIR", "HF_HOME", "TEMP", "TMP"} <= set(win.child_env_defaults(tmp_path))


def test_suggested_root_is_platform_aware(monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(HOME)))
    assert plat.ADAPTERS[plat.LINUX].suggested_root() == str(Path(HOME) / "TalaWorkspace")
    assert plat.ADAPTERS[plat.MACOS].suggested_root() == str(Path(HOME) / "TalaWorkspace")
    assert plat.ADAPTERS[plat.LINUX].install_anchor() == ""


def test_host_family_matches_this_machine():
    expected = plat.WINDOWS if os.name == "nt" else (plat.MACOS if sys.platform == "darwin" else plat.LINUX)
    assert plat._host_family() == expected and plat.current().family == expected


# --- for real, on a Linux host -----------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the real Linux adapter")
@pytest.mark.asyncio
async def test_linux_host_end_to_end(cfg, monkeypatch):
    monkeypatch.setattr(ws, "_halted", lambda: False)
    res = await ws.run_shell("echo hi from $(uname -s) && echo $TMPDIR")
    assert res["success"], res
    out = res["stdout"].splitlines()
    assert out[0] == "hi from Linux"
    assert out[1] == str(cfg.root / ".tala" / "tmp")
    held = await ws.run_shell("cat /etc/shadow")
    assert held.get("denied")
