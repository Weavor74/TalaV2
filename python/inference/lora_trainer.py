"""
lora_trainer.py - what the engine panel says about LoRA.

The per-node, per-turn JIT training queue that lived here is gone. It was found low-value (only one adapter can be attached per turn, so
N-1 of N were discarded; most nodes are generated documentation, so a per-node adapter learned the register of API prose) and it never
trained anything: it reported success over a placeholder or a bare ``pass``. It was removed rather than left as a
trap for whoever next flipped a flag.

What replaced it is real, and is two things:

* **training**: sdk/agent/lora_pipeline.py trains one QLoRA adapter on Llama 3.2 3B over the memories and replies Steven approved
  (sdk/agent/lora_approvals.py), weekly at night, and deploys it only if it beats the adapter being served (sdk/agent/lora_eval.py).
* **loading**: python/inference/persona_adapter.py serves the active adapter through a second llama-server with ``--lora`` and
  keeps the last three for rollback.

``IMPLEMENTED`` is True only when both of those genuinely work, verified end to end on Kagura (a real training run, a deployed
adapter, a chat turn served with ``--lora``). It is not a feature switch: whether training runs is ``lora_auto_train`` in learning.json.
"""
from __future__ import annotations

from typing import Any

#: Flip only when training AND loading an adapter both genuinely work.
IMPLEMENTED = True  # verified end to end on Kagura 2026-10-25: a real QLoRA run deployed, and a chat turn served with --lora


def engine_status() -> dict[str, Any]:
    """What the engine panel shows about LoRA: the truth about training, what is served, and whether a run is in progress."""
    try:
        from sdk.agent import lora_pipeline

        state = lora_pipeline.status()
    except Exception as exc:  # the panel must render even if the pipeline cannot be imported
        return {"implemented": False, "note": f"LoRA status unavailable: {exc}"}
    if not IMPLEMENTED:
        return {"implemented": False, "note": "The persona adapter is built but has not yet been verified end to end on this machine."}
    scheduled = "weekly at night" if state["auto_train"] else "scheduled training is off (lora_auto_train)"
    served = f"serving {state['active_adapter']}" if state["active_adapter"] else "no adapter served yet"
    last = f"last run {state['last_status']}" if state["last_status"] else "no run yet"
    return {"implemented": True, "auto_train": state["auto_train"], "running": state["running"], "last_status": state["last_status"],
            "active_adapter": state["active_adapter"], "adapters": state["adapters"], "serving": state["serving"], "routing": state["routing"],
            "note": f"Persona adapter: {scheduled}; {served}; {last}."}
