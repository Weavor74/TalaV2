# Tala's workstation: her own computer

The workstation toolset gives Tala a machine to work on: a shell, file
read/write/list/search, her own Python venv, and background jobs. It is
cross-platform: Windows (Kagura), Linux (including the Jetson, aarch64) and
macOS. See "Portability" below.

| Tool | What it does | Runs without asking when... |
|---|---|---|
| `run_shell` | shell command (PowerShell on Windows, bash/sh on Linux/macOS; the result says which), timeout (default 60s, max 30 min), output captured and truncated (full log saved under `.tala/runs/`) | it stays in the workspace and does nothing risky |
| `read_file` | read text, optional line range | always, unless deny-listed |
| `write_file` | create/overwrite/append | the path is inside the workspace (or an allowlisted extra root) |
| `list_dir` | list with sizes/mtimes, recursive, `sort_by=size|modified|name` | always, unless deny-listed |
| `search_files` | content or file-name search | always, unless deny-listed |
| `run_python` | run code or a `.py` file in `<root>/.tala/venv` (created on first use) | the code has no network/subprocess/delete/registry/dynamic-exec and stays in the workspace |
| `delete_path` | delete file/folder | never: always needs approval |
| `install_python_package` | pip install into her venv | never: always needs approval |
| `start_job` / `job_status` / `stop_job` | long-running background commands, max 3 at once, logs in `.tala/jobs/` | same rule as `run_shell`; stopping her own job is always allowed |

## Where she works: Settings > Workspace

* **The user chooses the workspace.** Settings > **Workspace** shows the current
  folder, a **Browse…** button (the native folder picker in the desktop app,
  which can also create a folder), a text field (the only option in the browser
  UI), optional **extra folders**, and **Save**.
* **No silent default.** Until a workspace is saved, every workstation tool
  refuses with "Tala's workspace is not configured … choose a workspace folder
  in Settings > Workspace", and Tala passes that on to the user. The platform
  default (`<install drive>\TalaWorkspace` on Windows, so `D:\TalaWorkspace` on Kagura;
  `~/TalaWorkspace` on Linux/macOS) is only offered as a suggestion in the picker.
* **Where it is stored.** Saving writes `root` and `extra_roots` to Tala's settings
  file `<user data>/data/workstation.json` (see Configuration). That file is outside
  the workspace and deny-listed for the tools, so Tala cannot move her own sandbox.
  `TALA_WORKSTATION_ROOT` remains a fallback for headless installs: it is used only
  when nothing has been saved, and a saved setting wins. (The env var is not called
  `TALA_WORKSPACE_ROOT`: older scripts already use that name for the TalaV2 checkout.)
