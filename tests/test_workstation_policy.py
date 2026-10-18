"""Workstation policy: sandbox boundaries, deny-lists and the shell/Python risk scanners."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.agent import workstation_policy as pol


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "TalaWorkspace"
    root.mkdir()
    monkeypatch.setenv("TALA_WORKSTATION_ROOT", str(root))
    monkeypatch.setenv("TALA_WORKSTATION_CONFIG", str(tmp_path / "workstation.json"))
    monkeypatch.delenv("TALA_WORKSTATION_WRITE_ROOTS", raising=False)
    monkeypatch.delenv("TALA_WORKSTATION_DENY", raising=False)
    monkeypatch.delenv("TALA_WORKSTATION", raising=False)
    return pol.load_config()


def _write_cfg(tmp_path, data):
    (tmp_path / "workstation.json").write_text(json.dumps(data), encoding="utf-8")
    return pol.load_config()


# --- paths ------------------------------------------------------------------

def test_in_root_write_runs(cfg):
    assert pol.classify_path_access(cfg, cfg.root / "a" / "b.txt", "write").verdict == pol.ALLOW


def test_parent_escape_is_outside_root(cfg):
    target = pol.resolve_in_root(cfg, "../escape.txt")
    v = pol.classify_path_access(cfg, target, "write")
    assert v.verdict == pol.APPROVE and "outside_root" in v.categories


def test_outside_root_write_needs_approval_by_default(cfg, tmp_path):
    v = pol.classify_path_access(cfg, tmp_path / "elsewhere.txt", "write")
    assert v.verdict == pol.APPROVE


def test_outside_root_write_can_be_hard_denied(cfg, tmp_path):
    cfg = _write_cfg(tmp_path, {"outside_root_writes": "deny"})
    assert pol.classify_path_access(cfg, tmp_path / "x.txt", "write").verdict == pol.DENY


def test_allowlisted_extra_root_is_writable(cfg, tmp_path, monkeypatch):
    extra = tmp_path / "shared"
    monkeypatch.setenv("TALA_WORKSTATION_WRITE_ROOTS", str(extra))
    cfg = pol.load_config()
    assert pol.classify_path_access(cfg, extra / "out.txt", "write").verdict == pol.ALLOW


def test_reads_outside_root_are_allowed_but_secrets_are_not(cfg, tmp_path):
    assert pol.classify_path_access(cfg, tmp_path / "notes.txt", "read").verdict == pol.ALLOW
    assert pol.classify_path_access(cfg, tmp_path / ".env", "read").verdict == pol.DENY
    assert pol.classify_path_access(cfg, cfg.root / ".env", "read").verdict == pol.ALLOW


@pytest.mark.parametrize("rel", [
    "src/Trike Trails/main.gd", "Antigravity/settings.json", "home/.ssh/id_ed25519", "home/.aws/credentials",
    "u/AppData/Roaming/Microsoft/Credentials/x", "u/AppData/Local/Google/Chrome/User Data/Default/Cookies",
    "u/.git-credentials", "u/.gemini/antigravity/brain",
])
def test_deny_list_blocks_every_mode(cfg, tmp_path, rel):
    target = tmp_path / rel
    for mode in ("read", "write", "delete"):
        assert pol.classify_path_access(cfg, target, mode).verdict == pol.DENY, (rel, mode)


def test_deny_list_blocks_deny_listed_paths_inside_root_too(cfg):
    assert pol.classify_path_access(cfg, cfg.root / "trike-copy" / "a.txt", "write").verdict == pol.DENY


def test_internal_area_is_not_writable(cfg):
    v = pol.classify_path_access(cfg, cfg.internal_dir / "audit" / "workstation-audit.jsonl", "write")
    assert v.verdict == pol.DENY
    assert pol.classify_path_access(cfg, cfg.audit_path, "read").verdict == pol.ALLOW


def test_config_file_is_deny_listed(cfg):
    assert pol.classify_path_access(cfg, cfg.config_path, "write").verdict == pol.DENY


def test_deletes_need_approval_and_root_cannot_be_deleted(cfg):
    assert pol.classify_path_access(cfg, cfg.root / "old.txt", "delete").verdict == pol.APPROVE
    assert pol.classify_path_access(cfg, cfg.root, "delete").verdict == pol.DENY


def test_extra_deny_pattern_from_env(cfg, monkeypatch):
    monkeypatch.setenv("TALA_WORKSTATION_DENY", "secret-project")
    cfg = pol.load_config()
    assert pol.classify_path_access(cfg, cfg.root / "secret-project" / "x", "read").verdict == pol.DENY


def test_broken_config_falls_back_to_defaults(cfg, tmp_path):
    (tmp_path / "workstation.json").write_text("{not json", encoding="utf-8")
    c = pol.load_config()
    assert c.outside_root_writes == pol.APPROVE and c.extra_write_roots == []


# --- shell ------------------------------------------------------------------

def _sh(cfg, cmd):
    return pol.classify_shell(cfg, cmd, cfg.root)


@pytest.mark.parametrize("cmd", [
    "Get-ChildItem", "Get-ChildItem -Recurse | Sort-Object Length -Descending | Select-Object -First 5",
    "Get-Content ri.txt", "python primes.py > primes.txt", "New-Item -ItemType Directory project-test",
    "Get-ChildItem C:\\Users\\steve\\Documents", "echo hello",
])
def test_ordinary_in_root_or_read_only_commands_run(cfg, cmd):
    (cfg.root / "primes.py").write_text("print(2)\n", encoding="utf-8")
    assert _sh(cfg, cmd).verdict == pol.ALLOW, (cmd, _sh(cfg, cmd).reasons)


@pytest.mark.parametrize("cmd,category", [
    ("Remove-Item old.txt", "delete"), ("rm old.txt", "delete"), ("Get-ChildItem | % { rm $_ }", "delete"),
    ("pip install requests", "install"), ("python -m pip install numpy", "install"), ("winget install x", "install"),
    ("Invoke-WebRequest https://example.com -OutFile a.html", "network"), ("iwr example.com", "network"),
    ("git clone https://github.com/x/y", "network"), ("Stop-Process -Name notepad", "process_kill"),
    ("taskkill /IM notepad.exe /F", "process_kill"), ("reg add HKCU\\Software\\X /v a /d b", "registry"),
    ("Set-ItemProperty HKCU:\\Software\\X -Name a -Value 1", "registry"),
    ("Start-Process notepad -Verb RunAs", "elevation"), ("schtasks /create /tn x /tr y", "elevation"),
    ("powershell -enc SQBFAFgA", "obfuscation"), ("iex (Get-Content a.ps1 -Raw)", "obfuscation"),
    ("Set-Content C:\\Users\\steve\\x.txt hi", "outside_root"), ("Copy-Item a.txt ..\\a.txt", "outside_root"),
    ("Get-Content $env:USERPROFILE\\x.txt | Set-Content y.txt", "outside_root"),
])
def test_risky_commands_are_held_for_approval(cfg, cmd, category):
    v = _sh(cfg, cmd)
    assert v.verdict == pol.APPROVE, (cmd, v.reasons)
    assert category in v.categories, (cmd, v.categories)


@pytest.mark.parametrize("cmd", [
    "Get-ChildItem 'D:\\src\\Trike Trails'", "Stop-Process -Name Antigravity", "Get-ChildItem D:\\tt-build-9f2908b",
    "Set-Content C:\\Windows\\System32\\drivers\\etc\\hosts x", "cmdkey /list", "Get-Content C:\\Users\\s\\.ssh\\id_rsa",
    "diskpart", "Remove-Item .tala\\audit\\workstation-audit.jsonl", "'' > .tala/audit/workstation-audit.jsonl",
])
def test_deny_listed_commands_never_run(cfg, cmd):
    assert _sh(cfg, cmd).verdict == pol.DENY, (cmd, _sh(cfg, cmd).reasons)


def test_running_an_in_root_script_scans_the_script(cfg):
    (cfg.root / "clean.py").write_text("import shutil\nshutil.rmtree('data')\n", encoding="utf-8")
    v = _sh(cfg, "python clean.py")
    assert v.verdict == pol.APPROVE and "delete" in v.categories


def test_outside_root_cwd_is_not_in_root(cfg, tmp_path):
    assert not pol.is_within(tmp_path, cfg.root)
    assert pol.is_within(cfg.root / "x", cfg.root)
    assert not pol.is_within(Path(str(cfg.root) + "-evil"), cfg.root)


# --- python -----------------------------------------------------------------

@pytest.mark.parametrize("src", [
    "print(sum(range(10)))", "import re\nrx = re.compile('a+')\nprint(rx)",
    "open('primes.txt', 'w').write('2 3 5')", "import math\nprint(math.sqrt(2))",
])
def test_plain_python_runs(cfg, src):
    assert pol.classify_python(cfg, src).verdict == pol.ALLOW, pol.classify_python(cfg, src).reasons


@pytest.mark.parametrize("src,category", [
    ("import requests\nrequests.get('http://x')", "network"), ("import subprocess\nsubprocess.run(['dir'])", "process"),
    ("import os\nos.remove('a')", "delete"), ("import winreg", "registry"), ("exec(open('x').read())", "obfuscation"),
    ("open(r'C:\\Users\\steve\\notes.txt', 'w')", "outside_root"), ("open('../x.txt','w')", "outside_root"),
])
def test_risky_python_is_held(cfg, src, category):
    v = pol.classify_python(cfg, src)
    assert v.verdict == pol.APPROVE and category in v.categories, (src, v.as_dict())


def test_python_touching_deny_listed_paths_is_denied(cfg):
    assert pol.classify_python(cfg, "open(r'D:\\src\\Trike Trails\\x.gd').read()").verdict == pol.DENY
    assert pol.classify_python(cfg, "open('C:/Windows/win.ini').read()").verdict == pol.DENY
