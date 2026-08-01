# File: `retrieval.py`

## Module Description

retrieval.py — Tala Cognitive Engine Room: Semantic Node Retrieval Engine
=======================================================================
Crawls the nodes directory, parses HTML Neural Nodes, and performs a local,
offline-first Semantic RAG similarity search using ChromaDB.

Milestone 11 — Task 11.1.1

## Functions

### `def index_nodes(nodes_dir)`
Parse all HTML Neural Nodes and embed them into the ChromaDB collection.

### `def retrieve_relevant_nodes(query, nodes_dir, top_k, threshold, where_filter)`
Search for relevant nodes using ChromaDB vector similarity, and multiply
the raw cosine similarity score by the node's confidence and relevance weights.

Parameters
----------
query:
    The search string (typically the user's prompt).
nodes_dir:
    Directory containing .html Neural Node files.
top_k:
    Maximum number of matched nodes to return.
threshold:
    Final activation score cutoff. Matches below this are discarded.
where_filter:
    Optional ChromaDB metadata filter dict (e.g. {"trust_tier": "public"}).

## Source Code

```python
"""
retrieval.py — Tala Cognitive Engine Room: Semantic Node Retrieval Engine
=======================================================================
Crawls the nodes directory, parses HTML Neural Nodes, and performs a local,
offline-first Semantic RAG similarity search using ChromaDB.

Milestone 11 — Task 11.1.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import json

import chromadb
from chromadb.config import Settings as ChromaSettings

from sdk.agent.parser import NeuralNode, parse_file
from sdk.agent.exceptions import InvalidNodeSchemaError
from python.harness.config import settings

logger = logging.getLogger(__name__)

# Initialize ChromaDB Persistent Client
CHROMA_DATA_DIR = settings.REPO_ROOT / "data" / "chroma"
try:
    chroma_client = chromadb.PersistentClient(path=str(CHROMA_DATA_DIR))
    node_collection = chroma_client.get_or_create_collection(
        name="tala_neural_nodes",
        metadata={"hnsw:space": "cosine"}
    )
except Exception as exc:
    logger.error(f"Failed to initialize ChromaDB: {exc}")
    node_collection = None


def index_nodes(nodes_dir: Path) -> None:
    """
    Parse all HTML Neural Nodes and embed them into the ChromaDB collection.
    """
    if not node_collection:
        logger.error("Cannot index nodes: ChromaDB collection not initialized.")
        return

    if not nodes_dir.exists() or not nodes_dir.is_dir():
        logger.warning("Nodes directory does not exist or is not a directory: %s", nodes_dir)
        return

    nodes: list[NeuralNode] = []
    for filepath in nodes_dir.glob("*.html"):
        if "_archive_legacy" in str(filepath):
            continue
        try:
            node = parse_file(str(filepath))
            nodes.append(node)
        except InvalidNodeSchemaError as exc:
            logger.warning("Skipping invalid node file %s: %s", filepath.name, exc)
        except Exception as exc:
            logger.error("Failed to parse node file %s: %s", filepath.name, exc)

    if not nodes:
        return

    ids = []
    documents = []
    metadatas = []

    for node in nodes:
        title = str(node.state_data.get("title", ""))
        # Construct the document to embed
        document = f"{node.node_id.replace('-', ' ')}\nTitle: {title}\nMemory: {node.semantic_memory}"
        
        # Build metadata for filtering and scoring
        metadata = {
            "node_type": node.node_type,
            "governance_status": node.governance_status,
            "weight_relevance": float(node.weight_relevance),
            "weight_confidence": float(node.weight_confidence),
            "trust_tier": str(node.trust_tier) if node.trust_tier else "unknown",
            "source": str(node.source) if node.source else "local",
        }

        ids.append(node.node_id)
        documents.append(document)
        metadatas.append(metadata)

    if ids:
        try:
            # Upsert overwrites existing nodes with the same ID, or inserts new ones
            node_collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
            logger.info(f"Successfully indexed {len(ids)} Neural Nodes into ChromaDB.")
        except Exception as exc:
            logger.error(f"Failed to upsert nodes into ChromaDB: {exc}")


def retrieve_relevant_nodes(
    query: str,
    nodes_dir: Path,
    top_k: int = 2,
    threshold: float = 0.05,
    where_filter: dict[str, Any] | None = None
) -> list[tuple[NeuralNode, float]]:
    """
    Search for relevant nodes using ChromaDB vector similarity, and multiply
    the raw cosine similarity score by the node's confidence and relevance weights.

    Parameters
    ----------
    query:
        The search string (typically the user's prompt).
    nodes_dir:
        Directory containing .html Neural Node files.
    top_k:
        Maximum number of matched nodes to return.
    threshold:
        Final activation score cutoff. Matches below this are discarded.
    where_filter:
        Optional ChromaDB metadata filter dict (e.g. {"trust_tier": "public"}).
    """
    if not node_collection:
        logger.error("Cannot retrieve nodes: ChromaDB collection not initialized.")
        return []

    if not query.strip():
        return []

    # First, ensure nodes are indexed
    index_nodes(nodes_dir)

    try:
        # Request more than top_k from chroma because weights might re-rank them
        fetch_k = max(top_k * 3, 10)
        
        results = node_collection.query(
            query_texts=[query],
            n_results=fetch_k,
            where=where_filter,
            include=["metadatas", "distances"]
        )
    except Exception as exc:
        logger.error(f"ChromaDB query failed: {exc}")
        return []

    if not results or not results.get("ids") or not results["ids"][0]:
        return []

    ids = results["ids"][0]
    distances = results["distances"][0] if "distances" in results and results["distances"] else [0.0]*len(ids)
    metadatas = results["metadatas"][0] if "metadatas" in results and results["metadatas"] else [{}]*len(ids)

    scored_candidates = []
    
    for node_id, distance, metadata in zip(ids, distances, metadatas):
        # ChromaDB cosine distance: smaller is closer. similarity = 1 - distance
        raw_similarity = max(0.0, 1.0 - distance)
        
        weight_relevance = float(metadata.get("weight_relevance", 1.0))
        weight_confidence = float(metadata.get("weight_confidence", 1.0))
        
        # Multiply semantic score by HTML-NN node weights
        final_score = raw_similarity * weight_relevance * weight_confidence
        
        if final_score >= threshold:
            scored_candidates.append((node_id, final_score))

    # Sort by final score descending
    scored_candidates.sort(key=lambda x: x[1], reverse=True)
    scored_candidates = scored_candidates[:top_k]

    if not scored_candidates:
        return []

    # Load actual NeuralNode objects from disk for the top results
    final_nodes: list[tuple[NeuralNode, float]] = []
    for node_id, score in scored_candidates:
        node_path = nodes_dir / f"{node_id}.html"
        if node_path.exists():
            try:
                node = parse_file(str(node_path))
                final_nodes.append((node, score))
            except Exception as exc:
                logger.warning(f"Failed to load node {node_id} during retrieval: {exc}")
        else:
            # Fallback scan
            for filepath in nodes_dir.glob("*.html"):
                if filepath.stem == node_id:
                    try:
                        node = parse_file(str(filepath))
                        final_nodes.append((node, score))
                    except Exception:
                        pass
                    break

    return final_nodes

```
