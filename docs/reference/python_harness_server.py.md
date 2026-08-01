# File: `server.py`

## Module Description

server.py — Tala Cognitive Engine Room: JSON-RPC 2.0 WebSocket Harness Server
==============================================================================
Entry point for the Python backend.  Runs a WebSocket server on
``ws://127.0.0.1:8765`` (configurable via Config) that speaks JSON-RPC 2.0.

Registered method handlers
---------------------------
  inference.generate  — stream a vLLM completion for a given prompt
  db.query            — raw parameterised SELECT against PostgreSQL
  db.insert           — parameterised INSERT, returns inserted row(s)
  token.count         — count tokens without deducting balance
  graph.activate      — activate a node in the cognitive graph
  vault.status        — return vault filesystem status

Architecture notes
------------------
- ALL durable state flows through db.py (PostgreSQL).
- mem0 is NEVER used for durable writes — only wrapped read-only via
  mem0_adapter.py.
- Token gate (token_counter.py) is called before every inference.generate.
- Graceful shutdown on SIGTERM / SIGINT: pool is drained, connections closed.

Milestone 1.2 — Tasks 1.2.1–1.2.4 (server orchestration layer)

## Classes

### `class JsonRpcError`
Standard JSON-RPC 2.0 error codes.


## Functions

### `def get_chat_mode_file_path()`
*No docstring provided.*

### `def get_current_chat_mode()`
*No docstring provided.*

### `def save_current_chat_mode(mode)`
*No docstring provided.*

### `def _configure_logging()`
Configure root logger to match settings.LOG_LEVEL.

### `def _ok(request_id, result)`
*No docstring provided.*

### `def _err(request_id, code, message, data)`
*No docstring provided.*

### `def _parse_request(raw)`
Parse a raw JSON string into (id, method, params).

Raises ValueError on malformed input.

### `def _normalize_endpoint_url(endpoint)`
Return a base endpoint URL guaranteed to have a /v1 (or equivalent) path.

Many users configure Ollama / LM Studio as ``http://localhost:11434``
without the ``/v1`` path segment.  Appending ``/chat/completions`` to that
bare host produces a 404.  This helper detects the situation and
auto-corrects it.

Rules:
- If the endpoint already ends with /v1, /v1beta, /v1beta/openai, or
  similar, return it unchanged.
- If the endpoint is a local host (localhost / 127.x) or contains a
  well-known local-LLM port (:11434, :1234, :8080, :8000), append /v1.
- Otherwise return unchanged (the caller may be pointing at a custom
  server that manages its own paths).

### `def clean_roleplay_memory(text)`
Cleans up structural metadata, story blueprint markers, and analytical behavioral
headers from the semantic memory text of Neural Nodes. Returns clean narrative paragraphs
suitable for RAG injection.

### `def _documentation_targets_from_nl(prompt)`
*No docstring provided.*

### `def _format_documentation_result(title, result)`
*No docstring provided.*

### `def _maybe_handle_documentation_nl(prompt)`
*No docstring provided.*

### `async def handle_inference_generate(params, ws, req_id)`
``inference.generate`` — Run vLLM inference (with token gate).

Params
------
prompt       : str   required — the text to complete
max_tokens   : int   optional — default 512
temperature  : float optional — default 0.7
session_id   : str   optional — tracing / ledger key
provider_id  : str   optional — route this request through a configured provider
stream       : bool  optional — if true, push partial tokens as notifications

Returns
-------
{ text: str, tokens_used: int, cost_usd: float, ledger_id: int }

### `async def handle_db_query(params, req_id)`
``db.query`` — Execute a parameterised SELECT and return rows.

Params
------
sql    : str            required — parameterised SQL (SELECT only)
args   : list[Any]      optional — positional parameters

Returns
-------
{ rows: list[dict], count: int }

### `async def handle_db_insert(params, req_id)`
``db.insert`` — Execute a parameterised INSERT (or UPDATE/DELETE).

Params
------
sql    : str       required — parameterised DML
args   : list[Any] optional — positional parameters

Returns
-------
{ status: str }  — PostgreSQL command tag, e.g. 'INSERT 0 1'

### `async def handle_token_count(params, req_id)`
``token.count`` — Count tokens in a prompt WITHOUT deducting balance.

Params
------
prompt : str   required

Returns
-------
{ tokens: int, cost_usd: float, balance: float, encoding: str }

### `async def handle_graph_activate(params, req_id)`
``graph.activate`` — Activate (upsert) a node in the cognitive graph.

Params
------
node_type           : str    required
content             : dict   required  — arbitrary JSON payload
execution_authority : str    optional  — 'user', 'agent', or 'system'

Returns
-------
{ node_id: str, node_type: str, created: bool }

### `async def handle_vault_status(params, req_id)`
``vault.status`` — Return filesystem status of the vault directory.

Returns
-------
{ path: str, exists: bool, file_count: int, size_bytes: int }

### `async def handle_harness_health(params, req_id)`
``harness.health`` — Return server health and status info.

### `async def handle_astro_transits(params, req_id)`
``astro.transits`` — Compute planetary transits for user, agent, Tala, or custom birth data.

### `async def handle_astro_synastry(params, req_id)`
``astro.synastry`` — Compare user-profile astro against active-agent astro.

### `async def handle_identity_get_user_profile(params, req_id)`
``identity.get_user_profile`` — Load user profile Neural Node details.

### `async def handle_identity_save_user_profile(params, req_id)`
``identity.save_user_profile`` — Compile user details into concept-user-profile.html
and anchor on the Proof-of-Authority blockchain node.

### `async def handle_graph_query_activations(params, req_id)`
``graph.query_activations`` — Compute activation scores and propagation for a given query.

### `async def handle_inference_get_providers(params, req_id)`
``inference.get_providers`` — Retrieve all configured inference providers sorted by priority.

### `async def handle_inference_save_providers(params, req_id)`
``inference.save_providers`` — Persist the list of providers and their ordering/priorities.

