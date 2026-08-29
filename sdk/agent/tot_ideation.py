"""
sdk/agent/tot_ideation.py
=========================
Tree-of-Thought (ToT) Ideation Module for TalaV2's planning loop.

Implements architectural prompt interception, cognitive frame libraries,
isolated generation contexts, Critic Agent scoring, and structured JSON
shortlist serialization.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keywords for architectural / task-decomposition prompt interception
# ---------------------------------------------------------------------------
#
# Regression this list structure fixes, caught live: a two-sentence request
# to "Plan a simple CLI calculator ... as a short design note" was hijacked
# into full Tree-of-Thought architectural ideation purely because it said
# "plan" and "design" -- words used constantly for tasks of any size, not a
# reliable signal of genuine architectural scope. The interception then
# produced wildly irrelevant "blueprint process" output (and a spurious
# workspace search) instead of just doing the small task, and separately
# collapsed goal decomposition into a single low-quality phase (see
# GoalTreeDecomposer's ToT-shortlist fallback path in queue.py).
#
# Fix: split into words that are unambiguous on their own (technical,
# specific, rare in everyday small-task phrasing) vs. generic verbs/nouns
# ("design", "plan", "structure", "strategy", "blueprint", "system",
# "architect", "modular", "component") that are used just as often for a
# calculator as for a distributed system and must never trigger the full
# pipeline alone. Every existing example this module is tested against
# ("Design a high-throughput cache *module*", "Plan the *database schema*
# migration strategy", "Let's *decompose* this system refactoring") already
# pairs a generic verb with one of these specific nouns -- so keeping only
# the specific-noun list preserves all of that intended behavior while a
# short "plan/design X as a note" request with no technical noun no longer
# matches anything here.
ARCHITECTURAL_KEYWORDS = [
    "architecture", "microservices", "class diagram", "folder structure",
    "uml", "api design", "database schema", "data pipeline", "module",
    "framework", "decompose", "refactor", "scalable", "high-performance",
    "low-latency", "tot ideation", "ideate",
]

def is_architectural_or_decomposition_prompt(prompt: str) -> bool:
    """
    Check if a prompt is open-ended architectural or task-decomposition.
    """
    if not prompt or not isinstance(prompt, str):
        return False
    p_lower = prompt.lower()
    return any(kw in p_lower for kw in ARCHITECTURAL_KEYWORDS)

# ---------------------------------------------------------------------------
# Cognitive Frame Library
# ---------------------------------------------------------------------------
COGNITIVE_FRAMES = {
    "Extreme Modularity": (
        "Focus on extreme separation of concerns, high decoupling, independent micro-modules, "
        "strict interface contracts, and pluggable architecture. Avoid compile-time dependencies."
    ),
    "High Performance": (
        "Focus on maximum throughput, minimal latency, CPU/memory efficiency, parallel execution, "
        "caching, non-blocking I/O, and optimal data structures."
    ),
    "Fault Tolerance": (
        "Focus on resilience, graceful degradation, circuit breakers, auto-recovery, redundancy, "
        "state validation, database authority, and boundary protection."
    ),
    "Minimalism": (
        "Focus on the simplest possible design, zero external dependencies, minimum line count, "
        "ease of readability, and elimination of speculative generality."
    )
}

# ---------------------------------------------------------------------------
# Resilient LLM Client (calls Tala server's inference endpoint or falls back)
# ---------------------------------------------------------------------------
async def _call_llm(prompt: str, system_prompt: str) -> str | None:
    """
    Unified method to call the LLM, trying the DB config and server first,
    then direct Ollama, and finally raising/returning None for fallback.
    """
    try:
        from python.harness.server import handle_inference_generate
        params = {
            "prompt": prompt,
            "stream": False,
            "raw_prompt": True,
            "system_prompt": system_prompt,
            "max_tokens": 1024,
            "temperature": 0.2,
        }
        res_str = await handle_inference_generate(params, ws=None, req_id="tot-ideation")
        res_dict = json.loads(res_str)
        if "result" in res_dict:
            return res_dict["result"]["text"]
    except Exception as exc:
        log.warning("ToT Ideation: handle_inference_generate unavailable: %s", exc)

    # Secondary fallback: direct Ollama call
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": "llama3.2",
                    "prompt": f"System: {system_prompt}\nUser: {prompt}",
                    "stream": False,
                }
            )
            if resp.status_code == 200:
                return resp.json().get("response")
    except Exception as exc:
        log.warning("ToT Ideation: Direct Ollama call failed: %s", exc)

    return None

# ---------------------------------------------------------------------------
# Programmatic Fallback Generators
# ---------------------------------------------------------------------------
def _generate_fallback_approaches(problem: str) -> list[dict[str, Any]]:
    words = [w for w in re.findall(r'\w+', problem) if len(w) > 4]
    topic = " ".join(words[:3]) or "System Component"
    
    approaches = []
    for frame_name, principles in COGNITIVE_FRAMES.items():
        if frame_name == "Extreme Modularity":
            approaches.append({
                "frame": frame_name,
                "approach_name": f"Decoupled Interface-Driven {topic}",
                "architectural_strategy": f"Implement {problem} using modular interfaces.",
                "tasks": [
                    f"Implement solution: {problem}",
                    f"Verify and compile solution: {problem}"
                ],
                "key_trade_offs": "Pros: Clean implementation. Cons: Initial setup overhead."
            })
        elif frame_name == "High Performance":
            approaches.append({
                "frame": frame_name,
                "approach_name": f"High-Performance {topic}",
                "architectural_strategy": f"Optimize implementation of {problem} for performance.",
                "tasks": [
                    f"Implement solution: {problem}",
                    f"Verify and compile solution: {problem}"
                ],
                "key_trade_offs": "Pros: High performance. Cons: Higher complexity."
            })
        elif frame_name == "Fault Tolerance":
            approaches.append({
                "frame": frame_name,
                "approach_name": f"Fault-Tolerant {topic}",
                "architectural_strategy": f"Build robust implementation of {problem} with error handling.",
                "tasks": [
                    f"Implement solution: {problem}",
                    f"Verify and compile solution: {problem}"
                ],
                "key_trade_offs": "Pros: High resilience. Cons: Slightly larger codebase."
            })
        else: # Minimalism
            approaches.append({
                "frame": frame_name,
                "approach_name": f"Direct Minimalist {topic}",
                "architectural_strategy": f"Directly fulfill: {problem}",
                "tasks": [
                    f"Implement solution: {problem}",
                    f"Verify and compile solution: {problem}"
                ],
                "key_trade_offs": "Pros: Simple and direct. Cons: None."
            })
    return approaches


def _generate_fallback_critic_eval(approach: dict[str, Any]) -> dict[str, Any]:
    frame = approach["frame"]
    if frame == "Extreme Modularity":
        return {
            "novelty": {"score": 8, "critique": "Good use of standard dynamic injection pattern."},
            "viability": {"score": 9, "critique": "Highly viable; python interfaces are lightweight."},
            "fit": {"score": 8, "critique": "Fits modular decomposition needs cleanly."}
        }
    elif frame == "High Performance":
        return {
            "novelty": {"score": 7, "critique": "Standard concurrent patterns, well applied."},
            "viability": {"score": 7, "critique": "Viable but debugging concurrent race conditions is hard."},
            "fit": {"score": 9, "critique": "Excellent fit for throughput-heavy execution constraints."}
        }
    elif frame == "Fault Tolerance":
        return {
            "novelty": {"score": 8, "critique": "Resilient architecture with robust transaction logs."},
            "viability": {"score": 8, "critique": "Very viable; circuit breakers are well understood."},
            "fit": {"score": 8, "critique": "Ensures database and execution consistency."}
        }
    else: # Minimalism
        return {
            "novelty": {"score": 6, "critique": "Very standard, but lack of dependencies is a solid choice."},
            "viability": {"score": 10, "critique": "Extremely viable; standard library is always available."},
            "fit": {"score": 7, "critique": "Good for simple runs, but lacks scale controls."}
        }

def _generate_fallback_shortlist(problem: str, evaluated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Rank by average score
    for item in evaluated:
        scores = [
            item["evaluation"]["novelty"]["score"],
            item["evaluation"]["viability"]["score"],
            item["evaluation"]["fit"]["score"]
        ]
        item["score"] = round(sum(scores) / 3.0, 2)
    
    evaluated.sort(key=lambda x: x["score"], reverse=True)
    
    shortlist = []
    for item in evaluated:
        app = item["approach"]
        frame = item["frame"]
        
        # Risk matrix & traps
        if frame == "Extreme Modularity":
            risk_matrix = [
                {"risk": "Interface fragmentation", "impact": "Medium", "likelihood": "Medium", "mitigation": "Design unified APIs and enforce lint checks."},
                {"risk": "Boilerplate overload", "impact": "Low", "likelihood": "High", "mitigation": "Provide base templates for quick setup."},
                {"risk": "Dependency injection bugs", "impact": "Medium", "likelihood": "Low", "mitigation": "Verify mappings in startup health tests."}
            ]
            trap_list = ["Avoid creating single-method interfaces unnecessarily.", "Do not write complex dynamic loading code when static imports suffice."]
        elif frame == "High Performance":
            risk_matrix = [
                {"risk": "Concurrency race conditions", "impact": "High", "likelihood": "Medium", "mitigation": "Use lock-free constructs and thread-safe types."},
                {"risk": "High CPU utilization", "impact": "Medium", "likelihood": "Medium", "mitigation": "Avoid active spin-locks; yield thread where appropriate."},
                {"risk": "Cache invalidation issues", "impact": "High", "likelihood": "Medium", "mitigation": "Implement TTL or eviction events."}
            ]
            trap_list = ["Do not use global locks in performance-critical areas.", "Avoid excessive garbage collector allocation cycles."]
        elif frame == "Fault Tolerance":
            risk_matrix = [
                {"risk": "Log synchronization bottlenecks", "impact": "Medium", "likelihood": "Medium", "mitigation": "Write logs asynchronously or in batches."},
                {"risk": "Cascading service failures", "impact": "High", "likelihood": "Low", "mitigation": "Isolate subsystems with separate circuit breakers."},
                {"risk": "State divergence", "impact": "High", "likelihood": "Medium", "mitigation": "Rely on Postgres database as the sole source of truth."}
            ]
            trap_list = ["Never catch exceptions silently without logging them.", "Do not allow database locks to block execution loops."]
        else: # Minimalism
            risk_matrix = [
                {"risk": "Lack of dynamic scalability", "impact": "Medium", "likelihood": "High", "mitigation": "Keep core logic simple but extensible."},
                {"risk": "Manual utility implementation", "impact": "Low", "likelihood": "High", "mitigation": "Use python standard library built-ins instead of custom code."},
                {"risk": "Harder to maintain as scale grows", "impact": "Medium", "likelihood": "Medium", "mitigation": "Refactor to modular structure if codebase grows past limits."}
            ]
            trap_list = ["Avoid reinventing wheels that exist in python standard libraries.", "Do not skip unit testing to keep files small."]

        shortlist.append({
            "approach_name": app["approach_name"],
            "frame": frame,
            "score": item["score"],
            "architectural_strategy": app["architectural_strategy"],
            "tasks": app["tasks"],
            "risk_matrix": risk_matrix,
            "trap_list": trap_list,
            # Stamped so callers can tell reasoning from scaffolding.
            #
            # Nothing produced here looked at the problem. The critiques are
            # hardcoded per cognitive frame ("Highly viable; python interfaces
            # are lightweight"), the scores are arithmetic over those fixed
            # numbers, and the approach names are the goal text with a prefix.
            # It exists so the pipeline keeps moving when the model is
            # unavailable, which is a reasonable thing for a *plan* to be and a
            # terrible thing for a *memory* to be: a run against an unreachable
            # model was writing four invented architectures with invented
            # scores into the graph as a durable "learning event", where they
            # then competed for recall against real lessons.
            "source": "fallback",
        })
    return shortlist


def is_fallback_shortlist(shortlist: list[dict[str, Any]]) -> bool:
    """True when nothing in this shortlist came from a model looking at the problem."""
    if not shortlist:
        return True
    return all(
        isinstance(entry, dict) and entry.get("source") == "fallback"
        for entry in shortlist
    )

# ---------------------------------------------------------------------------
async def get_tot_mode() -> str:
    """
    Returns the resolved ToT mode ('full', 'fast', 'programmatic', 'disabled').
    Resolves environment variable TALA_TOT_MODE first, then auto-detects based on active model.
    """
    mode = os.environ.get("TALA_TOT_MODE", "auto").strip().lower()
    if mode in ("full", "fast", "programmatic", "fallback", "disabled"):
        if mode == "fallback":
            return "programmatic"
        return mode

    # Auto-detect mode based on active model
    try:
        from python.harness import db
        # Get active or highest priority provider
        row = await db.fetchrow(
            "SELECT config FROM inference_providers WHERE status = 'active' OR priority = 1 LIMIT 1"
        )
        if row and row["config"]:
            cfg = row["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            model_name = str(cfg.get("model", "")).lower()
            
            # Check for very small models or local names
            if any(term in model_name for term in ("0.5b", "1b", "1.5b", "3b", "gemma-2-2b", "gemma2b", "qwen2.5-0.5b", "qwen2.5-1.5b", "phi-3", "phi3")):
                return "programmatic"
            elif any(term in model_name for term in ("7b", "8b", "llama3", "llama3.1", "llama3.2", "mistral", "qwen", "local")):
                return "fast"
    except Exception as e:
        log.warning("ToT auto-detection failed: %s", e)
    
    return "fast"

# ---------------------------------------------------------------------------
# Core Tree-of-Thought Execution Pipeline
# ---------------------------------------------------------------------------
async def run_tot_ideation(problem: str) -> list[dict[str, Any]]:
    """
    Run Tree-of-Thought (ToT) ideation over a core problem.
    Spawns isolated child contexts for each cognitive lens, evaluates
    via a strict Critic Agent, and outputs a structured shortlist.
    """
    tot_mode = await get_tot_mode()
    log.info("ToT Ideation: Initiating ideation pipeline (mode=%s) for prompt: %r", tot_mode, problem)
    
    if tot_mode == "disabled":
        return []
    elif tot_mode == "programmatic":
        approaches = _generate_fallback_approaches(problem)
        evaluated = []
        for app in approaches:
            evaluated.append({
                "frame": app["frame"],
                "approach": app,
                "evaluation": _generate_fallback_critic_eval(app)
            })
        return _generate_fallback_shortlist(problem, evaluated)
    elif tot_mode == "fast":
        system_prompt = (
            "You are a principal software architect. Given a problem, propose four architectural strategies "
            "corresponding to these four conceptual lenses: Extreme Modularity, High Performance, Fault Tolerance, and Minimalism. "
            "For each approach, describe the strategy, list the tasks, evaluate it (novelty, viability, and fit scores out of 10), "
            "and provide a risk matrix and trap list. Output your response strictly as a JSON array matching the specified shortlist schema. "
            "DO NOT generate any explanatory text before or after the JSON block."
        )
        
        prompt = (
            f"Problem: \"{problem}\"\n\n"
            f"Format the response strictly as a JSON array of objects, where each object has the following keys:\n"
            f"- \"approach_name\" (str)\n"
            f"- \"frame\" (one of: \"Extreme Modularity\", \"High Performance\", \"Fault Tolerance\", \"Minimalism\")\n"
            f"- \"score\" (float, average of novelty, viability, fit)\n"
            f"- \"architectural_strategy\" (str)\n"
            f"- \"tasks\" (list of str)\n"
            f"- \"risk_matrix\" (list of objects, each containing keys: \"risk\", \"impact\", \"likelihood\", \"mitigation\")\n"
            f"- \"trap_list\" (list of str)\n\n"
            f"Output ONLY valid JSON matching the schema description above."
        )
        
        shortlist_res = await _call_llm(prompt, system_prompt)
        shortlist = None
        if shortlist_res:
            try:
                json_match = re.search(r"```json\s*(.*?)\s*```", shortlist_res, re.DOTALL | re.IGNORECASE)
                json_str = json_match.group(1) if json_match else shortlist_res
                shortlist = json.loads(json_str.strip())
            except Exception:
                pass
        
        if not shortlist:
            log.warning("ToT Ideation: LLM failed for fast shortlist. Triggering shortlist fallback.")
            approaches = _generate_fallback_approaches(problem)
            evaluated = []
            for app in approaches:
                evaluated.append({
                    "frame": app["frame"],
                    "approach": app,
                    "evaluation": _generate_fallback_critic_eval(app)
                })
            shortlist = _generate_fallback_shortlist(problem, evaluated)
        return shortlist

    # Original "full" 9-turn mode loop
    generated_approaches = []
    
    # 1. Spawn isolated generation contexts for each lens
    for frame_name, principles in COGNITIVE_FRAMES.items():
        prompt = (
            f"Approach Name: [Descriptive Name]\n"
            f"Principles to follow:\n{principles}\n\n"
            f"Propose a strategy and task list for the problem:\n\"{problem}\"\n\n"
            f"Format exactly as:\n"
            f"Approach Name: [Descriptive Name]\n"
            f"Architectural Strategy: [Detailed architecture]\n"
            f"Tasks:\n- Task 1\n- Task 2\n"
        )
        
        system_prompt = f"You are a software architect focusing on {frame_name}."
        
        approach_text = await _call_llm(prompt, system_prompt)
        
        if approach_text:
            # Parse strategy and tasks
            lines = approach_text.splitlines()
            approach_name = f"{frame_name} Approach"
            strategy = approach_text
            tasks = []
            
            for line in lines:
                if line.lower().startswith("approach name:"):
                    approach_name = line.split(":", 1)[1].strip()
                elif line.strip().startswith("-") or line.strip().startswith("*"):
                    tasks.append(line.strip(" -*"))
            
            generated_approaches.append({
                "frame": frame_name,
                "approach_name": approach_name,
                "architectural_strategy": strategy,
                "tasks": tasks or [f"Implement standard {frame_name} pattern."]
            })
        else:
            log.warning("ToT Ideation: LLM failed for frame %s. Triggering approach fallback.", frame_name)
    
    # Fallback if no contexts successfully completed
    if len(generated_approaches) < len(COGNITIVE_FRAMES):
        generated_approaches = _generate_fallback_approaches(problem)

    # 2. Evaluate each approach via Critic Agent
    evaluated_approaches = []
    for app in generated_approaches:
        frame_name = app["frame"]
        critic_prompt = (
            f"Evaluate this approach for problem: \"{problem}\"\n"
            f"Approach:\n{app['architectural_strategy']}\n"
            f"Output a JSON evaluation block with keys 'novelty', 'viability', 'fit' and 'score' for each."
        )
        
        system_prompt = (
            "You are a strict, highly skeptical senior engineer. "
            "Evaluate viability, novelty (1-10), and fit (1-10). Output JSON ONLY."
        )
        
        critic_res = await _call_llm(critic_prompt, system_prompt)
        
        evaluation = None
        if critic_res:
            try:
                # Extract JSON
                json_match = re.search(r"```json\s*(.*?)\s*```", critic_res, re.DOTALL | re.IGNORECASE)
                json_str = json_match.group(1) if json_match else critic_res
                evaluation = json.loads(json_str.strip())
            except Exception:
                pass
                
        if not evaluation:
            log.warning("ToT Ideation: LLM failed for Critic Agent evaluation. Triggering evaluation fallback.")
            evaluation = _generate_fallback_critic_eval(app)
            
        evaluated_approaches.append({
            "frame": frame_name,
            "approach": app,
            "evaluation": evaluation
        })

    # 3. Formulate JSON shortlist
    shortlist_prompt = (
        f"Problem: \"{problem}\"\n"
        f"Evaluations:\n{json.dumps(evaluated_approaches, indent=2)}\n\n"
        f"Generate the final structured JSON shortlist of the top-scored non-obvious approaches, "
        f"including risk matrices and trap lists. Use the specified JSON schema."
    )
    
    system_prompt = "You are a principal software architect. Output JSON ONLY matching the schema."
    
    shortlist_res = await _call_llm(shortlist_prompt, system_prompt)
    shortlist = None
    if shortlist_res:
        try:
            json_match = re.search(r"```json\s*(.*?)\s*```", shortlist_res, re.DOTALL | re.IGNORECASE)
            json_str = json_match.group(1) if json_match else shortlist_res
            shortlist = json.loads(json_str.strip())
        except Exception:
            pass
            
    if not shortlist:
        log.warning("ToT Ideation: LLM failed for final shortlist formulation. Triggering shortlist fallback.")
        shortlist = _generate_fallback_shortlist(problem, evaluated_approaches)

    return shortlist

