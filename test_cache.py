"""
test_cache.py - Auto-generated documentation base.
"""

import asyncio
from sdk.agent.queue import GoalTreeDecomposer
from python.harness.db import init_pool, execute, fetchrow

class MockDecomposer(GoalTreeDecomposer):
    async def _decompose_via_llm(self, user_goal: str):
        # Prevent hanging
        return [{"id": "mock_id", "description": user_goal, "dependencies": []}]

async def main():
    await init_pool()
    
    goal = "cache_hygiene_test_goal"
    decomposer = MockDecomposer()
    phash = decomposer._get_prompt_hash()
    
    print(f"Current Prompt Hash: {phash}")
    
    # Insert a fake decomposition with the CORRECT hash
    import json
    fake_tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    
    await execute("DELETE FROM goal_decompositions WHERE goal = $1", goal)
    await execute(
        "INSERT INTO goal_decompositions (goal, tasks_json, prompt_hash) VALUES ($1, $2, $3)",
        goal, json.dumps(fake_tasks), phash
    )
    
    # 2. Decompose - should HIT the cache
    print("\n--- Test 1: Expect Cache HIT ---")
    tasks = await decomposer.decompose(goal)
    if tasks and tasks[0].description == "task 1":
        print("PASS: Cache hit successfully.")
    else:
        print("FAIL: Cache missed incorrectly.")
        
    # 3. Simulate a Policy Change (change the hash in the DB so it's stale)
    print("\n--- Test 2: Expect Cache MISS (stale policy) ---")
    stale_hash = "stale_hash_12345"
    await execute(
        "UPDATE goal_decompositions SET prompt_hash = $1 WHERE goal = $2",
        stale_hash, goal
    )
    
    tasks2 = await decomposer.decompose(goal)
    # The cache will miss, and it will fallback to our mocked LLM call
    if tasks2 and tasks2[0].description == goal:
        print("PASS: Cache missed because of stale hash!")
    else:
        print(f"FAIL: Cache didn't miss. Got: {[t.description for t in tasks2]}")

if __name__ == "__main__":
    asyncio.run(main())
