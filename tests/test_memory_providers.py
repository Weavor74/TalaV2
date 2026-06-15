import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from sdk.agent.memory.manager import MemoryManager
from sdk.agent.memory.provider import MemoryProvider

class MockProvider(MemoryProvider):
    def __init__(self, name, results, fail=False):
        self.name = name
        self.results = results
        self.fail = fail
        self.initialized = False
        self.stored = []
        self.session_results = results

    async def initialize(self):
        self.initialized = True

    async def teardown(self):
        self.initialized = False

    async def search(self, query, sources, limit=20, allow_private=False):
        if self.fail:
            raise RuntimeError(f"Provider {self.name} failed")
        return self.results

    async def session_search(self, query, limit=20):
        if self.fail:
            raise RuntimeError(f"Provider {self.name} failed session search")
        return self.session_results

    async def store(self, source_type, ref_id, content, meta):
        self.stored.append((source_type, ref_id, content, meta))

@pytest.mark.asyncio
async def test_memory_manager_search():
    manager = MemoryManager()
    
    provider1 = MockProvider("p1", [{"id": "p1-1", "score": 0.8}, {"id": "p1-2", "score": 0.5}])
    provider2 = MockProvider("p2", [{"id": "p2-1", "score": 0.9}])
    provider3 = MockProvider("p3", [], fail=True)
    
    manager.register_provider(provider1)
    manager.register_provider(provider2)
    manager.register_provider(provider3)
    
    await manager.initialize()
    assert provider1.initialized
    
    results = await manager.search("test", ["session"], limit=2)
    # Expect sorted by score DESC, and gracefully ignored provider3's failure
    assert len(results) == 2
    assert results[0]["id"] == "p2-1"
    assert results[1]["id"] == "p1-1"

@pytest.mark.asyncio
async def test_memory_manager_session_search():
    manager = MemoryManager()
    
    provider1 = MockProvider("p1", [{"id": "turn-1", "score": 0.9}])
    manager.register_provider(provider1)
    
    results = await manager.session_search("test", limit=5)
    assert len(results) == 1
    assert results[0]["id"] == "turn-1"

@pytest.mark.asyncio
async def test_memory_manager_store():
    manager = MemoryManager()
    
    provider1 = MockProvider("p1", [])
    provider2 = MockProvider("p2", [])
    manager.register_provider(provider1)
    manager.register_provider(provider2)
    
    await manager.store("session", "ref-1", "content data", {"title": "Test"})
    assert len(provider1.stored) == 1
    assert len(provider2.stored) == 1
    assert provider1.stored[0][2] == "content data"
