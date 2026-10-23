"""
settings_nn.py - Auto-generated documentation base.
"""

import json
import logging
import hashlib
from datetime import datetime, timezone
import urllib.request
import urllib.error

# Import database module relative to python/harness
from python.harness import db
from sdk.agent.anchor import canonicalize_and_anchor
from sdk.agent.documentation_nodes import resolve_active_nodes_dir

logger = logging.getLogger(__name__)

def _parse_db_json(val) -> dict:
    if not val:
        return {}
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    try:
        return dict(val)
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# Helper to save settings node to disk and anchor on the blockchain
# ---------------------------------------------------------------------------

SETTINGS_NODES = {
    "settings-inference": "Inference Settings",
    "settings-agent": "Agent Settings",
    "settings-workflows": "Workflows Settings",
    "settings-guardrails": "Guardrails Settings",
    "settings-search": "Search Settings",
    "settings-system": "System Settings",
    "settings-git": "Git Settings",
    "settings-storage": "Storage Settings",
    "settings-database": "Database Settings",
    "settings-runtime": "Runtime Settings",
    "settings-auth": "Auth Settings",
    "settings-firewall": "Firewall Settings",
    "settings-code-registry": "Code Registry Settings",
}

async def _save_settings_node(node_id: str, title: str, content_html: str, state_data: dict):
    """
    Save the settings panel data to a compliant HTML Neural Node.
    Marks it with data-privacy="private" and anchors its hash on the blockchain.
    """
    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)

    current_iso_time = datetime.now(timezone.utc).isoformat()
    
    # Dynamically build relationship anchors to all other settings nodes
    rel_anchors = []
    for other_id, other_title in SETTINGS_NODES.items():
        if other_id != node_id:
            rel_anchors.append(
                f'      <li>\n'
                f'        <a href="nodes/{other_id}.html"\n'
                f'           data-relationship-type="references"\n'
                f'           data-target-node-id="{other_id}">\n'
                f'          {other_title}\n'
                f'        </a>\n'
                f'      </li>'
            )
    rel_html = "\n".join(rel_anchors)
    
    adapter_packet = {
        "activation_keywords": [],
        "tool_affordances": [],
        "forbidden_actions": [],
        "repair_policies": {},
        "validators": {},
        "confidence": 1.0
    }
    
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        node_id, content_html, state_data, adapter_packet
    )
    
    node_provenance = {
        "core_hash": f"sha256:{core_hash}",
        "chaincode_id": chaincode_id,
        "anchoring_status": anchoring_status,
        "raw_response": raw_response,
        "key_fingerprint": public_key if public_key else "unanchored",
        "parent_chaincode_ids": [],
        "created_by": "tala-system",
        "created_at": current_iso_time
    }
    
    state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Settings: {title}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="settings"
  data-schema-version="neural-node/v2"
  data-execution-authority="1.00"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">

  <header id="node-identity">
    <h1 id="node-title">{title}</h1>
  </header>

  <section id="semantic-memory">
    {content_html}
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
{rel_html}
    </ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
{json.dumps(node_provenance, indent=2)}
  </script>