### `async def handle_inference_get_embedded_status(params, req_id)`
``inference.get_embedded_status`` — Return progress and status of the default embedded model download.

This handler is deliberately non-blocking.  The module may not be loaded yet
(the pre-warm runs in a background task); in that case we immediately return
``not_downloaded`` rather than blocking the event loop for 5-15 s on a cold
huggingface_hub import — which would exceed the 15 s RPC timeout.

### `async def handle_inference_trigger_download(params, req_id)`
``inference.trigger_download`` — Manually start downloading the default embedded model.

### `async def handle_inference_get_engines(params, req_id)`
``inference.get_engines`` — Returns all supported embedded engines and their install status.

### `async def handle_inference_install_engine(params, req_id)`
``inference.install_engine`` — Triggers the background pip installation of an engine.

### `async def handle_inference_get_engine_install_progress(params, req_id)`
``inference.get_engine_install_progress`` — Returns the status and logs of an engine installation.

### `async def handle_inference_detect_local_providers(params, req_id)`
``inference.detect_local_providers`` — On-demand re-scan of local inference engines.

Probes the standard local-engine endpoints (Ollama :11434, LM Studio :1234,
vLLM :8000, generic OpenAI-compat :8080) and updates the inference_providers
table, then returns the full updated provider list so the renderer can
refresh immediately without a separate get_providers call.

### `async def handle_agent_get_agents(params, req_id)`
``agent.get_agents`` — Retrieve all configured agents sorted by created_at.

### `async def handle_agent_save_agent(params, req_id)`
``agent.save_agent`` — Create or update an agent persona.

### `async def handle_agent_delete_agent(params, req_id)`
``agent.delete_agent`` — Delete a custom agent.

### `async def handle_agent_set_active(params, req_id)`
``agent.set_active`` — Toggle a specific agent as active and deactivate all others.

### `async def handle_agent_get_chat_mode(params, req_id)`
``agent.get_chat_mode`` — Get current chat mode ('rp', 'hybrid', 'assistant').

### `async def handle_agent_set_chat_mode(params, req_id)`
``agent.set_chat_mode`` — Set current chat mode ('rp', 'hybrid', 'assistant') and update DB active agent.

### `async def handle_contacts_list(params, req_id)`
``contacts.list`` — Retrieve all contacts sorted by username.

### `async def handle_contacts_save(params, req_id)`
``contacts.save`` — Create or update a contact.

### `async def handle_contacts_delete(params, req_id)`
``contacts.delete`` — Delete a contact by username.

### `def _normalize_peer_endpoint(endpoint)`
*No docstring provided.*

### `def _normalize_chain_endpoint(endpoint)`
*No docstring provided.*

### `def _derive_chain_endpoint(endpoint_url, chain_endpoint_url)`
*No docstring provided.*

### `async def _register_peer_on_chain_directory(chain_endpoint_url, peer_payload)`
*No docstring provided.*

### `async def _heartbeat_peer_on_chain_directory(chain_endpoint_url, peer_payload)`
*No docstring provided.*

### `async def _call_peer_rpc(endpoint, method, params)`
*No docstring provided.*

### `def _normalize_peer_node_key(value)`
*No docstring provided.*

### `def _node_reference_candidates(node)`
*No docstring provided.*

### `def _get_local_nodes_directory()`
*No docstring provided.*

### `def _parse_peer_node_reference(reference)`
*No docstring provided.*

### `def _extract_peer_node_references(text)`
*No docstring provided.*

### `async def _get_firewall_contact_access_rights(peer_id)`
*No docstring provided.*

### `def _firewall_allows_node_name(access_rights, node_names)`
*No docstring provided.*

### `async def _peer_can_resolve_node_reference(contact, node_names)`
*No docstring provided.*

### `def _serialize_peer_node(node, matched_reference)`
*No docstring provided.*

### `async def _resolve_local_html_nn_reference(reference, nodes_dir)`
*No docstring provided.*

### `async def _call_peer_node_reference(endpoint, contact, node_reference)`
*No docstring provided.*

### `def _format_peer_node_context(peer_username, reference, node_payload)`
*No docstring provided.*

### `async def _get_contact(username, wallet_address)`
*No docstring provided.*

### `async def _get_master_profile_config()`
*No docstring provided.*

### `def _profile_for_permissions(profile_config, permissions)`
*No docstring provided.*

### `def _packet_signature(packet, secret)`
*No docstring provided.*

### `def _sign_packet(packet, secret)`
*No docstring provided.*

### `def _verify_packet(packet, secret)`
*No docstring provided.*

### `def _accept_any_tala_client_enabled()`
*No docstring provided.*

### `def _is_tala_handshake_packet(packet)`
*No docstring provided.*

### `async def _enroll_unknown_tala_contact(packet)`
*No docstring provided.*

### `def _contact_secret(contact)`
*No docstring provided.*

### `async def _record_p2p_message(peer_username, direction, message)`
*No docstring provided.*

### `async def handle_p2p_handshake(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_receive_handshake(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_send_message(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_receive_message(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_resolve_node_reference(params, req_id)`
*No docstring provided.*

### `async def _get_client_setting(key)`
*No docstring provided.*

### `async def _save_client_setting(key, value)`
*No docstring provided.*

### `async def handle_chain_connection_get(params, req_id)`
*No docstring provided.*

### `async def handle_chain_stay_local(params, req_id)`
*No docstring provided.*

### `async def handle_chain_disconnect(params, req_id)`
*No docstring provided.*

### `async def handle_chain_connect_vps(params, req_id)`
*No docstring provided.*

### `async def handle_chain_auto_connect(params, req_id)`
*No docstring provided.*

### `async def handle_profiles_list(params, req_id)`
``profiles.list`` — Retrieve all profiles, optionally filtered by type.

### `async def handle_profiles_get(params, req_id)`
``profiles.get`` — Retrieve a single profile by id.

### `async def handle_profiles_save(params, req_id)`
``profiles.save`` — Create or update a profile.

