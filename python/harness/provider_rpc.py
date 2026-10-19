"""
NAME
       python.harness.provider_rpc - inference provider management JSON-RPC handlers

SYNOPSIS
       from python.harness.provider_rpc import (
           handle_inference_get_providers,
           handle_inference_save_providers,
           handle_inference_get_embedded_status,
           handle_inference_trigger_download,
           handle_inference_get_engines,
           handle_inference_install_engine,
           handle_inference_get_engine_install_progress,
           handle_inference_detect_local_providers,
       )

DESCRIPTION
       Inference provider configuration, detection, and lifecycle management
       handlers extracted from server.py under the size ratchet.
       Handles listing, updating, and detecting external and embedded AI engines.
"""

from __future__ import annotations

import asyncio

import logging
from typing import Any

from python.harness import db
from python.harness.jsonrpc import JsonRpcError, _err, _ok

logger = logging.getLogger(__name__)


async def handle_inference_get_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_get_providers - retrieve configured inference providers

    SYNOPSIS
           handle_inference_get_providers(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.get_providers`` - Retrieve all configured inference
           providers sorted by priority. Redacts API secrets to ensure credentials
           are never transmitted over unauthenticated JSON-RPC channels.

           - params: Request parameters (empty dictionary expected).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_save_providers(1)
    """
    try:
        from python.harness import server
        from python.harness.credential_crypto import config_has_secret, redact_config

        rows = await server.load_inference_providers()
        providers = []
        for r in rows:
            import json as json_mod
            cfg_dict = r["config"]
            if isinstance(cfg_dict, str):
                cfg_dict = json_mod.loads(cfg_dict)
            providers.append({
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "priority": r["priority"],
                "status": r["status"],
                "config": redact_config(cfg_dict),
                "hasApiKey": config_has_secret(cfg_dict),
                "locked": r["locked"],
            })
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("inference.get_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_save_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_save_providers - persist inference providers ordering and config

    SYNOPSIS
           handle_inference_save_providers(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.save_providers`` - Persist the list of providers and their
           ordering/priorities. Re-merges existing credentials for redacted records
           so API keys are not destroyed on save. Validates remote providers have
           endpoints when API keys are present.

           - params: Dictionary containing 'providers' (list of provider dicts).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_get_providers(1)
    """
    providers = params.get("providers")
    if providers is None or not isinstance(providers, list):
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "'providers' (list) is required")

    import json as json_mod
    
    for p in providers:
        if p.get("type") == "remote":
            cfg = p.get("config", {})
            api_key = cfg.get("apiKey") or p.get("apiKey") or ""
            endpoint = cfg.get("endpoint") or p.get("endpoint") or ""
            
            if api_key.strip() and not endpoint.strip():
                service = cfg.get("service", "").lower()
                provider_name = p.get("name", "this provider")
                return _err(
                    req_id,
                    JsonRpcError.INVALID_PARAMS,
                    f"Remote provider '{provider_name}' has an API key but no endpoint. "
                    f"The endpoint/address field is required for remote providers. "
                    f"Configure it in Settings → Inference → Edit Provider."
                )
    
    try:
        from python.harness.credential_crypto import (
            decrypt_config,
            merge_preserved_secrets,
        )

        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing_cfgs: dict[str, Any] = {}
                existing_ids: set[str] = set()
                for _row in await conn.fetch("SELECT id, config FROM inference_providers"):
                    existing_cfgs[_row["id"]] = decrypt_config(_row["config"]) or {}
                    existing_ids.add(_row["id"])

                new_ids = {p["id"] for p in providers}

                # A provider that existed and is no longer in the submitted
                # list was removed by the user -- tombstone it so the next
                # startup's auto-detect doesn't silently bring it back just
                # because the underlying Ollama/LM Studio model is still
                # installed. A provider that reappears (re-added by hand)
                # clears its own tombstone.
                removed_ids = existing_ids - new_ids
                if removed_ids:
                    await conn.executemany(
                        "INSERT INTO dismissed_inference_providers (id) VALUES ($1) "
                        "ON CONFLICT (id) DO NOTHING",
                        [(rid,) for rid in removed_ids],
                    )
                if new_ids:
                    await conn.execute(
                        "DELETE FROM dismissed_inference_providers WHERE id = ANY($1::text[])",
                        list(new_ids),
                    )

                # Delete existing providers
                await conn.execute("DELETE FROM inference_providers")
                # Insert the updated list
                for idx, p in enumerate(providers):
                    cfg = p.get("config", {})
                    flat_cfg = {
                        "model": p.get("model"),
                        "engine": p.get("engine"),
                        "endpoint": p.get("endpoint"),
                        "apiKey": p.get("apiKey"),
                        "service": p.get("service"),
                    }
                    for k, v in flat_cfg.items():
                        if v is not None:
                            cfg[k] = v

                    cfg = merge_preserved_secrets(cfg, existing_cfgs.get(p["id"], {}))
                    
                    from python.harness.credential_crypto import encrypt_config
                    stored_cfg = encrypt_config(cfg)

                    await conn.execute(
                        """
                        INSERT INTO inference_providers (id, type, name, priority, status, config, locked)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        p["id"],
                        p["type"],
                        p["name"],
                        idx + 1,
                        p.get("status", "unknown"),
                        json_mod.dumps(stored_cfg),
                        bool(p.get("locked", False)),
                    )
        try:
            from python.harness.settings_nn import update_settings_inference_node
            await update_settings_inference_node()
        except Exception as exc:
            logger.warning("Failed to update inference settings node: %s", exc)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.save_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_embedded_status(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_get_embedded_status - return progress of default embedded model download

    SYNOPSIS
           handle_inference_get_embedded_status(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.get_embedded_status`` - Return progress and status of the default
           embedded model download without blocking the event loop.

           - params: Request parameters (empty dictionary expected).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_trigger_download(1)
    """
    import sys
    try:
        mod = sys.modules.get("python.inference.llama_server_manager")
        if mod is None:
            return _ok(req_id, {
                "status": "not_downloaded",
                "progress": 0.0,
                "error": ""
            })
        status, progress, error = mod.get_status()
        from python.inference import external_engine
        engine = await asyncio.to_thread(
            external_engine.summary, bool(mod.builtin_variants()))
        return _ok(req_id, {
            "status": status,
            "progress": progress,
            "error": error,
            "engine": engine,
        })
    except Exception as exc:
        logger.exception("inference.get_embedded_status error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_trigger_download(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_trigger_download - start downloading default embedded model

    SYNOPSIS
           handle_inference_trigger_download(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.trigger_download`` - Manually initiate asynchronous downloading
           of the default embedded local model.

           - params: Request parameters (empty dictionary expected).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_get_embedded_status(1)
    """
    try:
        from python.inference.llama_server_manager import start_download_async
        start_download_async()
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.trigger_download error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_engines(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_get_engines - get supported embedded engines and installation status

    SYNOPSIS
           handle_inference_get_engines(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.get_engines`` - Returns all supported embedded inference engines
           and their installation status.

           - params: Request parameters (empty dictionary expected).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_install_engine(1)
    """
    try:
        from python.inference.engine_manager import get_all_engines
        return _ok(req_id, {"engines": get_all_engines()})
    except Exception as exc:
        logger.exception("inference.get_engines error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_install_engine(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_install_engine - trigger pip installation of an engine

    SYNOPSIS
           handle_inference_install_engine(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.install_engine`` - Triggers background pip installation of a selected engine.

           - params: Dictionary containing 'engine_id' (string).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_get_engine_install_progress(1)
    """
    engine_id = params.get("engine_id")
    if not engine_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "engine_id required")
        
    try:
        from python.inference.engine_manager import start_install
        start_install(engine_id)
        return _ok(req_id, {"success": True})
    except Exception as exc:
        logger.exception("inference.install_engine error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_get_engine_install_progress(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_get_engine_install_progress - get engine installation progress

    SYNOPSIS
           handle_inference_get_engine_install_progress(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.get_engine_install_progress`` - Returns installation status and logs for an engine.

           - params: Dictionary containing 'engine_id' (string).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_install_engine(1)
    """
    engine_id = params.get("engine_id")
    if not engine_id:
        return _err(req_id, JsonRpcError.INVALID_PARAMS, "engine_id required")
        
    try:
        from python.inference.engine_manager import get_status
        return _ok(req_id, get_status(engine_id))
    except Exception as exc:
        logger.exception("inference.get_engine_install_progress error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))


async def handle_inference_detect_local_providers(params: dict[str, Any], req_id: Any) -> str:
    """
    NAME
           handle_inference_detect_local_providers - on-demand scan of local inference engines

    SYNOPSIS
           handle_inference_detect_local_providers(params: dict[str, Any], req_id: Any) -> str

    DESCRIPTION
           ``inference.detect_local_providers`` - Probes standard local engine endpoints
           (Ollama, LM Studio, vLLM, local OpenAI-compatible) and updates the inference_providers
           table, returning the redacted list of providers.

           - params: Request parameters (empty dictionary expected).
           - req_id: JSON-RPC request identifier.

    SEE ALSO
           handle_inference_get_providers(1)
    """
    try:
        from python.harness import server
        from python.harness.credential_crypto import config_has_secret, redact_config

        await server._auto_detect_and_verify_providers()
        rows = await server.load_inference_providers()
        import json as json_mod
        providers = []
        for r in rows:
            cfg = r["config"]
            if isinstance(cfg, str):
                cfg = json_mod.loads(cfg)
            providers.append({
                "id":       r["id"],
                "type":     r["type"],
                "name":     r["name"],
                "priority": r["priority"],
                "status":   r["status"],
                "config":   redact_config(cfg),
                "hasApiKey": config_has_secret(cfg),
                "locked":   r["locked"],
            })
        return _ok(req_id, {"providers": providers})
    except Exception as exc:
        logger.exception("inference.detect_local_providers error")
        return _err(req_id, JsonRpcError.INTERNAL_ERROR, str(exc))