</article>
</body>
</html>
"""
    
    # 4. Compute wrapper hash and inject it
    wrapper_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
    
    html = html.replace(
        '<script type="application/json" id="node-provenance" data-cognitive-block="provenance">\n' + json.dumps(node_provenance, indent=2).replace('"wrapper_hash": "sha256:' + wrapper_hash + '"', '"wrapper_hash": "sha256:..."'),
        '<script type="application/json" id="node-provenance" data-cognitive-block="provenance">\n' + json.dumps(node_provenance, indent=2)
    )
    # the replace logic above is tricky, let's just rebuild the provenance block
    # Actually, simpler: compute wrapper hash, update dict, rebuild HTML
    
    node_provenance["wrapper_hash"] = "..."
    prov_json_placeholder = json.dumps(node_provenance, indent=2)
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Settings: {title}</title>
</head>
<body>
<article
  data-node-id="{node_id}"
  data-node-type="settings"
  data-schema-version="neural-node/v2"
  data-execution-authority="1.00"
  data-memory-authority="1.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">

  <header id="node-identity">
    <h1 id="node-title">{title}</h1>
  </header>

  <section id="semantic-memory">
    {content_html}
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
{rel_html}
    </ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
    wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
    node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
    html = html_template.replace("__PROV_PLACEHOLDER__", json.dumps(node_provenance, indent=2))
    
    # Save to disk
    node_file = nodes_dir / f"{node_id}.html"
    node_file.write_text(html, encoding="utf-8")
    logger.info("Saved settings node: %s", node_file)

# ---------------------------------------------------------------------------
# Individual settings tab generators
# ---------------------------------------------------------------------------

async def update_settings_inference_node():
    try:
        rows = await db.fetch("SELECT id, type, name, priority, status, config, locked FROM inference_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            # Redact API Keys from state_data to prevent leak
            cfg = _parse_db_json(r["config"])
            if "apiKey" in cfg and cfg["apiKey"]:
                cfg["apiKey"] = "********"
            
            providers.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "priority": r["priority"],
                "status": r["status"],
                "config": cfg,
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>Active Inference Providers</h2>",
            "<p>Configured LLM inference backends used for cognitive operations and task completion.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Type</th><th>Priority</th><th>Status</th><th>Model</th></tr></thead>",
            "<tbody>"
        ]
        for p in providers:
            model = p["config"].get("model", "—")
            html_lines.append(f"<tr><td>{p['name']}</td><td>{p['type']}</td><td>{p['priority']}</td><td>{p['status']}</td><td>{model}</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Inference Settings",
            "tags": ["private", "settings", "inference"],
            "providers": providers
        }
        await _save_settings_node("settings-inference", "Inference Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-inference node: %s", exc)

async def update_settings_agent_node():
    try:
        rows = await db.fetch("SELECT id, name, type, role, crew_type, is_active, config FROM agents ORDER BY name ASC")
        agents = []
        for r in rows:
            agents.append({
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "role": r["role"],
                "crew_type": r["crew_type"],
                "is_active": r["is_active"],
                "config": _parse_db_json(r["config"])
            })
            
        html_lines = [
            "<h2>Agent Workforce Personas</h2>",
            "<p>Configured agents and coordination workforce profiles defined inside the Tala Cognitive Engine.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Type</th><th>Role</th><th>Crew Type</th><th>Active</th></tr></thead>",
            "<tbody>"
        ]
        for a in agents:
            html_lines.append(f"<tr><td>{a['name']}</td><td>{a['type']}</td><td>{a['role']}</td><td>{a['crew_type'] or '—'}</td><td>{'Yes' if a['is_active'] else 'No'}</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Agent Settings",
            "tags": ["private", "settings", "agents"],
            "agents": agents
        }
        await _save_settings_node("settings-agent", "Agent Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-agent node: %s", exc)

async def update_settings_workflows_node():
    try:
        from python.harness.workflows_util import get_all_workflows
        workflows = get_all_workflows()
                    
        html_lines = [
            "<h2>Configured Workflows</h2>",
            "<p>Automated task workflows and pipelines registered inside the engine.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Flow ID</th><th>Steps Count</th></tr></thead>",
            "<tbody>"
        ]
        for w in workflows:
            steps_count = len(w.get("nodes", []))
            html_lines.append(f"<tr><td>{w.get('name', 'Unnamed')}</td><td><code>{w.get('flow_id', '—')}</code></td><td>{steps_count}</td></tr>")
        if not workflows:
            html_lines.append("<tr><td colspan='3'>No workflows configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Workflows Settings",
            "tags": ["private", "settings", "workflows"],
            "workflows": workflows
        }
        await _save_settings_node("settings-workflows", "Workflows Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-workflows node: %s", exc)

async def update_settings_guardrails_node():
    try:
        rows = await db.fetch("SELECT category, rules FROM guardrails_config WHERE id = 1")
        category = "none"
        rules = {}
        if rows:
            category = rows[0]["category"]
            rules = _parse_db_json(rows[0]["rules"])
            
        html_lines = [
            "<h2>Guardrails Safety Configuration</h2>",
            f"<p>Active safety profile category: <strong>{category.upper()}</strong></p>",
            "<table>",
            "<thead><tr><th>Safety Rule ID</th><th>Status / Configuration</th></tr></thead>",
            "<tbody>"
        ]
        for k, v in rules.items():
            html_lines.append(f"<tr><td><code>{k}</code></td><td>{v}</td></tr>")
        if not rules:
            html_lines.append("<tr><td colspan='2'>No guardrail rules active.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Guardrails Settings",
            "tags": ["private", "settings", "guardrails"],
            "category": category,
            "rules": rules
        }
        await _save_settings_node("settings-guardrails", "Guardrails Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-guardrails node: %s", exc)

async def update_settings_search_node():
    try:
        rows = await db.fetch("SELECT id, name, endpoint, priority, locked FROM search_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            providers.append({
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "priority": r["priority"],
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>Search Providers</h2>",
            "<p>Web search backends used for research and crawl operations.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Endpoint</th><th>Priority</th><th>Locked/Forced</th></tr></thead>",
            "<tbody>"
        ]
        for p in providers:
            status_text = "Yes" if p["locked"] else "No"
            html_lines.append(f"<tr><td>{p['name']}</td><td><code>{p['endpoint']}</code></td><td>{p['priority']}</td><td>{status_text}</td></tr>")
        if not providers:
            html_lines.append("<tr><td colspan='4'>No search providers configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Search Settings",
            "tags": ["private", "settings", "search"],
            "providers": providers
        }
        await _save_settings_node("settings-search", "Search Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-search node: %s", exc)

async def update_settings_system_node():
    try:
        import platform
        import multiprocessing
        
        os_info = {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor() or platform.machine(),
            "node": platform.node(),
        }
        
        cpu_count_logical = multiprocessing.cpu_count()
        cpu_count_physical = cpu_count_logical
        cpu_freq_mhz = 0
        ram_total_gb = 0
        disk_total_gb = 0
        
        try:
            import psutil
            cpu_count_physical = psutil.cpu_count(logical=False) or cpu_count_logical
            cpu_freq = psutil.cpu_freq()
            cpu_freq_mhz = round(cpu_freq.current) if cpu_freq else 0
            vm = psutil.virtual_memory()
            ram_total_gb = round(vm.total / (1024 ** 3), 2)
            disk = psutil.disk_usage("/")
            disk_total_gb = round(disk.total / (1024 ** 3), 1)
        except ImportError:
            pass
            
        html_lines = [
            "<h2>System Hardware & OS Specifications</h2>",
            "<table>",
            "<thead><tr><th>Specification</th><th>Value</th></tr></thead>",
            "<tbody>",
            f"<tr><td>Host Hostname</td><td><code>{os_info['node']}</code></td></tr>",
            f"<tr><td>OS Platform</td><td>{os_info['system']} {os_info['release']}</td></tr>",
            f"<tr><td>OS Version</td><td>{os_info['version']}</td></tr>",
            f"<tr><td>Architecture</td><td>{os_info['machine']}</td></tr>",
            f"<tr><td>Physical / Logical Cores</td><td>{cpu_count_physical} Phys / {cpu_count_logical} Log</td></tr>",
            f"<tr><td>CPU Freq</td><td>{cpu_freq_mhz} MHz</td></tr>",
            f"<tr><td>RAM Installed</td><td>{ram_total_gb} GB</td></tr>",
            f"<tr><td>Disk Capacity</td><td>{disk_total_gb} GB</td></tr>",
            f"<tr><td>Python Runtime</td><td>{platform.python_version()}</td></tr>",
            "</tbody></table>"
        ]
        
        state_data = {
            "title": "System Specs",
            "tags": ["private", "settings", "system"],
            "os": os_info,
            "cpu": {
                "logical_cores": cpu_count_logical,
                "physical_cores": cpu_count_physical,
                "freq_mhz": cpu_freq_mhz
            },
            "ram": {"total_gb": ram_total_gb},
            "disk": {"total_gb": disk_total_gb}
        }
        await _save_settings_node("settings-system", "System Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-system node: %s", exc)

async def update_settings_git_node():
    try:
        rows = await db.fetch("SELECT id, name, endpoint, username, priority, locked FROM git_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            providers.append({
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "username": r["username"],
                "priority": r["priority"],
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>Git Hosting Providers</h2>",
            "<p>Configured repository hosts used for syncing workspace files.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Endpoint</th><th>Username</th><th>Priority</th><th>Locked/Forced</th></tr></thead>",
            "<tbody>"
        ]
        for p in providers:
            status_text = "Yes" if p["locked"] else "No"
            html_lines.append(f"<tr><td>{p['name']}</td><td><code>{p['endpoint']}</code></td><td>{p['username']}</td><td>{p['priority']}</td><td>{status_text}</td></tr>")
        if not providers:
            html_lines.append("<tr><td colspan='5'>No git providers configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Git Settings",
            "tags": ["private", "settings", "git"],
            "providers": providers
        }
        await _save_settings_node("settings-git", "Git Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-git node: %s", exc)

async def update_settings_storage_node():
    try:
        rows = await db.fetch("SELECT id, name, provider_type, priority, locked FROM storage_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            providers.append({
                "id": r["id"],
                "name": r["name"],
                "provider_type": r["provider_type"],
                "priority": r["priority"],
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>Cloud Storage Providers</h2>",
            "<p>Configured remote / cloud file storage backends.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Type</th><th>Priority</th><th>Forced</th></tr></thead>",
            "<tbody>"
        ]
        for p in providers:
            forced_str = "Yes" if p["locked"] else "No"
            html_lines.append(f"<tr><td>{p['name']}</td><td>{p['provider_type']}</td><td>{p['priority']}</td><td>{forced_str}</td></tr>")
        if not providers:
            html_lines.append("<tr><td colspan='4'>No storage providers configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Storage Settings",
            "tags": ["private", "settings", "storage"],
            "providers": providers
        }
        await _save_settings_node("settings-storage", "Storage Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-storage node: %s", exc)

async def update_settings_database_node():
    try:
        rows = await db.fetch("SELECT id, name, endpoint, username, database, priority, locked FROM database_providers ORDER BY priority ASC")
        providers = []
        for r in rows:
            providers.append({
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "username": r["username"],
                "database": r["database"],
                "priority": r["priority"],
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>External Databases</h2>",
            "<p>Configured external database connections and replica servers.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Endpoint</th><th>Database</th><th>Username</th><th>Priority</th><th>Forced</th></tr></thead>",
            "<tbody>"
        ]
        for p in providers:
            forced_str = "Yes" if p["locked"] else "No"
            html_lines.append(f"<tr><td>{p['name']}</td><td><code>{p['endpoint']}</code></td><td>{p['database']}</td><td>{p['username']}</td><td>{p['priority']}</td><td>{forced_str}</td></tr>")
        if not providers:
            html_lines.append("<tr><td colspan='6'>No database providers configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Database Settings",
            "tags": ["private", "settings", "database"],
            "providers": providers
        }
        await _save_settings_node("settings-database", "Database Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-database node: %s", exc)

async def update_settings_auth_node():
    try:
        rows = await db.fetch("SELECT id, name, auth_type, priority, locked FROM auth_credentials ORDER BY priority ASC")
        credentials = []
        for r in rows:
            credentials.append({
                "id": r["id"],
                "name": r["name"],
                "auth_type": r["auth_type"],
                "priority": r["priority"],
                "locked": r["locked"]
            })
            
        html_lines = [
            "<h2>Auth Credentials</h2>",
            "<p>Configured API keys, SSH keys, and server authentication protocols. <em>All private keys and secrets are redacted from the knowledge layer.</em></p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Auth Type</th><th>Priority</th><th>Forced</th></tr></thead>",
            "<tbody>"
        ]
        for c in credentials:
            forced_str = "Yes" if c["locked"] else "No"
            html_lines.append(f"<tr><td>{c['name']}</td><td>{c['auth_type']}</td><td>{c['priority']}</td><td>{forced_str}</td></tr>")
        if not credentials:
            html_lines.append("<tr><td colspan='4'>No auth credentials configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Auth Settings",
            "tags": ["private", "settings", "auth"],
            "credentials": credentials
        }
        await _save_settings_node("settings-auth", "Auth Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-auth node: %s", exc)

async def update_settings_firewall_node():
    try:
        rows = await db.fetch("SELECT id, name, access_rights, priority FROM firewall_contacts ORDER BY priority ASC")
        contacts = []
        for r in rows:
            contacts.append({
                "id": r["id"],
                "name": r["name"],
                "access_rights": _parse_db_json(r["access_rights"]),
                "priority": r["priority"]
            })
            
        html_lines = [
            "<h2>Firewall Contacts & Access Control</h2>",
            "<p>Trusted peer contacts and their cryptographic clearance levels.</p>",
            "<table>",
            "<thead><tr><th>Name</th><th>Peer Address / ID</th><th>Clearance Level</th><th>Priority</th></tr></thead>",
            "<tbody>"
        ]
        for c in contacts:
            clearance = c["access_rights"].get("clearance_level", "—")
            html_lines.append(f"<tr><td>{c['name']}</td><td><code>{c['id']}</code></td><td>{clearance}</td><td>{c['priority']}</td></tr>")
        if not contacts:
            html_lines.append("<tr><td colspan='4'>No firewall contacts configured.</td></tr>")
        html_lines.append("</tbody></table>")
        
        state_data = {
            "title": "Firewall Settings",
            "tags": ["private", "settings", "firewall"],
            "contacts": contacts
        }
        await _save_settings_node("settings-firewall", "Firewall Settings", "\n".join(html_lines), state_data)
    except Exception as exc:
        logger.exception("Failed to update settings-firewall node: %s", exc)


async def _update_settings_code_registry_node():
    """
    Mirror CodeRegistry (sdk/agent/code_registry.py) into an HTML-NN settings
    node, the same way every other settings category (system, inference,
    runtime, ...) is mirrored. CodeRegistry's code style/boilerplate template
    key-value pairs already persist reliably in the client_settings table --
    this doesn't fix a data-loss bug, it closes a visibility gap: unlike
    every other settings category, that data previously had no HTML-NN
    representation, so it never surfaced through context-injection or
    semantic search the way the model's other configuration does.
    """
    try:
        from sdk.agent.code_registry import CodeRegistry

        registry_entries = await CodeRegistry.list_keys("tala.code.")

        html_lines = [
            "<h2>Code Style & Boilerplate Template Registry</h2>",
            "<p>Key-value code generation styles and templates consulted during code synthesis.</p>",
            "<table>",
            "<thead><tr><th>Key</th><th>Value</th></tr></thead>",
            "<tbody>",
        ]
        for key in sorted(registry_entries.keys()):
            value = registry_entries[key]
            display_value = str(value)
            if "\n" in display_value or len(display_value) > 60:
                cell = f"<pre>{display_value}</pre>"
            else:
                cell = display_value
            html_lines.append(f"<tr><td><code>{key}</code></td><td>{cell}</td></tr>")
        if not registry_entries:
            html_lines.append("<tr><td colspan='2'>No code registry entries configured.</td></tr>")
        html_lines.append("</tbody></table>")

        state_data = {
            "title": "Code Registry",
            "tags": ["private", "settings", "code-registry"],
            "registry_entries": registry_entries,
        }
        await _save_settings_node(
            "settings-code-registry", "Code Registry Settings", "\n".join(html_lines), state_data
        )
    except Exception as exc:
        logger.exception("Failed to update settings-code-registry node: %s", exc)

# ---------------------------------------------------------------------------
# Core synchronization facade
# ---------------------------------------------------------------------------

async def sync_settings_to_nodes():
    """Sync all 13 settings panels to HTML Neural Nodes in the background."""
    logger.info("Starting synchronization of settings panels to HTML Neural Nodes...")
    await update_settings_inference_node()
    await update_settings_agent_node()
    await update_settings_workflows_node()
    await update_settings_guardrails_node()
    await update_settings_search_node()
    await update_settings_system_node()
    await update_settings_git_node()
    await update_settings_storage_node()
    await update_settings_database_node()
    await update_settings_auth_node()
    await update_settings_firewall_node()
    await _update_settings_code_registry_node()
    logger.info("Completed synchronization of all 13 settings panel HTML Neural Nodes.")

# Alias for backwards compatibility across harness and tools
update_all_settings_nodes = sync_settings_to_nodes



async def save_agent_persona_node(agent_id: str, name: str, role: str, config: dict):
    """
    Generate or update a persona HTML Neural Node file for a custom agent.
    Includes the agent's tag, system prompt, and metadata.
    """
    nodes_dir = resolve_active_nodes_dir()
    nodes_dir.mkdir(parents=True, exist_ok=True)

    current_iso_time = datetime.now(timezone.utc).isoformat()
    
    system_prompt = config.get("system_prompt", "")
    tag = config.get("tag", "").strip()
    
    tags = ["persona"]
    if tag:
        tags.append(tag)
        
    state_data = {
        "title": name,
        "description": "Custom agent persona profile.",
        "tags": tags,
        "role": role,
        "config": config
    }
    state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
    
    content_html = f"<h2>System Directive</h2>\n<p>{system_prompt}</p>"
    
    adapter_packet = {
        "activation_keywords": [],
        "tool_affordances": [],
        "forbidden_actions": [],
        "repair_policies": {},
        "validators": {},
        "confidence": 0.8
    }
    
    core_hash, chaincode_id, anchoring_status, raw_response, public_key = canonicalize_and_anchor(
        f"persona-{agent_id}", content_html, state_data, adapter_packet
    )
    
    node_provenance = {
        "core_hash": f"sha256:{core_hash}",
        "chaincode_id": chaincode_id,
        "anchoring_status": anchoring_status,
        "raw_response": raw_response,
        "key_fingerprint": public_key if public_key else "unanchored",
        "parent_chaincode_ids": [],
        "created_by": "tala-system",
        "created_at": current_iso_time
    }

    state_json = json.dumps(state_data, indent=2, ensure_ascii=False)
    
    node_provenance["wrapper_hash"] = "..."
    prov_json_placeholder = json.dumps(node_provenance, indent=2)
    
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Persona: {name}</title>
</head>
<body>
<article
  data-node-id="{agent_id}"
  data-node-type="persona"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.50"
  data-memory-authority="1.00"
  data-weight-relevance="0.60"
  data-weight-confidence="0.50"
  data-chaincode-id="{chaincode_id}"
  data-content-hash="{core_hash}"
  data-source="local"
  data-trust-tier="system"
  data-privacy="private">
  
  <header id="node-identity">
    <h1 id="node-title">{name}</h1>
  </header>
  
  <section id="semantic-memory">
    {content_html}
  </section>
  
  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
    </ul>
  </nav>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{state_json}
  </script>

  <script type="application/json" id="adapter-packet" data-cognitive-block="adapter">
{json.dumps(adapter_packet, indent=2)}
  </script>

  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">
__PROV_PLACEHOLDER__
  </script>
</article>
</body>
</html>
"""
    wrapper_hash = hashlib.sha256(html_template.replace("__PROV_PLACEHOLDER__", prov_json_placeholder).encode("utf-8")).hexdigest()
    node_provenance["wrapper_hash"] = f"sha256:{wrapper_hash}"
    html = html_template.replace("__PROV_PLACEHOLDER__", json.dumps(node_provenance, indent=2))
    
    node_file = nodes_dir / f"persona-{agent_id}.html"
    node_file.write_text(html, encoding="utf-8")
    logger.info("Saved agent persona node: %s", node_file)


