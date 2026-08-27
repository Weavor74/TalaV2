"""Generate ui-view-* HTML Neural Nodes from the UI audit.

Deliberately writes into the `ui-*` namespace, NOT `program-doc-*`, so the
self_documenter's fingerprint regeneration cannot flatten these (UI-009).
"""
import json
import html
from datetime import datetime, timezone
from pathlib import Path

NODES = Path("nodes")
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def esc(t):
    return html.escape(str(t), quote=False)


def node_html(node_id, title, node_type, prose, sections, rels, state, mem="0.70"):
    secs = ""
    for heading, body in sections:
        secs += f'\n  <section id="{heading[0]}">\n    <h2>{esc(heading[1])}</h2>\n{body}  </section>\n'

    rel_items = ""
    for target, label, rtype, role in rels:
        rel_items += (
            f'      <li><a href="./{target}.html" data-target-node-id="{target}" '
            f'data-relationship-type="{rtype}" data-relationship-weight="0.90" '
            f'data-navigation-role="{role}" data-authority-required="none">{esc(label)}</a></li>\n'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v2">
  <title>{esc(title)}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="{node_type}"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="{mem}"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="0.95"
  data-weight-behavior-influence="0.00"
  data-weight-governance-importance="0.70"
  data-governance-status="context-only"
  data-source="local"
  data-trust-tier="system"
  data-privacy="internal">

  <header id="node-identity">
    <h1 id="node-title">{esc(title)}</h1>
  </header>

  <section id="semantic-memory">
{prose}  </section>
{secs}
  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
{rel_items}    </ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{json.dumps(state, indent=2, ensure_ascii=False)}
  </script>
</article>
</body>
</html>
"""


def table(rows, headers):
    out = "    <table>\n      <tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>\n"
    for r in rows:
        out += "      <tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>\n"
    return out + "    </table>\n"


def ul(items):
    return "    <ul>\n" + "".join(f"      <li>{esc(i)}</li>\n" for i in items) + "    </ul>\n"


def p(text):
    return f"    <p>{esc(text)}</p>\n"


# ── the rail ────────────────────────────────────────────────────────────────
RAIL = [
    ("btn-toggle-drawer", "hamburger", "Toggle File Explorer", "-"),
    ("browser", "globe", "Web Browser", "ui-view-browser"),
    ("research", "book", "Research Library", "ui-view-research"),
    ("neural-node", "neural", "Neural Nodes", "ui-view-neural-node"),
    ("blockchain", "chain", "Blockchain Ledger", "ui-view-blockchain"),
    ("agent-builder", "arrows", "Agent Builder", "ui-view-agent-builder"),
    ("mission-control", "circle", "Mission Control", "ui-view-mission-control"),
    ("vault", "person", "Vault Directory", "ui-view-vault"),
    ("contacts", "people", "Contacts Directory", "ui-view-contacts"),
    ("btn-app-quit", "power", "Shutdown App", "-"),
    ("settings", "gear", "System Settings", "ui-view-settings"),
]

VIEWS = {}

VIEWS["neural-node"] = dict(
    title="UI View: Neural Nodes",
    steven="This is the landing page when it first starts up.",
    reach="Sidebar rail, 4th icon (neural). Default view on startup.",
    purpose="Browse every HTML Neural Node as cards or as a graph.",
    controls=[
        ("btn-view-grid", "Switch the node list to Grid", "works"),
        ("btn-view-graph", "Switch the node list to Graph", "works"),
        ("node-search-input", "Search nodes", "works"),
        ("btn-refresh-nodes", "Refresh", "works"),
        ("btn-new-node", "+ New Node", "works"),
    ],
    reads=[
        "Card badge renders data-node-type.",
        "Exec: and Mem: pills render data-execution-authority and data-memory-authority. Steven calls these authority weights.",
        "Trust: renders data-trust-tier.",
    ],
    broken=[],
)

VIEWS["browser"] = dict(
    title="UI View: Web Browser",
    steven="Tala is supposed to have full control of this web browser.",
    reach="Sidebar rail, 2nd icon (globe).",
    purpose="An embedded Electron webview with back / forward / reload and an address bar.",
    controls=[
        ("btn-browser-back", "Back", "works"),
        ("btn-browser-forward", "Forward", "works"),
        ("btn-browser-refresh", "Reload", "works"),
        ("browser-url-input", "Address bar; Enter navigates", "works"),
        ("browser-iframe", "The page itself. Named iframe but is a <webview>", "works"),
    ],
    reads=[
        "Address bar self-updates from did-navigate and did-navigate-in-page.",
        "Python can push this browser to a URL via the browser.navigate notification.",
    ],
    broken=[
        "UI-013: Tala's six browser tools drive a headless Playwright browser, not this one.",
        "UI-014: browser.get_dom is a stub that returns success.",
        "UI-015: browser.execute_js broadcasts to a channel with no listener.",
        "UI-017: the READY badge is static decoration.",
    ],
)

VIEWS["research"] = dict(
    title="UI View: Research Library",
    steven="This is the research view, it is meant to work like NotebookLM. One of the key features is that it will be able to use the search engine that the user adds with a search API in a later tab.",
    reach="Sidebar rail, 3rd icon (book). Titled Evidence Browser.",
    purpose="Search the web and Tala's own memory together, read a result in full, and distil selected results into a Neural Node.",
    controls=[
        ("research-subject", "Enter Query", "works"),
        ("research-search-engine", "Search Engine picker, populated from configured providers", "works"),
        ("toggle-source-all", "Search all sources", "works"),
        ("research-sources-list", "Result list; click a row to load its context", "works"),
        ("btn-research-search", "Search", "works"),
        ("btn-research-save-node", "Save / Distill selected into a node", "works"),
    ],
    reads=[
        "Every snippet is scanned for prompt injection before display.",
        "Per-source errors have a red banner render path.",
    ],
    broken=[
        "UI-018: a failing search provider silently degrades to DuckDuckGo scraping.",
        "UI-019: the /search substring misroutes most commercial search APIs.",
        "UI-022: no way to flag a result into chat.",
    ],
)

VIEWS["blockchain"] = dict(
    title="UI View: Blockchain Ledger",
    steven="This is supposed to track the chain for both Ghost chain and Real Chain. Ghost chain essentially is non-value other than tracking transaction locally. But the real chain only happens when connected to the internet.",
    reach="Sidebar rail, 5th icon (chain).",
    purpose="Show the local Proof-of-Authority chain as a flow of block cards.",
    controls=[
        ("btn-refresh-blockchain", "Refresh the chain", "works"),
        ("chain-height", "Block height, live", "works"),
        ("chain-validity", "VALID / COMPROMISED / UNREACHABLE, live", "works"),
    ],
    reads=[
        "Served by blockchain/src/app.js on port 8332.",
        "Offline handling is genuinely good: clear error, height 0 (Offline), validity UNREACHABLE.",
    ],
    broken=[
        "UI-023: Ghost/Real is a runtime mode that is never recorded on a transaction.",
        "UI-024: no transaction has ever been written in Real mode.",
        "UI-025: the renderer hardcodes 127.0.0.1:8332.",
    ],
)

VIEWS["agent-builder"] = dict(
    title="UI View: Agent Builder",
    steven="Agent builder.",
    reach="Sidebar rail, 6th icon (arrows). Four sub-tabs via data-builder-tab.",
    purpose="Manage worker personas, author swarm topologies, watch the task board, and monitor swarm runs.",
    controls=[
        ("builder-tab-workers", "Workers sub-tab", "works"),
        ("builder-tab-topologies", "Topologies sub-tab", "works"),
        ("builder-tab-taskboard", "Task Board sub-tab", "works"),
        ("builder-tab-runs", "Runs sub-tab", "works"),
        ("btn-topology-new", "New topology", "works"),
        ("btn-topology-edit", "Edit topology", "works"),
        ("btn-topology-delete", "Delete topology", "works"),
        ("btn-topology-start", "Run Swarm", "works"),
        ("kanban-pending", "Inbox column", "works"),
        ("kanban-planned", "Planned column", "works"),
        ("kanban-ready", "Ready column", "works"),
        ("kanban-running", "Running column", "works"),
        ("kanban-review", "Review column", "works"),
        ("kanban-blocked", "Blocked column", "works"),
        ("kanban-done", "Done column", "works"),
    ],
    reads=[
        "Topologies are authored as HTML-NN nodes and labelled neural node in the UI.",
        "A swarm can be a member of a crew but is not a crew itself.",
    ],
    broken=[
        "UI-029: task descriptions are injected into innerHTML unescaped.",
        "UI-030: the board silently shows only the newest 50 tasks.",
        "UI-033: Manage Workers in Settings targets a selector that does not exist.",
    ],
)

VIEWS["mission-control"] = dict(
    title="UI View: Mission Control",
    steven="These are tasks being worked on.",
    reach="Sidebar rail, 7th icon (circle). Polls every 4 seconds while active.",
    purpose="The active task queue plus codebase diagnostics.",
    controls=[
        ("btn-mc-refresh", "Refresh everything", "works"),
        ("btn-mc-halt", "HALT LOOP", "works"),
        ("btn-mc-task-new", "New Task", "works"),
        ("btn-mc-task-clear", "Clear Queue", "works"),
        ("btn-mc-scan", "Scan Repo", "works"),
        ("btn-mc-lint", "Run Linter", "works"),
        ("btn-mc-revert", "Revert", "works"),
    ],
    reads=[
        "Editing an in_progress or running task is refused unless forced, because the goal loop writes its own status back.",
        "paused has its own colour so it does not read as pending.",
    ],
    broken=[
        "UI-034: nothing ever ages a completed task out.",
        "UI-035: cleared tasks reappear because the test suite writes to the live database.",
    ],
)

VIEWS["vault"] = dict(
    title="UI View: Vault",
    steven="Vault section does not work at all. It is meant for storage of editable private documents. Things the local LLM could reference, but nothing gets shared outside.",
    reach="Sidebar rail, 8th icon (person).",
    purpose="Private documents, stored as HTML-NN typed vault and encrypted at rest. The local model can read and search them; they never leave this machine.",
    controls=[
        ("btn-vault-new", "+ New Document", "works"),
        ("vault-search-input", "Filter documents by title or tag", "works"),
        ("vault-doc-title", "Document title, stored in clear so the vault can be listed", "works"),
        ("vault-doc-tags", "Comma separated tags", "works"),
        ("vault-doc-body", "Document contents, sealed before they touch disk", "works"),
        ("vault-grant-remote", "Allow this document to be read while a remote model is active", "works"),
        ("vault-grant-contacts", "Contacts this specific document may be sent to", "works"),
        ("btn-vault-save", "Save and seal", "works"),
        ("btn-vault-delete", "Delete, with no backup to recover from", "works"),
    ],
    reads=[
        "A vault document is an HTML-NN with data-node-type=vault. Its body is encrypted with the same key credentials use; its title and tags are not, so the vault can be listed without the key.",
        "Steven settled four separate rules for 'nothing gets shared outside': remote models are refused unless that document carries an explicit grant; vault bodies ARE indexed locally into ChromaDB; backup and sync never see the vault or its index; contacts get access only by per-document invite.",
        "Every check fails closed. Not knowing which inference provider is active is not permission.",
        "The agent's tools are vault_list, vault_read and vault_search, and they are read-only: writing and deleting stay with the user.",
    ],
    broken=[
        "UI-036 is closed, but note the view is a text editor only. There is no file drop or import, so a PDF or an image cannot be put in the vault yet.",
    ],
)

VIEWS["contacts"] = dict(
    title="UI View: Contacts Directory",
    steven="When finally online we will be able to add people to this based on their remote wallet ID. This also is part of what allows remote users to access local user skills.",
    reach="Sidebar rail, 9th icon (people).",
    purpose="Peer identity and transport: username, wallet address, endpoint, shared secret, and sharing permissions.",
    controls=[
        ("btn-contacts-add", "+ Add Contact", "works"),
        ("contacts-search-input", "Filter the list", "works"),
        ("btn-contacts-save", "Save Permissions", "works"),
        ("btn-contacts-connect", "Connect (p2p handshake)", "works"),
        ("btn-contacts-delete", "Delete Contact", "works"),
    ],
    reads=[
        "Packet signing is real: HMAC-SHA256 over canonical JSON, compared with compare_digest.",
        "Being a contact confers no access. Access is granted per resource, by invitation.",
    ],
    broken=[
        "UI-040: the skills permission does not gate p2p.marketplace_search.",
        "UI-041: an enrolling peer currently chooses its own permissions.",
        "UI-043: contacts have no blockchain integration yet.",
    ],
)

VIEWS["settings"] = dict(
    title="UI View: System Settings",
    steven="settings.<panel>",
    reach="Sidebar rail, bottom gear icon. 18 panels selected by data-panel.",
    purpose="All configuration. Eighteen panels under one nav.",
    controls=[
        ("settings-tab-nav", "Panel switcher, 18 entries", "works"),
        ("settings-tab-nav", "Panel switcher, 18 entries", "works"),
        ("btn-settings-export", "EXPORT", "dead"),
        ("btn-settings-import", "IMPORT", "dead"),
        ("btn-settings-apply", "APPLY CHANGES", "dead"),
    ],
    reads=[
        "Panels: inference, agent, identity, workflows, guardrails, search, system, git, storage, database, runtime, plugins, auth, firewall, channels, logging, architecture, about.",
        "Panels currently save on change; APPLY CHANGES is to become staged-then-apply.",
    ],
    broken=[
        "UI-044: the environment and scope selectors are cosmetic.",
        "UI-045: EXPORT, IMPORT and APPLY CHANGES have no handlers.",
        "UI-109: the env buttons were designed as presets setting data root, inference and storage together.",
    ],
)

VIEWS["editor"] = dict(
    title="UI View: Editor",
    steven="The editing section was meant to be the center pane. Her original usage intent was a method to edit files and documents. Be them txt, images or whatever. This included code.",
    reach="No sidebar entry by design. It is the centre pane the Explorer opens a file into.",
    purpose="Tala's original purpose: editing files and documents of any type.",
    controls=[
        ("editor-textarea", "The editor", "partial"),
        ("editor-preview-pane", "Rendered preview", "partial"),
        ("btn-editor-edit-mode", "EDIT mode, in the tab strip", "works"),
        ("btn-editor-preview-mode", "PREVIEW mode, in the tab strip", "works"),
        ("btn-save-file", "SAVE, in the tab strip", "works"),
    ],
    reads=[
        "Preview is gated to .md and .html only.",
        "Steven wants all formats covered, image and text.",
    ],
    broken=[
        "UI-114: the editor is a textarea. Code has no language awareness and images cannot open at all.",
        "UI-115: the Explorer never lists a user file, so the editor is unreachable by its only intended route.",
    ],
)

VIEWS["logs"] = dict(
    title="UI View: System Logs",
    steven="-",
    reach="Registered as a TabManager destination but nothing ever opens it.",
    purpose="Vestigial. Its own text points to the bottom panel tabs.",
    controls=[],
    reads=[
        "Logs have two real homes: the bottom panel tabs, and the settings.logging panel.",
    ],
    broken=[
        "UI-117: a stub with no trigger. Delete it or give it a purpose.",
    ],
)


def build_view(key, v):
    node_id = f"ui-view-{key}"
    prose = p(f"{v['purpose']} Reached: {v['reach']}")
    if v["steven"] and v["steven"] != "-":
        prose += p(f"The operator describes this screen as: “{v['steven']}”")
    if v["broken"]:
        prose += p(f"{len(v['broken'])} known defect(s) affect this screen; see the Known defects section before relying on it.")

    sections = []
    if v["controls"]:
        sections.append((("controls", "Controls"),
                         table(v["controls"], ["Element id", "What it does", "Verdict"])))
    if v["reads"]:
        sections.append((("behaviour", "Behaviour notes"), ul(v["reads"])))
    if v["broken"]:
        sections.append((("known-defects", "Known defects"), ul(v["broken"])))

    rels = [("ui-map-index", "UI Map Index", "part-of", "primary-context"),
            ("system-neural-node-v2-schema", "HTML-NN v2 Schema", "bounded-by", "schema-contract")]

    state = {
        "title": v["title"],
        "description": v["purpose"],
        "tags": ["ui", "ui-view", "navigation", key],
        "view_id": f"view-{key}",
        "reached_by": v["reach"],
        "operator_label": v["steven"],
        "controls": [{"id": c[0], "does": c[1], "verdict": c[2]} for c in v["controls"]],
        "known_defects": v["broken"],
        "metadata": {"author": "ui-audit", "created_or_updated": NOW,
                     "schema": "neural-node/v2", "source": "docs/ui-audit-findings.md"},
    }
    return node_id, node_html(node_id, v["title"], "documentation", prose, sections, rels, state)


def build_index():
    prose = p("Index of Tala's user interface. Eleven views and eighteen settings panels, "
              "with how each is reached. Start here to navigate the UI, then open the "
              "ui-view-* node for the screen in question.")
    prose += p("Nine views are reached from the left icon rail. The Editor is the centre pane the "
               "file Explorer opens into, and the Logs view is vestigial.")

    sections = [
        (("sidebar-rail", "Sidebar rail, top to bottom"),
         table([(r[0], r[1], r[2], r[3]) for r in RAIL],
               ["id or data-view", "Icon", "Tooltip", "Node"])),
        (("settings-panels", "Settings panels"),
         p("Selected by data-panel on #settings-tab-nav: inference, agent, identity, workflows, "
           "guardrails, search, system, git, storage, database, runtime, plugins, auth, firewall, "
           "channels, logging, architecture, about.")),
        (("chrome", "Persistent chrome"),
         ul(["global-command-bar: Ctrl+P command bar, top centre.",
             "workspace-tabs: open tabs, dynamically managed.",
             "terminal-panel: bottom dock with TERMINAL 1, Blockchain Node, Python Harness.",
             "status-bar: WS status, token balance, active agent, version.",
             "Agent Chat: right panel. Green dot means active agent."])),
    ]
    rels = [(f"ui-view-{k}", VIEWS[k]["title"], "contains", "child-view") for k in VIEWS]
    rels.append(("system-neural-node-v2-schema", "HTML-NN v2 Schema", "bounded-by", "schema-contract"))

    state = {
        "title": "UI Map Index",
        "description": "Index of every Tala UI view and settings panel, and how each is reached.",
        "tags": ["ui", "ui-map", "navigation", "index"],
        "views": {f"view-{k}": f"ui-view-{k}" for k in VIEWS},
        "sidebar_rail": [{"target": r[0], "icon": r[1], "tooltip": r[2], "node": r[3]} for r in RAIL],
        "settings_panels": ["inference", "agent", "identity", "workflows", "guardrails", "search",
                            "system", "git", "storage", "database", "runtime", "plugins", "auth",
                            "firewall", "channels", "logging", "architecture", "about"],
        "metadata": {"author": "ui-audit", "created_or_updated": NOW,
                     "schema": "neural-node/v2", "source": "docs/ui-audit-findings.md"},
    }
    return "ui-map-index", node_html("ui-map-index", "UI Map Index", "concept", prose, sections, rels, state, mem="0.85")


written = []
nid, doc = build_index()
(NODES / f"{nid}.html").write_text(doc, encoding="utf-8")
written.append(nid)

for key, v in VIEWS.items():
    nid, doc = build_view(key, v)
    (NODES / f"{nid}.html").write_text(doc, encoding="utf-8")
    written.append(nid)

print(f"wrote {len(written)} nodes")
for w in written:
    print("  ", w)