### `async def handle_profiles_delete(params, req_id)`
``profiles.delete`` — Delete a profile by id.

### `async def handle_nodes_import_markdown(params, req_id)`
``nodes.import_markdown`` — Import and convert Markdown files to HTML Neural Nodes.

### `async def handle_docs_create_documentation_node(params, req_id)`
``docs.create_documentation_node`` — Create one validated HTML-NN documentation node.

### `async def handle_docs_create_learning_node(params, req_id)`
``docs.create_learning_node`` — Create a fresh HTML-NN learning event node.

### `async def handle_docs_generate_program_nodes(params, req_id)`
``docs.generate_program_nodes`` — Generate validated program documentation HTML-NNs.

### `async def handle_docs_verify_documentation_nodes(params, req_id)`
``docs.verify_documentation_nodes`` — Verify documentation HTML-NNs from disk.

### `async def handle_docs_generate_and_verify_program_nodes(params, req_id)`
``docs.generate_and_verify_program_nodes`` — Generate docs, then verify from disk.

### `async def handle_docs_register_program_target(params, req_id)`
``docs.register_program_target`` — Register a new documentation target.

### `async def handle_docs_list_program_targets(params, req_id)`
``docs.list_program_targets`` — List built-in and registered documentation targets.

### `async def handle_docs_refresh_node_grid(params, req_id)`
``docs.refresh_node_grid`` — Renderer hint to reload visible HTML-NN cards.

### `async def handle_workflow_list(params, req_id)`
``workflow.list`` — Retrieve all configured workflows.

### `async def handle_workflow_save(params, req_id)`
``workflow.save`` — Create or update a workflow configuration.

### `async def handle_workflow_delete(params, req_id)`
``workflow.delete`` — Delete a workflow configuration.

### `async def handle_workflow_execute(params, ws, req_id)`
``workflow.execute`` — Execute a workflow node-by-node.

### `async def handle_mcp_connect_server(params, req_id)`
``mcp.connect_server`` — Register an MCP server configuration.

### `async def handle_mcp_list_servers(params, req_id)`
``mcp.list_servers`` — List persisted/registered MCP server configs.

### `async def handle_mcp_remove_server(params, req_id)`
``mcp.remove_server`` — Remove an MCP server from memory and persistence.

### `async def handle_mcp_list_tools(params, req_id)`
``mcp.list_tools`` — List tools from one MCP server, or all registered servers.

### `async def handle_mcp_call_tool(params, req_id)`
``mcp.call_tool`` — Call a tool on a registered MCP server.

### `async def handle_guardrails_get(params, req_id)`
``guardrails.get`` — Retrieve active guardrails configuration.

### `async def handle_guardrails_save(params, req_id)`
``guardrails.save`` — Save guardrails configuration.

### `async def handle_system_info(params, req_id)`
``system.info`` — Collect host hardware, OS, and installed software versions.
Returns a structured dict consumable directly by the frontend System panel.

### `async def handle_git_get_providers(params, req_id)`
``git.get_providers`` — Retrieve all configured Git hosting providers sorted by priority.

### `async def handle_git_save_providers(params, req_id)`
``git.save_providers`` — Persist Git provider list (replaces entire table).

### `async def handle_search_get_providers(params, req_id)`
``search.get_providers`` — Retrieve all configured search providers sorted by priority.

### `async def handle_search_save_providers(params, req_id)`
``search.save_providers`` — Save / persist search providers list and order.

### `async def handle_search_query(params, req_id)`
``search.query`` — Run search query through SearchManager.

### `async def handle_storage_get_providers(params, req_id)`
``storage.get_providers`` — Retrieve all configured storage providers sorted by priority.
Credentials are stored as a JSONB blob to support heterogeneous provider types.

### `async def handle_storage_save_providers(params, req_id)`
``storage.save_providers`` — Persist storage providers list (replaces entire table).

### `async def handle_database_get_providers(params, req_id)`
``database.get_providers`` — Retrieve all configured Database providers sorted by priority.

### `async def handle_database_save_providers(params, req_id)`
``database.save_providers`` — Persist Database provider list (replaces entire table).

### `def _is_redacted_secret(value)`
*No docstring provided.*

### `def _auth_config_has_secret(config)`
*No docstring provided.*

### `def _redact_auth_config(config)`
*No docstring provided.*

### `def _merge_redacted_auth_config(incoming, existing)`
*No docstring provided.*

### `async def handle_runtime_get_instances(params, req_id)`
``runtime.get_instances`` — Retrieve all configured remote HTML-NN instances sorted by priority.

### `async def handle_runtime_save_instances(params, req_id)`
``runtime.save_instances`` — Persist remote HTML-NN instance list (replaces entire table).

### `async def handle_auth_get_credentials(params, req_id)`
``auth.get_credentials`` — Retrieve all configured credentials sorted by priority.

### `async def handle_auth_save_credentials(params, req_id)`
``auth.save_credentials`` — Persist credentials list (replaces entire table).

### `async def handle_firewall_get_contacts(params, req_id)`
``firewall.get_contacts`` — Retrieve all trusted contacts sorted by priority.

### `async def handle_firewall_save_contacts(params, req_id)`
``firewall.save_contacts`` — Persist trusted contacts list (replaces entire table).

### `def infer_subsystem(message, trace_dict)`
Helper to dynamically infer subsystem classification from message or trace.

### `def infer_event_type(message, trace_dict)`
Helper to dynamically infer event_type classification from message or trace.

### `async def handle_logging_get_events(params, req_id)`
``logging.get_events`` — Query and filter log events from agent_log.
Also returns real-time metrics for dashboard cards.

### `async def handle_logging_get_health(params, req_id)`
``logging.get_system_health`` — Return dynamic health indicators for the 12 subservices.

### `async def handle_logging_clear_events(params, req_id)`
``logging.clear_events`` — Delete log records from the agent_log table.

### `async def handle_logging_archive_events(params, req_id)`
``logging.archive_events`` — Export matching logs to data/archives/ folder and delete them.

