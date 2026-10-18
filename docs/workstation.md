# Tala's workstation: her own computer

The workstation toolset gives Tala a machine to work on: a PowerShell shell,
file read/write/list/search, her own Python venv, and background jobs. It is
Windows-first (Kagura) and also runs on Linux/macOS (bash) for tests and CI.

| Tool | What it does | Runs without asking when... |
|---|---|---|
| `run_shell` | PowerShell command, timeout (default 60s, max 30 min), output captured and truncated (full log saved under `.tala/runs/`) | it stays in the workspace and does nothing risky |
| `read_file` | read text, optional line range | always, unless deny-listed |
| `write_file` | create/overwrite/append | the path is inside the workspace (or an allowlisted extra root) |
| `list_dir` | list with sizes/mtimes, recursive, `sort_by=size|modified|name` | always, unless deny-listed |
| `search_files` | content or file-name search | always, unless deny-listed |
| `run_python` | run code or a `.py` file in `<root>/.tala/venv` (created on first use) | the code has no network/subprocess/delete/registry/dynamic-exec and stays in the workspace |
| `delete_path` | delete file/folder | never: always needs approval |
| `install_python_package` | pip install into her venv | never: always needs approval |
| `start_job` / `job_status` / `stop_job` | long-running background commands, max 3 at once, logs in `.tala/jobs/` | same rule as `run_shell`; stopping her own job is always allowed |

## Where she works

* Root: `D:\TalaWorkspace` on Windows (`~/TalaWorkspace` elsewhere). Override with
  `TALA_WORKSTATION_ROOT`. (Not `TALA_WORKSPACE_ROOT`: older scripts already use
  that name to mean the TalaV2 checkout.)
* `<root>\.tala\` holds the audit log, job logs, run logs, temp and the venv.
  It is hidden from listings and not writable through the tools.
* Child processes get secret-looking environment variables stripped, run at
  below-normal priority, and on Windows have pip/uv/npm caches and TEMP
  pointed at D: (`D:\caches\...`, `<root>\.tala\tmp`).

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
3. **deny**: never runs and cannot be approved. Trike Trails (`*trike*`,
   `D:\tt-*`), Antigravity (`*antigravity*`, `~/.gemini`), `C:\Windows`,
   registry hives, SSH/AWS/Azure/GPG/kube/docker credentials, `.git-credentials`,
   `.npmrc`/`.pypirc`/`.netrc`, Windows Credential Manager / DPAPI folders,
   browser profiles, `cmdkey`/`vaultcmd`, disk/boot destruction, Defender
   settings, `.env` files outside the root, the workstation config file and the
   `.tala` audit area.

The emergency halt refuses every workstation action first, and kills running
workstation processes and jobs (they are registered with `async_shell`).

## Configuration

Optional JSON at `%TALA_USER_DATA%\data\workstation.json` (or `TALA_WORKSTATION_CONFIG`).
It lives outside the workspace and is deny-listed, so Tala cannot loosen it herself.

```json
{
  "root": "D:\\TalaWorkspace",
  "extra_write_roots": ["D:\\TalaShared"],
  "extra_deny_patterns": ["my-private-project"],
  "outside_root_writes": "approve",
  "auto_delete_in_root": false,
  "default_timeout": 60,
  "max_timeout": 1800,
  "max_output_chars": 12000,
  "max_jobs": 3
}
```

Environment overrides: `TALA_WORKSTATION=0` (disable), `TALA_WORKSTATION_ROOT`,
`TALA_WORKSTATION_WRITE_ROOTS` and `TALA_WORKSTATION_DENY` (`;`-separated),
`TALA_WORKSTATION_PYTHON` (base interpreter for the venv).

## Who gets the tools

* **Main chat** (Tala as the owner): the core five (`run_shell`, `read_file`,
  `write_file`, `list_dir`, `run_python`) are part of the always-offered tool
  floor (`python/harness/tool_selection.py: core_tools()`); the rest are picked
  by the keyword ranker when relevant.
* **Room agents**: `python scripts/grant_workstation_tools.py --agent <name>`
  adds them to the agent's `tool_allowlist` and syncs the AuthorizationGate
  `execute_tool` grants, exactly as the agent editor does.

## Audit

Every call (allowed, held, denied, halted) is one JSON line in
`<root>\.tala\audit\workstation-audit.jsonl`: timestamp, tool, arguments
(clipped), verdict with reasons, approval id, outcome, duration.

## Honest limits

* The shell and Python checks are pattern scanners, not an OS sandbox. They catch
  the ordinary routes (and scripts she wrote herself are scanned before they run),
  but code that builds a path or command at runtime can get past them. Real
  isolation on Windows means running the workstation under a separate low-privilege
  local account (or an AppContainer/Windows Sandbox); that needs admin to set up.
* Job tracking does not survive a harness restart. A job still running at shutdown is not
  guaranteed to stop with the harness; stop jobs before restarting.
* No GUI/browser control here; `desktop_*` tools and `web_browser.py` are separate.