* **Validation on save** (server-side, so both UIs get the same answer, and the reason
  is shown in the UI). The folder must exist or be creatable (it is created on save),
  must be a folder and writable, and must not be:
  a whole drive / mount point / filesystem root (`C:\`, `D:\`, `/`); a system folder
  (Windows, Program Files, ProgramData; `/etc`, `/usr`, `/boot`, `/proc`, `/sys` …;
  `/System`, `/Library`, `/Applications`); the user profile folder itself or a folder
  that contains it; an app-data/credential folder (`AppData`, `~/.something`);
  anything deny-listed (Trike Trails, Antigravity, Gemini, credential stores, browser
  profiles); or Tala's own install/repo or data folder, or one that contains them.
  Extra folders get the same checks.
* **Changing it takes effect immediately.** The config is re-read on every tool call,
  so the sandbox, the audit log location and the `run_python` venv
  (`<root>/.tala/venv`) all move with the next call; no restart. **Background jobs
  already running keep running** in the workspace they started in: they stay visible
  in `job_status` (with their `workspace`) and can be stopped with `stop_job`, which
  audits in that old workspace. The save result says how many are still running.
  Each change is recorded as a `workspace_change` entry in the audit log of both the
  old and the new workspace.
* RPCs (local owner connection only): `workstation.get_config`,
  `workstation.validate_workspace` (a dry run, creates nothing) and
  `workstation.set_workspace {root, extra_roots}`. They are not tools: the model
  cannot change its own workspace.
* `<root>/.tala/` holds the audit log, job logs, run logs, temp and the venv.
  It is hidden from listings and not writable through the tools.
* Child processes get secret-looking environment variables stripped, run at
  below-normal priority (a Windows priority class; `nice -n 10` on Linux/macOS), and
  get TEMP/TMPDIR inside `<root>/.tala/tmp`. On Windows, pip/uv/npm/HF caches go to
  the install drive (`D:\caches\...`, `D:\hf-cache` on Kagura) unless already set.
  On Linux/macOS, caches stay where the system puts them unless `TALA_CACHE_ROOT` is set.

## The three verdicts

1. **allow**: read-only work anywhere not deny-listed, and anything inside the root.
2. **approve**: held for the owner. Covers delete, writes outside the root, installs,
   network, killing processes, registry, elevation/system config, nested
   shells/encoded commands, and Python that does any of those. Tala gets back
   `approval_required` with a task id. The request appears in **Mission Control**
   as a task in `review` with Approve / Reject buttons. After approval she
   repeats the *identical* call, which runs once (the approval is bound to a
   SHA-256 of the exact tool + arguments, including file content, and is consumed
   on use). A rejected action stays rejected. If the approval queue (Postgres) is
   down, the action is refused.
3. **deny**: never runs and cannot be approved. A common set on every OS: Trike Trails
   (`*trike*`), Antigravity (`*antigravity*`, `~/.gemini`), SSH/AWS/Azure/GPG/kube/docker
   credentials, `.git-credentials`, `.npmrc`/`.pypirc`/`.netrc`, editor credential
   storage. Then one set per OS:
   *Windows*: `C:\Windows`, registry hives, Credential Manager / DPAPI folders, browser
   profiles, `cmdkey`/`vaultcmd`/`lsass`, diskpart/format/bcdedit, Defender settings,
   `D:\tt-*`.
   *Linux*: `/etc`, `/boot`, `/proc`, `/sys`, keyrings (`~/.local/share/keyrings`,
   `secret-tool`, kwallet), `~/.password-store`, Chrome/Chromium/Firefox profiles,
   `mkfs`/`wipefs`/`dd of=/dev/…`/`fdisk`.
   *macOS*: `/System`, `/etc`, `~/Library/Keychains`, `security find-*-password`, browser
   profiles and cookies, `diskutil erase…`, `csrutil`, `nvram`.
   Always: `.env` files outside the root, the workstation config file and the
   `.tala` audit area.

The emergency halt refuses every workstation action first, and kills running
workstation processes and jobs (they are registered with `async_shell`).

## Configuration

JSON at `<user data>/data/workstation.json` (or `TALA_WORKSTATION_CONFIG`). Settings >
Workspace writes `root` and `extra_roots`; the rest is for operators. The file lives
outside the workspace and is deny-listed, so Tala cannot loosen it herself.

```json
{
  "root": "D:\\TalaWorkspace",
  "extra_roots": ["D:\\TalaShared"],
  "extra_write_roots": [],
  "extra_deny_patterns": ["my-private-project"],
  "outside_root_writes": "approve",
  "auto_delete_in_root": false,
  "default_timeout": 60,
  "max_timeout": 1800,
  "max_output_chars": 12000,
  "max_jobs": 3
}
```

Environment: `TALA_WORKSTATION=0` (disable), `TALA_WORKSTATION_ROOT` (fallback when nothing is saved),
`TALA_WORKSTATION_WRITE_ROOTS` and `TALA_WORKSTATION_DENY` (`;`-separated),
`TALA_WORKSTATION_PYTHON` (base interpreter for the venv).

## Who gets the tools

* **Main chat** (Tala as the owner): the core five (`run_shell`, `read_file`,
  `write_file`, `list_dir`, `run_python`) are part of the always-offered tool
  floor (`python/harness/tool_selection.py: _core_tools()`) for local and remote
  providers; the embedded tier (8-tool budget) and the other six tools go
  through the keyword ranker.
* **Room agents**: `python scripts/grant_workstation_tools.py --agent <name>`
  adds them to the agent's `tool_allowlist` and syncs the AuthorizationGate
  `execute_tool` grants, exactly as the agent editor does.

## Audit

Every call (allowed, held, denied, halted) is one JSON line in
`<root>\.tala\audit\workstation-audit.jsonl`: timestamp, tool, arguments
(clipped), verdict with reasons, approval id, outcome, duration.

## Portability (Windows, Linux, macOS)

Everything OS-specific lives in one adapter module, `sdk/agent/workstation_platform.py`:
one `Platform` object per OS family (Windows, Linux, macOS) carrying

* the deny patterns (`COMMON_DENY` + `WINDOWS_DENY` / `LINUX_DENY` / `MACOS_DENY`) and
  the system folders that can never be a workspace;
* the command-risk tables for that OS's shell (`COMMON_SHELL_RISK` +
  `WINDOWS_SHELL_RISK` for PowerShell, or `POSIX_SHELL_RISK` for bash/sh: `rm`,
  `find -delete`, `sudo`, `apt`/`dnf`/`brew`, `systemctl`, `chmod`, `pkill`, `base64 -d | sh` …),
  the command-position aliases, the read-only and write verbs, and the
  path/env-variable forms to look for (`C:\…`, `\\server\share`, `$env:…`; or `/…`,
  `$HOME`, `~`);
* how to run a shell (`shell_argv`: PowerShell, or bash falling back to sh), lower its
  priority, spawn it in its own process group, the venv python path
  (`Scripts\python.exe` vs `bin/python`), cache/temp defaults and the suggested workspace.

`workstation_policy.py` (verdicts), `workstation.py` (execution, approvals, audit,
Settings) and `workstation_tools.py` (registry wrappers) have no `os.name` checks: they
use pathlib and `plat.current()`. Porting to another OS means adding one `Platform`.

Tests: `tests/test_workstation_platform.py` selects each adapter explicitly, so the Linux
and macOS rules run on any CI host (and the PowerShell cases in
`tests/test_workstation_policy.py` run on Linux CI with the Windows adapter). On a
Linux host it also runs the real adapter end to end (bash, TMPDIR, `/etc/shadow` denied).

## Honest limits

* The shell and Python checks are pattern scanners. They catch the ordinary
  routes, but code that builds a path or command at runtime can get past them.
  OS isolation is in `sdk/agent/sandbox.py` plus OS adapters:
  * **Linux**: bubblewrap if present, otherwise `unshare` (network/pid, not FS).
  * **macOS**: `sandbox-exec -p` (deprecated, still functional).
  * **Windows**: an **AppContainer** (not a job object alone). The first
    workstation spawn calls `CreateAppContainerProfile` for `Tala.Workstation`
    (per-user, **no admin**). The workspace and approved extra roots get an ACL
    grant for that SID; Tala's install and user-data folders are never granted.
    Network capabilities (`internetClient`, `privateNetworkClientServer`) are
    omitted unless the command was approved for network or `TALA_WORKSTATION_NET=1`.
    A job object still wraps the tree (kill-on-close). Read+execute ACLs are
    granted on the **specific** interpreter, venv and tool paths the child
    runs (`icacls /grant:r`, never a whole drive) so a workspace on `D:` can
    still start Python when `ALL APPLICATION PACKAGES` is missing. `cmd.exe`
    and `powershell.exe` are launched with `CreateProcessW(lpApplicationName=NULL)`
    and the canonical System32 path from `GetSystemDirectoryW`; Tala does
    **not** icacls `Windows`/`System32` (that tree already grants
    `ALL APPLICATION PACKAGES`). CreateProcess Win32 2 is `spawn-failed`,
    not an unsandboxed fallback. Workspace
    write grants use `/grant:r` and are **revoked** (`icacls /remove`) when
    Settings > Workspace moves the folder. If AppContainer APIs fail, the
    command runs unsandboxed **loudly**: the tool result carries
    `sandbox_fallback` + a warning, the UI gets `workstation.sandbox_fallback`
    (same system-notice path as `inference.fallback`), and the workstation
    audit log records `sandbox_fallback`. Access denied names the path and
    does **not** fall back to unsandboxed. Not used: LPAC (breaks Python/cmd),
    restricted tokens, Windows Sandbox/Hyper-V, WFP (admin). AppContainer may
    still **read** world-readable files on volumes that grant
    `ALL APPLICATION PACKAGES`; it must not **write** Tala's install.
  Workstation sandbox wrap is on by default on Windows (`sandbox_default`);
  set `TALA_WORKSTATION_SANDBOX=0` to disable. Linux stays opt-in
  (`TALA_WORKSTATION_SANDBOX=1`) because `unshare` is not a full FS sandbox.
* Job tracking does not survive a harness restart. A job still running at shutdown is not
  guaranteed to stop with the harness; stop jobs before restarting.
* No GUI/browser control here; `desktop_*` tools and `web_browser.py` are separate.