### `async def fetch_webpage_text(url)`
Fetch clean text content from a URL.

Extracts headings/paragraphs first; falls back to <meta> description / OG tags
for pages that block scraping (YouTube, Reddit, etc.).

### `async def execute_llm_generation(prompt, system_prompt)`
Non-streaming LLM generation traversing configuration priority list with fallback routing.

### `async def handle_research_search(params, req_id)`
``research.search`` — Dispatches search queries to discover relevant web sources.

### `async def handle_research_crawl_and_summarize(params, req_id)`
``research.crawl_and_summarize`` — Scrapes selected URLs, aggregates clean text, and
prompts the active LLM to output a compiled HTML Neural Node summarizing the findings.

### `async def handle_task_submit(params, req_id)`
``task.submit`` — Accept a goal, decompose it, and queue/execute it using GoalEngine.

### `async def handle_task_list(params, req_id)`
``task.list`` — Fetch all queued/executing tasks.

### `async def handle_task_assign_worker(params, req_id)`
*No docstring provided.*

### `async def handle_task_set_model_override(params, req_id)`
*No docstring provided.*

### `async def handle_task_create_worktree(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_workers_active(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_topology_list(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_start(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_pause(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_resume(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_cancel(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_status(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_runs_list(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_runs_get(params, req_id)`
*No docstring provided.*

### `async def handle_swarm_artifact_inspect(params, req_id)`
*No docstring provided.*

### `async def handle_gateway_status(params, req_id)`
*No docstring provided.*

### `async def handle_plugins_list(params, req_id)`
*No docstring provided.*

### `async def handle_plugins_validate(params, req_id)`
*No docstring provided.*

### `async def handle_plugins_reload(params, req_id)`
*No docstring provided.*

### `async def handle_task_control(params, req_id)`
``task.control`` — Pause, resume, or abort task loops.

### `async def handle_task_clear(params, req_id)`
``task.clear`` — Wipe all tasks and their associated telemetry.

### `async def handle_task_get_telemetry(params, req_id)`
``task.get_telemetry`` — Fetch event telemetry streams.

### `async def handle_repo_scan(params, req_id)`
``repo.scan`` — Scan repository code structure.

### `async def handle_repo_lint(params, req_id)`
``repo.lint`` — Lint repository for architectural violations.

### `async def handle_repo_checkpoint(params, req_id)`
``repo.checkpoint`` — Create repository state checkpoint.

### `async def handle_repo_restore(params, req_id)`
``repo.restore`` — Revert to the last checkpoint.

### `async def handle_task_report(params, req_id)`
``task.report`` — Replay event logs and generate progress report.

### `async def handle_build_diagnose(params, req_id)`
``build.diagnose`` — Diagnose compiler or terminal failure logs.

### `async def handle_build_repair(params, req_id)`
``build.repair`` — Triggers IterativeDebugger loop to fix a target file.

### `async def handle_shell_run(params, req_id)`
``shell.run`` — Governed async shell execution.

### `async def handle_code_generate(params, req_id)`
``code.generate`` — Generate file from a template.

### `async def handle_code_repair(params, req_id)`
``code.repair`` — Apply a surgical line patch to file content.

### `async def handle_test_generate(params, req_id)`
``test.generate`` — Generate a test suite.

### `async def handle_code_propose_patch(params, req_id)`
``code.propose_patch`` — Propose bounded exact-replacement file edits.

### `async def handle_code_apply_patch(params, req_id)`
``code.apply_patch`` — Apply bounded exact-replacement file edits.

### `async def handle_code_propose_file_write(params, req_id)`
``code.propose_file_write`` — Propose bounded workspace file creation.

### `async def handle_code_write_files(params, req_id)`
``code.write_files`` — Create bounded text files inside the workspace.

### `async def handle_test_run_validation(params, req_id)`
``test.run_validation`` — Run allowlisted validation commands.

### `async def handle_system_self_test(params, req_id)`
``system.self_test`` — Run Tala's repeatable local health and validation suite.

### `async def handle_system_trigger_update(params, req_id)`
``system.trigger_update`` — Pull latest code from the repository and notify clients.

### `async def handle_system_download_update(params, req_id)`
``system.download_update`` — Package the current repository into a ZIP file and return as Base64.

### `async def handle_self_improvement_create_plan(params, req_id)`
``self_improvement.create_plan`` — Create a bounded self-improvement plan.

### `async def handle_self_improvement_execute_plan(params, req_id)`
``self_improvement.execute_plan`` — Execute a bounded audited improvement plan.

### `async def handle_self_improvement_dry_run_goal(params, req_id)`
``self_improvement.dry_run_goal`` — Create and execute a bounded dry-run plan.

### `async def handle_self_improvement_execute_goal(params, req_id)`
``self_improvement.execute_goal`` — Execute a goal through the selected approved bridge.

### `async def handle_wiki_query(params, req_id)`
``wiki.query`` — Query Neural Wiki nodes.

### `async def handle_wiki_update(params, req_id)`
``wiki.update`` — Update an HTML Neural Node.

### `async def handle_command_parse(params, req_id)`
``command.parse`` — Parses slash commands using CommandParser.

### `async def handle_repo_broken_imports(params, req_id)`
``repo.broken_imports`` — Detect broken imports across Python modules.

### `async def handle_repo_ipc_audit(params, req_id)`
``repo.ipc_audit`` — Audit IPC registry to cross-reference RPC and slash commands.

### `async def handle_wiki_check_exists(params, req_id)`
``wiki.check_exists`` — Check if a wiki node exists for a given feature/node_id.

### `async def handle_wiki_self_document_phase(params, req_id)`
``wiki.self_document_phase`` — Generate or update self-documentation for a phase.

### `async def handle_telemetry_stream_subscribe(params, ws, req_id)`
``telemetry.stream_subscribe`` — Register client websocket for live status updates.

### `async def handle_heartbeat_list(params, req_id)`
``heartbeat.list`` — Return all configured heartbeat jobs.

