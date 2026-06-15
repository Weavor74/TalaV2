import sys
import asyncio
from sdk.agent.tools import generate_detailed_neural_node

async def run_test():
    detailed_markdown = """**Description:** This function collects and provides real-time metrics about the system.
**Inputs:**
* None
**Outputs:**
* A dictionary containing metrics.
"""
    result = await generate_detailed_neural_node(
        node_id="function-get_system_metrics",
        title="Get System Metrics",
        node_type="function",
        execution_authority="0.1",
        detailed_markdown=detailed_markdown
    )
    print("Result:", result)

asyncio.run(run_test())
