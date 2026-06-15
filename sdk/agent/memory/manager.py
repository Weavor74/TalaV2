import asyncio
import logging
from typing import Any

from sdk.agent.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

class MemoryManager:
    """
    Manages a collection of pluggable memory providers and routes queries.
    """
    def __init__(self):
        self._providers: list[MemoryProvider] = []
        self._initialized = False

    def register_provider(self, provider: MemoryProvider) -> None:
        self._providers.append(provider)

    async def initialize(self) -> None:
        if self._initialized:
            return
        
        for provider in self._providers:
            try:
                await provider.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize memory provider {provider.__class__.__name__}: {e}")
        
        self._initialized = True

    async def teardown(self) -> None:
        for provider in self._providers:
            try:
                await provider.teardown()
            except Exception as e:
                logger.error(f"Failed to teardown memory provider {provider.__class__.__name__}: {e}")
        self._initialized = False

    async def search(self, query: str, sources: list[str], limit: int = 20, allow_private: bool = False) -> list[dict[str, Any]]:
        """
        Scatter-gather search across all registered providers, returning blended top results.
        """
        tasks = []
        for provider in self._providers:
            tasks.append(
                asyncio.create_task(
                    provider.search(query=query, sources=sources, limit=limit, allow_private=allow_private)
                )
            )

        if not tasks:
            return []

        results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        
        combined_results = []
        for i, res in enumerate(results_lists):
            if isinstance(res, Exception):
                logger.error(f"MemoryProvider {self._providers[i].__class__.__name__} search failed: {res}")
            else:
                combined_results.extend(res)

        # Sort combined results by score DESC and take top limit
        combined_results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return combined_results[:limit]

    async def session_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Scatter-gather session_search across all registered providers.
        """
        tasks = []
        for provider in self._providers:
            tasks.append(
                asyncio.create_task(
                    provider.session_search(query=query, limit=limit)
                )
            )

        if not tasks:
            return []

        results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        
        combined_results = []
        for i, res in enumerate(results_lists):
            if isinstance(res, Exception):
                logger.error(f"MemoryProvider {self._providers[i].__class__.__name__} session_search failed: {res}")
            else:
                combined_results.extend(res)

        combined_results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return combined_results[:limit]

    async def store(self, source_type: str, ref_id: str, content: str, meta: dict[str, Any]) -> None:
        """
        Store/index new memory entry across all suitable providers.
        """
        tasks = []
        for provider in self._providers:
            tasks.append(
                asyncio.create_task(
                    provider.store(source_type=source_type, ref_id=ref_id, content=content, meta=meta)
                )
            )
            
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

# Global singleton
_manager = MemoryManager()

def get_manager() -> MemoryManager:
    return _manager