Returns
-------
{ jobs: list[HeartbeatJob] }

### `async def handle_heartbeat_create(params, req_id)`
``heartbeat.create`` — Create a new heartbeat job.

Params
------
name      : str  required
cron_expr : str  required  e.g. '0 9 * * 1' or '@startup'
goal_text : str  required  natural-language goal for GoalEngine
enabled   : bool optional  default true

### `async def handle_heartbeat_update(params, req_id)`
``heartbeat.update`` — Update an existing heartbeat job.

Params
------
id        : str  required
name      : str  optional
cron_expr : str  optional
goal_text : str  optional
enabled   : bool optional

### `async def handle_heartbeat_delete(params, req_id)`
``heartbeat.delete`` — Delete a heartbeat job by id.

Params
------
id : str  required

### `async def handle_heartbeat_fire_now(params, req_id)`
``heartbeat.fire_now`` — Manually trigger a heartbeat job immediately.

Params
------
id : str  required

### `async def handle_heartbeat_reload(params, req_id)`
``heartbeat.reload`` — Hot-reload all heartbeat jobs from the database
without restarting the harness.

### `async def handle_task_status(params, ws, req_id)`
*No docstring provided.*

### `async def handle_task_phase_events(params, ws, req_id)`
*No docstring provided.*

### `async def handle_task_artifacts(params, ws, req_id)`
*No docstring provided.*

### `async def handle_memory_search(params, req_id)`
*No docstring provided.*

### `async def handle_memory_session_search(params, req_id)`
*No docstring provided.*

### `async def handle_memory_browse(params, req_id)`
*No docstring provided.*

### `async def handle_memory_record_completed_turn(params, req_id)`
*No docstring provided.*

### `async def handle_memory_get_user_model(params, req_id)`
*No docstring provided.*

### `async def handle_memory_scan_context(params, req_id)`
*No docstring provided.*

### `async def handle_memory_reindex_node(params, req_id)`
*No docstring provided.*

### `async def handle_memory_save_node(params, req_id)`
*No docstring provided.*

### `async def handle_memory_delete_node(params, req_id)`
*No docstring provided.*

### `async def handle_research_unified_search(params, req_id)`
*No docstring provided.*

### `async def handle_research_distill_to_node(params, req_id)`
*No docstring provided.*

### `async def handle_memory_reindex_profiles(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_semantic_search(params, req_id)`
*No docstring provided.*

### `async def handle_multimodal_capture_screen(params, req_id)`
*No docstring provided.*

### `async def handle_multimodal_transcribe_voice(params, req_id)`
*No docstring provided.*

### `async def handle_browser_get_dom(params, req_id)`
*No docstring provided.*

### `async def handle_browser_execute_js(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_webrtc_offer(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_webrtc_answer(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_encrypted_payload(params, req_id)`
*No docstring provided.*

### `async def handle_marketplace_publish(params, req_id)`
*No docstring provided.*

### `async def handle_marketplace_search(params, req_id)`
*No docstring provided.*

### `async def handle_p2p_marketplace_search(params, req_id)`
*No docstring provided.*

### `async def handle_marketplace_download(params, req_id)`
*No docstring provided.*

### `async def _handle_webrtc_message(peer_username, raw_message)`
*No docstring provided.*

### `async def _handle_connection(ws, path)`
Handle the lifetime of a single WebSocket client connection.

Each inbound message is parsed as JSON-RPC 2.0, dispatched to the
appropriate handler concurrently, and the response is sent back on the same socket.

### `async def broadcast_notification(method, params)`
Send a JSON-RPC 2.0 notification to all connected WebSocket clients.

### `async def _auto_detect_and_verify_providers()`
Detect local providers (Ollama) and test all registered providers to update their status.

### `async def _startup()`
Initialise DB pool and kick off background tasks.

This runs as an asyncio task AFTER the WebSocket server is already bound,
so slow DB / model-cache operations never delay the initial WS handshake.

### `async def _startup_background()`
Perform slower non-critical hardware discovery, model caching verification, and provider auto-detection in background.

### `async def _shutdown()`
Drain and close all subsystems on shutdown.

### `def _install_signal_handlers(loop)`
Register SIGTERM / SIGINT handlers for graceful shutdown.

### `async def _monitor_stdin()`
Monitor stdin for EOF. If stdin is closed, trigger shutdown.

### `async def _monitor_parent_process()`
Monitor if the parent process is still alive. If it dies, trigger shutdown.

### `async def _run_server()`
Main coroutine: bind the WebSocket server FIRST, then initialise subsystems.

Binding the WS server before any slow I/O (DB pool, model cache checks)
means Electron can connect within its 2-second window.  All slow work is
moved into background tasks that run after the port is open.

### `async def _prewarm_embedded_engine()`
Import python.inference.embedded_engine in a thread executor so the
heavy huggingface_hub import happens before the renderer polls for status.
This task is started immediately after the WS server is bound and runs
independently of DB initialisation.

### `def main()`
CLI entry point.  Run with::

    python -m harness.server
    # or
    python python/harness/server.py

## Source Code