async def delete_agent_persona_node(agent_id: str):
    """Delete a persona HTML Neural Node file for an agent and anchor on blockchain."""
    nodes_dir = resolve_active_nodes_dir()

    node_file = nodes_dir / f"persona-{agent_id}.html"
    if node_file.exists():
        node_file.unlink()
        logger.info("Deleted agent persona node: %s", node_file)
        
        # Anchor deletion on blockchain
        try:
            tx_data = json.dumps({
                "id": f"persona-{agent_id}",
                "type": "node-delete",
                "hash": "0" * 64,
                "creator": "tala-system",
                "counterparty": "blockchain-ledger"
            }).encode("utf-8")
            
            headers = {
                "Content-Type": "application/json",
                "Content-Length": len(tx_data),
            }
            # /chain/tx requires the local admin secret (or a signature);
            # this is a local system write, so the secret is the right proof.
            try:
                from sdk.agent.ghost_persistence import _local_chain_admin_headers
                headers.update(_local_chain_admin_headers())
            except Exception:
                logger.debug("could not attach the local chain admin secret", exc_info=True)

            from sdk.agent.ghost_persistence import local_chain_base_url

            req = urllib.request.Request(
                f"{local_chain_base_url()}/chain/tx",
                data=tx_data,
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=1.0) as response:
                response.read()

            # Deleting a persona on this machine is local activity. The
            # "blockchain-ledger" counterparty names the chain, not a party,
            # so this is Ghost and is never offered to the shared chain.
            from python.harness import chains
            chains.index_sync(
                chains.GHOST, f"persona-{agent_id}", "node-delete",
                counterparty="blockchain-ledger",
                document_ref=f"persona-{agent_id}",
            )
        except Exception as exc:
            logger.debug("Failed to anchor agent persona node deletion on blockchain: %s", exc)

