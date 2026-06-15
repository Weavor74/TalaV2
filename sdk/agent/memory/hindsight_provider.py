import httpx
import logging
from typing import Any

from sdk.agent.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

class HindsightProvider(MemoryProvider):
    """
    Integrates with Hindsight (or equivalent OS/Screen context daemon) 
    via a local HTTP endpoint.
    """
    def __init__(self, endpoint: str = "http://localhost:8080/api/search"):
        self.endpoint = endpoint
        self.client = None

    async def initialize(self) -> None:
        self.client = httpx.AsyncClient(timeout=3.0)
        logger.info(f"HindsightProvider initialized targeting {self.endpoint}")

    async def teardown(self) -> None:
        if self.client:
            await self.client.aclose()

    async def search(self, query: str, sources: list[str], limit: int = 20, allow_private: bool = False) -> list[dict[str, Any]]:
        if not query or not self.client:
            return []

        try:
            response = await self.client.post(
                self.endpoint,
                json={"query": query, "limit": limit},
            )
            response.raise_for_status()
            data = response.json()
            
            results = []
            for item in data.get("results", []):
                results.append({
                    "id": f"hindsight-{item.get('id', 'unknown')}",
                    "source_type": "hindsight",
                    "title": item.get("title", "Hindsight Result"),
                    "snippet": item.get("snippet", ""),
                    "score": float(item.get("score", 0.5)),
                    "created_at": item.get("timestamp", ""),
                    "ref": {"id": item.get("id", "")},
                    "trust": "external"
                })
            return results
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"HindsightProvider search failed: {e}")
            return []
        except Exception as e:
            logger.error(f"HindsightProvider unexpected error: {e}")
            return []

    async def session_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        # Hindsight doesn't strictly search Tala sessions, so return empty 
        # or we could map to a specific hindsight session scope if supported.
        return []

    async def store(self, source_type: str, ref_id: str, content: str, meta: dict[str, Any]) -> None:
        # Currently, Hindsight captures context out-of-band via OS. 
        # Writing directly to Hindsight from Tala is not typically implemented.
        pass