```python
"""\nserver.py — Tala Cognitive Engine Room: JSON-RPC 2.0 WebSocket Harness Server\n==============================================================================\nEntry point for the Python backend.  Runs a WebSocket server on\n``ws://127.0.0.1:8765`` (configurable via Config) that speaks JSON-RPC 2.0.\n\nRegistered method handlers\n---------------------------\n  inference.generate  — stream a vLLM completion for a given prompt\n  db.query            — raw parameterised SELECT against PostgreSQL\n  db.insert           — parameterised INSERT, returns inserted row(s)\n  token.count         — count tokens without deducting balance\n  graph.activate      — activate a node in the cognitive graph\n  vault.status        — return vault filesystem status\n\nArchitecture notes\n------------------\n- ALL durable state flows through db.py (PostgreSQL).\n- mem0 is NEVER used for durable writes — only wrapped read-only via\n  mem0_adapter.py.\n- Token gate (token_counter.py) is called before every inference.generate.\n- Graceful shutdown on SIGTERM / SIGINT: pool is drained, connections closed.\n\nMilestone 1.2 — Tasks 1.2.1–1.2.4 (server orchestration layer)\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport hashlib\nimport hmac\nimport json\nimport logging\nimport os\nimport re\nimport signal\nimport sys\nimport traceback\nimport uuid\nfrom collections.abc import AsyncIterator\nfrom datetime import datetime, timezone\nfrom typing import Any\n\nimport websockets\nfrom websockets.server import WebSocketServerProtocol\n\n# ---------------------------------------------------------------------------\n# Path setup & package imports (Task 1.2.2)\n# ---------------------------------------------------------------------------\nimport sys\nfrom pathlib import Path\n\n# Add project root (three levels up from this file) to Python path\n_ROOT_DIR = str(Path(__file__).resolve().parent.parent.parent)\nif _ROOT_DIR not in sys.path:\n    sys.path.insert(0, _ROOT_DIR)\n\nfrom python.harness.config import settings\nfrom python.harness import db\nfrom sdk.agent.exceptions import (\n    SafetyGuardrailViolation,\n    PiiAccessBlockedError,\n)\nfrom python.harness.token_counter import (\n    InsufficientTokenBalanceError,\n    count_and_check,\n    count_tokens,\n    get_wallet_balance,\n)\nfrom python.harness.settings_nn import (\n    sync_settings_to_nodes,\n    update_settings_inference_node,\n    update_settings_agent_node,\n    update_settings_workflows_node,\n    update_settings_guardrails_node,\n    update_settings_search_node,\n    update_settings_git_node,\n    update_settings_storage_node,\n    update_settings_database_node,\n    update_settings_runtime_node,\n    update_settings_auth_node,\n    update_settings_firewall_node,\n)\nfrom python.harness.heartbeat_scheduler import HeartbeatScheduler, register_broadcast\n\nlogger = logging.getLogger(__name__)\n\n# Helper functions for persisting and loading the active chat mode (rp, hybrid, assistant)\ndef get_chat_mode_file_path() -> Path:\n    user_data = os.environ.get("TALA_USER_DATA")\n    if user_data:\n        base_dir = Path(user_data)\n    else:\n        appdata = os.environ.get("APPDATA")\n        if appdata:\n            base_dir = Path(appdata) / "tala-engine"\n        else:\n            base_dir = Path.home() / "AppData" / "Roaming" / "tala-engine"\n    return base_dir / "chat_mode.json"\n\ndef get_current_chat_mode() -> str:\n    try:\n        path = get_chat_mode_file_path()\n        if path.exists():\n            data = json.loads(path.read_text(encoding="utf-8"))\n            mode = data.get("mode", "assistant")\n            if mode in ("rp", "hybrid", "assistant"):\n                return mode\n    except Exception:\n        pass\n    return "assistant"\n\ndef save_current_chat_mode(mode: str):\n    try:\n        path = get_chat_mode_file_path()\n        path.parent.mkdir(parents=True, exist_ok=True)\n        path.write_text(json.dumps({"mode": mode}), encoding="utf-8")\n    except Exception as exc:\n        logger.warning("Failed to save chat mode to %s: %s", path, exc)\n\n\n# ---------------------------------------------------------------------------\n# Logging setup\n# ---------------------------------------------------------------------------\n\ndef _configure_logging() -> None:\n    """Configure root logger to match settings.LOG_LEVEL."""\n    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)\n    logging.basicConfig(\n        level=level,\n        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",\n        datefmt="%Y-%m-%dT%H:%M:%S",\n        stream=sys.stdout,\n    )\n    # Quieten noisy libraries\n    logging.getLogger("websockets").setLevel(logging.WARNING)\n    logging.getLogger("asyncio").setLevel(logging.WARNING)\n    logging.getLogger("asyncpg").setLevel(logging.WARNING)\n\n\n# ---------------------------------------------------------------------------\n# JSON-RPC 2.0 helpers\n# ---------------------------------------------------------------------------\n\nJSONRPC_VERSION = "2.0"\n\nclass JsonRpcError:\n    """Standard JSON-RPC 2.0 error codes."""\n    PARSE_ERROR      = -32700\n    INVALID_REQUEST  = -32600\n    METHOD_NOT_FOUND = -32601\n    INVALID_PARAMS   = -32602\n    INTERNAL_ERROR   = -32603\n\n\ndef _ok(request_id: Any, result: Any) -> str:\n    return json.dumps({\n        "jsonrpc": JSONRPC_VERSION,\n        "id": request_id,\n        "result": result,\n    })\n\n\ndef _err(request_id: Any, code: int, message: str, data: Any = None) -> str:\n    error: dict[str, Any] = {"code": code, "message": message}\n    if data is not None:\n        error["data"] = data\n    return json.dumps({\n        "jsonrpc": JSONRPC_VERSION,\n        "id": request_id,\n        "error": error,\n    })\n\n\ndef _parse_request(raw: str) -> tuple[Any, str, dict[str, Any]]:\n    """\n    Parse a raw JSON string into (id, method, params).\n\n    Raises ValueError on malformed input.\n    """\n    try:\n        msg = json.loads(raw)\n    except json.JSONDecodeError as exc:\n        raise ValueError(f"Parse error: {exc}") from exc\n\n    if not isinstance(msg, dict):\n        raise ValueError("Request must be a JSON object")\n    if msg.get("jsonrpc") != JSONRPC_VERSION:\n        raise ValueError("jsonrpc field must be '2.0'")\n    if "method" not in msg:\n        raise ValueError("Missing 'method' field")\n\n    req_id = msg.get("id")  # may be None for notifications\n    method = str(msg["method"])\n    params = msg.get("params", {})\n    if not isinstance(params, dict):\n        raise ValueError("'params' must be a JSON object (dict)")\n    return req_id, method, params\n\n\n# ---------------------------------------------------------------------------\n# Method handlers\n# ---------------------------------------------------------------------------\n\n\ndef _normalize_endpoint_url(endpoint: str) -> str:\n    """Return a base endpoint URL guaranteed to have a /v1 (or equivalent) path.\n\n    Many users configure Ollama / LM Studio as ``http://localhost:11434``\n    without the ``/v1`` path segment.  Appending ``/chat/completions`` to that\n    bare host produces a 404.  This helper detects the situation and\n    auto-corrects it.\n\n    Rules:\n    - If the endpoint already ends with /v1, /v1beta, /v1beta/openai, or\n      similar, return it unchanged.\n    - If the endpoint is a local host (localhost / 127.x) or contains a\n      well-known local-LLM port (:11434, :1234, :8080, :8000), append /v1.\n    - Otherwise return unchanged (the caller may be pointing at a custom\n      server that manages its own paths).\n    """\n    ep = endpoint.rstrip("/")\n    # Already has an OpenAI-compatible versioned sub-path?\n    versioned_suffixes = ("/v1", "/v1beta", "/v1beta/openai", "/openai/v1", "/api/v1")\n    if any(ep.endswith(s) for s in versioned_suffixes):\n        return ep\n\n    # Local engine without explicit path -> append /v1\n    local_indicators = ("localhost", "127.0.0.1", "0.0.0.0", ":11434", ":1234", ":8080", ":8000", ":8888")\n    if any(ind in ep for ind in local_indicators):\n        corrected = ep + "/v1"\n        logger.info("Auto-appended /v1 to local LLM endpoint: %s -> %s", ep, corrected)\n        return corrected\n\n    return ep\n\n\ndef clean_roleplay_memory(text: str) -> str:\n    """\n    Cleans up structural metadata, story blueprint markers, and analytical behavioral\n    headers from the semantic memory text of Neural Nodes. Returns clean narrative paragraphs\n    suitable for RAG injection.\n    """\n    if not text:\n        return ""\n    \n    # Split lines\n    lines = [line.strip() for line in text.split("\n")]\n    cleaned = []\n    \n    # Headers to stop processing completely (drop all lines after these)\n    stop_headers = (\n        "long-term impact hooks",\n        "behavioral patterns",\n        "behavioral patterns observed",\n        "emotional tendencies",\n        "environmental / situational triggers",\n        "environmental triggers",\n        "style guarantees",\n        "long-term imprint"\n    )\n    \n    # Metadata prefixes to skip\n    skip_prefixes = (\n        "type:", "persistence:", "age / life stage:", "emotional weight:", "theme anchor:",\n        "purpose:", "tala — long-term memory file", "memory classification", "memory title",\n        "story outline"\n    )\n    \n    # Section headers to skip\n    skip_headers = (\n        "exposition (setup)", "inciting incident", "rising action", "climax", "resolution (denouement)",\n        "exposition", "resolution"\n    )\n    \n    for line in lines:\n        if not line:\n            continue\n        line_lower = line.lower()\n        \n        # Check if we need to stop\n        if any(h in line_lower for h in stop_headers):\n            break\n            \n        # Check if we should skip metadata\n        if any(line_lower.startswith(p) for p in skip_prefixes):\n            continue\n            \n        # Check if we should skip section headers\n        if line_lower in skip_headers:\n            continue\n            \n        cleaned.append(line)\n        \n    return "\n".join(cleaned).strip()\n\n\n_DOC_TARGET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (\n    ("codebase", ("entire codebase", "whole codebase", "full codebase", "codebase", "project documentation", "code documentation")),\n    ("bridges", ("bridges", "bridge docs", "tool contracts", "tool contract")),\n    ("self-improvement.system", ("self-improvement system", "self improvement system", "self-improvement capability", "self improvement capability")),\n    ("tool.registry", ("tool registry", "tools registry", "registered tools")),\n    ("documentation.generator", ("documentation generator", "doc generator", "generate documentation")),\n    ("self_improvement.execute_plan", ("self_improvement.execute_plan", "execute_plan", "file_writes")),\n    ("code.patch.bridge", ("code.propose_file_write", "code.write_files", "code patch bridge", "code.patch.bridge", "code-writing", "code writing")),\n    ("chat.panel", ("chat panel", "chat.panel")),\n    ("workflow", ("workflow", "workflow builder", "workflow executor")),\n    ("settings.inference", ("inference settings", "settings.inference", "inference providers")),\n    ("renderer.ui", ("renderer ui", "renderer", "ui panels")),\n    ("electron.shell", ("electron shell", "electron")),\n    ("python.harness", ("python harness", "backend harness")),\n    ("html-nn.runtime", ("html-nn runtime", "html neural node runtime", "node runtime")),\n    ("tests.validation", ("tests validation", "test validation", "validation tests")),\n    ("vault.profile", ("vault profile", "vault")),\n    ("blockchain.ledger", ("blockchain ledger", "ledger")),\n)\n\n\ndef _documentation_targets_from_nl(prompt: str, *, for_generation: bool = False) -> list[str]:\n    lowered = prompt.lower()\n    targets: list[str] = []\n    for target, keywords in _DOC_TARGET_KEYWORDS:\n        if target in lowered or any(keyword in lowered for keyword in keywords):\n            targets.append(target)\n    if re.search(r"\b(tala|herself|itself)\b", lowered) and any(\n        term in lowered for term in ("document", "documentation")\n    ) and any(term in lowered for term in ("codebase", "project", "herself", "itself", "entire", "whole", "full")):\n        targets.append("codebase")\n    if for_generation:\n        generation_targets: list[str] = []\n        for target in targets:\n            if target in {"code.patch.bridge", "self_improvement.execute_plan", "test.validation.bridge", "docs.refresh_node_grid"}:\n                generation_targets.append("bridges")\n            else:\n                generation_targets.append(target)\n        targets = generation_targets\n    return list(dict.fromkeys(targets))\n\n\ndef _format_documentation_result(title: str, result: dict[str, Any]) -> str:\n    lines = [\n        title,\n        f"Created: {result.get('created', 0)}",\n        f"Updated: {result.get('updated', 0)}",\n        f"Unchanged: {result.get('unchanged', 0)}",\n        f"Errors: {len(result.get('errors', []))}",\n    ]\n    if "verified" in result:\n        lines.append(f"Verified: {result.get('verified', 0)}")\n    nodes = result.get("nodes", [])\n    if isinstance(nodes, list):\n        for node in nodes:\n            if not isinstance(node, dict):\n                continue\n            lines.append(\n                "- "\n                f"{node.get('node_id')} -> {node.get('file_name')} | "\n                f"target: {node.get('doc_target')} | "\n                f"schema: {node.get('schema_version', 'unknown')} | "\n                f"validation: {'passed' if node.get('validation_passed') else 'failed'}"\n            )\n    errors = result.get("errors", [])\n    if errors:\n        lines.append("Error details:")\n        lines.extend(f"- {error}" for error in errors)\n    return "\n".join(lines)\n\n\ndef _maybe_handle_documentation_nl(prompt: str) -> str | None:\n    lowered = prompt.lower().strip()\n    if lowered.startswith("/"):\n        return None\n\n    wants_docs = any(term in lowered for term in ("generate doc", "create doc", "update doc", "regenerate doc", "html-nn documentation")) or bool(\n        re.search(r"\bdocument\b", lowered)\n    )\n    wants_verify = any(term in lowered for term in ("verify", "validate", "check", "prove"))\n    wants_list = any(term in lowered for term in ("list doc target", "list documentation target", "what doc targets"))\n    negates_generation = any(\n        term in lowered\n        for term in (\n            "do not generate",\n            "don't generate",\n            "do not create",\n            "don't create",\n            "do not update",\n            "don't update",\n        )\n    )\n    wants_generation = not negates_generation and (any(term in lowered for term in ("generate", "create", "update", "regenerate")) or bool(\n        re.search(r"\bdocument\b", lowered)\n    ))\n\n    if not (wants_docs or wants_verify or wants_list):\n        return None\n\n    from sdk.agent.documentation_nodes import (\n        generate_and_verify_program_nodes,\n        list_program_documentation_targets,\n        verify_documentation_nodes,\n    )\n\n    if wants_list:\n        result = list_program_documentation_targets()\n        return json.dumps(result, indent=2)\n\n    verify_targets = _documentation_targets_from_nl(prompt, for_generation=False)\n    if wants_verify and not wants_generation:\n        result = verify_documentation_nodes(targets=verify_targets or None)\n        return _format_documentation_result("Documentation disk verification complete.", result)\n\n    if wants_docs:\n        generation_targets = _documentation_targets_from_nl(prompt, for_generation=True)\n        result = generate_and_verify_program_nodes(generation_targets or ["chat.panel"])\n        return _format_documentation_result("Documentation generation and disk verification complete.", result)\n\n    return None\n\n\nasync def handle_inference_generate(\n    params: dict[str, Any],\n    ws: WebSocketServerProtocol,\n    req_id: Any,\n) -> str:\n    """\n    ``inference.generate`` — Run vLLM inference (with token gate).\n\n    Params\n    ------\n    prompt       : str   required — the text to complete\n    max_tokens   : int   optional — default 512\n    temperature  : float optional — default 0.7\n    session_id   : str   optional — tracing / ledger key\n    provider_id  : str   optional — route this request through a configured provider\n    stream       : bool  optional — if true, push partial tokens as notifications\n\n    Returns\n    -------\n    { text: str, tokens_used: int, cost_usd: float, ledger_id: int }\n    """\n    prompt = params.get("prompt")\n    if not prompt or not isinstance(prompt, str):\n        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'prompt' (str) is required")\n\n    session_id: str = str(params.get("session_id", uuid.uuid4()))\n\n    # Guardrails prompt validation (pre-inference)\n    try:\n        from python.harness.guardrails_manager import GuardrailsManager\n        guardrails_mgr = await GuardrailsManager.load_current()\n        guardrails_mgr.validate_prompt(prompt)\n    except (SafetyGuardrailViolation, PiiAccessBlockedError) as exc:\n        err_msg = f"Guardrail blocked prompt: {exc.message}"\n        logger.warning(err_msg)\n        await db.log_to_db(\n            "WARNING",\n            err_msg,\n            {"session_id": session_id, "error_type": type(exc).__name__, "payload": exc.payload}\n        )\n        return _err(\n            req_id,\n            JsonRpcError.INTERNAL_ERROR,\n            exc.message,\n            {\n                "error_type": type(exc).__name__,\n                "payload": exc.payload,\n            }\n        )\n\n    if not bool(params.get("raw_prompt", False)):\n        documentation_response = _maybe_handle_documentation_nl(prompt)\n        if documentation_response is not None:\n            return _ok(req_id, {\n                "text": documentation_response,\n                "tokens_used": 0,\n                "cost_usd": 0.0,\n                "ledger_id": None,\n                "balance_after": None,\n                "session_id": session_id,\n                "routed_by": "documentation-nl-bridge",\n            })\n\n    max_tokens: int = int(params.get("max_tokens", 512))\n    temperature: float = float(params.get("temperature", 0.7))\n    stream: bool = bool(params.get("stream", False))\n    history: list[dict[str, Any]] = params.get("history", [])\n\n    raw_prompt = bool(params.get("raw_prompt", False))\n    if raw_prompt:\n        active_system_prompt = params.get("system_prompt", "You are a precise, helpful software engineering assistant. Output code blocks or clean answers as requested.")\n        messages = []\n        if active_system_prompt:\n            messages.append({"role": "system", "content": active_system_prompt})\n        if isinstance(history, list):\n            for msg in history:\n                role = "assistant" if msg.get("role") in ("agent", "assistant") else "user"\n                content = msg.get("content", "").strip()\n                if content:\n                    messages.append({"role": role, "content": content})\n        messages.append({"role": "user", "content": prompt})\n... (file truncated, showing first 500 lines) ...
```
