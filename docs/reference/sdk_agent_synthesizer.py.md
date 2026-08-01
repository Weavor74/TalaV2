# File: `synthesizer.py`

## Module Description

sdk/agent/synthesizer.py
========================
Synthesizer Worker for aggregating parallel tasks

## Classes

### `class SynthesizerWorker`
Acts as a sink node for parallel Swarm executions, aggregating their results
and generating a unified synthesis report.

- **`def synthesize(cls, goal_text, upstream_results)`**
  - Aggregate results from upstream dependencies.

## Source Code

```python
"""
sdk/agent/synthesizer.py
========================
Synthesizer Worker for aggregating parallel tasks
"""
import logging
from typing import Any, Dict, List
import json

log = logging.getLogger(__name__)

class SynthesizerWorker:
    """
    Acts as a sink node for parallel Swarm executions, aggregating their results
    and generating a unified synthesis report.
    """
    
    @classmethod
    async def synthesize(cls, goal_text: str, upstream_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Aggregate results from upstream dependencies.
        """
        log.info(f"SynthesizerWorker: Synthesizing {len(upstream_results)} results for goal: '{goal_text}'.")
        
        # 1. Fetch prior evidence
        prior_evidence_text = "No prior evidence found."
        try:
            from python.harness.memory import handle_research_unified_search
            evidence_res = await handle_research_unified_search({"query": goal_text, "sources": ["all"]})
            results = evidence_res.get("results", [])
            
            # Sort by score and take top 5
            results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            top_results = results[:5]
            
            if top_results:
                evidence_blocks = []
                for r in top_results:
                    evidence_blocks.append(f"Source [{r.get('source_type')}]: {r.get('snippet', '')}")
                prior_evidence_text = "\\n\\n".join(evidence_blocks)
        except Exception as exc:
            log.warning(f"SynthesizerWorker failed to fetch prior evidence: {exc}")
            
        # 2. Format upstream outputs
        aggregated_output = []
        all_success = True
        
        for idx, result in enumerate(upstream_results):
            if not result.get("success", False):
                all_success = False
            aggregated_output.append(f"--- Upstream Node {result.get('node_id', idx+1)} Output ---\\n{result.get('output', 'No output')}")
            
        upstream_text = "\\n\\n".join(aggregated_output)
        
        # 3. Call LLM to synthesize
        prompt = f"""
You are the final Synthesizer for a Swarm of agents.
Your Goal was: "{goal_text}"

You have two streams of information.
1. Prior Evidence (historical memory, nodes, web context related to the goal):
{prior_evidence_text}

2. Upstream Outputs (the exact outputs of the worker agents who just ran):
{upstream_text}

Combine these into a cohesive, highly readable Synthesis Report. Address the Goal using the upstream outputs, and enrich it or validate it using the Prior Evidence. Do not mention "Agent 1" or "Prior Evidence" mechanically—weave it together naturally.
Output only the markdown report.
"""
        
        final_report = "Synthesis failed."
        try:
            from python.harness.server import handle_inference_generate
            params = {
                "prompt": prompt,
                "stream": False,
                "raw_prompt": True,
                "system_prompt": "You are a master technical writer and orchestrator."
            }
            res_str = await handle_inference_generate(params, req_id="synthesizer")
            res_dict = json.loads(res_str)
            if "result" in res_dict:
                final_report = res_dict["result"]["text"].strip()
            else:
                final_report = f"LLM Error: {res_dict.get('error')}\\n\\nRaw Upstreams:\\n{upstream_text}"
        except Exception as exc:
            log.warning(f"SynthesizerWorker LLM generation failed: {exc}")
            final_report = f"Fallback Synthesis:\\n{upstream_text}"
            
        if not all_success:
            final_report = "**Warning**: One or more upstream tasks failed.\\n\\n" + final_report
            
        return {
            "success": all_success,
            "output": final_report
        }

```
