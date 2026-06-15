from abc import ABC, abstractmethod
from typing import Any

class MemoryProvider(ABC):
    """
    Abstract base class for all memory providers in Tala.
    """
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the provider (e.g., establish connections, load models)."""
        pass

    @abstractmethod
    async def teardown(self) -> None:
        """Teardown the provider (e.g., close connections)."""
        pass

    @abstractmethod
    async def search(self, query: str, sources: list[str], limit: int = 20, allow_private: bool = False) -> list[dict[str, Any]]:
        """
        Search memory across various sources.
        
        Returns a list of dictionaries with standard keys:
        - id
        - source_type
        - title
        - snippet
        - score
        - created_at
        - ref (dict with id)
        - trust
        """
        pass

    @abstractmethod
    async def session_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Search strictly within conversation sessions/turns.
        """
        pass

    @abstractmethod
    async def store(self, source_type: str, ref_id: str, content: str, meta: dict[str, Any]) -> None:
        """
        Store or index a new memory entry.
        """
        pass
