import asyncio
import logging
from sdk.agent.goal_engine import GoalEngine
from python.harness import db

logging.basicConfig(level=logging.INFO)

async def test():
    # Initialize DB pool for telemetry/guardrails checks
    await db.init_pool()
    
    engine = GoalEngine()
    goal = "Create a hello.txt file that says hello world"
    phases = [
        "Create hello.txt",
        "Write 'hello world' into hello.txt using the file system"
    ]
    # Trigger auto document which triggers synthesis and HTML generation
    await engine._auto_document_skill(goal, phases, "test-task-123")

if __name__ == "__main__":
    asyncio.run(test())
