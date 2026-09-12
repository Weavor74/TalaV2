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

import chromadb

from sdk.agent.parser import NeuralNode, parse_file
from sdk.agent.exceptions import InvalidNodeSchemaError
from python.harness.config import settings

logger = logging.getLogger(__name__)

# Initialize ChromaDB Persistent Client
CHROMA_DATA_DIR = settings.REPO_ROOT / "data" / "chroma"


def _init_chroma_collection():
    """
    Open (or create) the persistent Chroma store.

    A corrupt store is moved aside and rebuilt fresh rather than left in
    place: the chroma index is a derived cache of nodes/*.html — never the
    source of truth — so recovery loses nothing that index_nodes() can't
    regenerate on the next call. Without this, a single corrupt store
    (confirmed twice on this machine: 2026-08-07 backup dir, and again
    2026-08-11 via a Rust PanicException out of chroma's sqlite migration
    layer) silently killed retrieval process-wide, permanently: every
    retrieve/index call logged "collection not initialized" and returned
    nothing until someone manually deleted the directory.
    """
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_DATA_DIR))
        return client.get_or_create_collection(
            name="tala_neural_nodes",
            metadata={"hnsw:space": "cosine"}
        )
    except BaseException as exc:
        logger.error("Failed to initialize ChromaDB: %s", exc)

    try:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        backup_dir = CHROMA_DATA_DIR.with_name(f"chroma.corrupted-backup-{stamp}")
        CHROMA_DATA_DIR.rename(backup_dir)
        logger.warning(
            "ChromaDB store was corrupt; moved to %s and rebuilding fresh. "
            "The index re-populates from nodes/*.html on the next index_nodes() call.",
            backup_dir,
        )
        client = chromadb.PersistentClient(path=str(CHROMA_DATA_DIR))
        return client.get_or_create_collection(
            name="tala_neural_nodes",
            metadata={"hnsw:space": "cosine"}
        )
    except BaseException as exc:
        logger.error("ChromaDB self-heal failed; retrieval disabled: %s", exc)
        return None


node_collection = _init_chroma_collection()


_INDEXED_MTIMES: dict[str, float] = {}

def _is_vault_file(path: Path) -> bool:
    return path.name.startswith("vault-")


def _vault_node_for_index(path: Path):
    """
    A vault document with its body unsealed, for the local index only.

    Returns None rather than raising if it cannot be opened: a vault the key no
    longer matches should degrade to unsearchable, not take the whole indexing
    pass down with it.
    """
    try:
        from python.harness.vault_nodes import read_document
        from sdk.agent.parser import parse_file

        opened = read_document(path.stem)
        if not opened.get("success"):
            logger.warning(
                "retrieval: vault document %s could not be unsealed and is not "
                "searchable: %s", path.stem, opened.get("error", ""),
            )
            return None

        node = parse_file(str(path))
        # Replace the safe on-disk summary with the real content, in memory.
        node.semantic_memory = f"{opened.get('title', '')}\n\n{opened.get('body', '')}"
        return node
    except Exception as exc:
        logger.warning("retrieval: could not prepare vault %s for indexing: %s",
                       path.stem, exc)
        return None

#: How far the current mood may move a retrieval score, as a fraction. At 0.3
#: a perfectly aligned node scores 1.3x and a perfectly opposed one 0.7x, so
#: mood reorders the middle of the ranking without ever promoting an
#: irrelevant node over a relevant one. Deliberately not routed through
#: MAX_ASTRO_INFLUENCE: that cap governs *style* deltas, and this is retrieval.
MOOD_WEIGHT: float = 0.3


def _node_valence(node) -> dict[str, float]:
    """
    The four valence keys off a parsed node, defaulting to neutral-ish.

    Read from the node's own ``node-metadata`` block, which ``update_manifest``
    writes and which now carries the astro projection rather than a keyword
    guess about the node's text.
    """
    from python.astrology.valence import VALENCE_KEYS

    raw: Any = {}
    try:
        meta = getattr(node, "metadata", None) or {}
        if isinstance(meta, dict):
            raw = meta.get("emotional_valence") or {}
        if not raw:
            state = getattr(node, "state_data", None) or {}
            if isinstance(state, dict):
                raw = state.get("emotional_valence") or {}
    except Exception:
        raw = {}

    out: dict[str, float] = {}
    for key in VALENCE_KEYS:
        try:
            out[key] = float(raw.get(key, 0.5)) if isinstance(raw, dict) else 0.5
        except (TypeError, ValueError):
            out[key] = 0.5
    return out


def index_nodes(nodes_dir: Path) -> None:
    """
    Parse new or modified HTML Neural Nodes and embed them into ChromaDB incrementally.
    """
    global _INDEXED_MTIMES
    if not node_collection:
        logger.error("Cannot index nodes: ChromaDB collection not initialized.")
        return

    if not nodes_dir.exists() or not nodes_dir.is_dir():
        logger.warning("Nodes directory does not exist or is not a directory: %s", nodes_dir)
        return

    nodes: list[NeuralNode] = []
    updated_mtimes: dict[str, float] = {}

    for filepath in nodes_dir.glob("*.html"):
        if "_archive_legacy" in str(filepath):
            continue

        # Vault documents are sealed on disk, so their file text is ciphertext
        # and indexing it verbatim would index noise. Steven's decision was
        # that vault bodies ARE searchable locally -- "index it locally, it
        # never leaves the machine" -- so they are unsealed for the index and
        # only for the index. The consequence is deliberate and worth stating:
        # the Chroma directory holds plaintext of the vault, which is why the
        # vault and its index are excluded from every sync and backup path.
        if _is_vault_file(filepath):
            node = _vault_node_for_index(filepath)
            if node is not None:
                nodes.append(node)
                updated_mtimes[str(filepath)] = filepath.stat().st_mtime
            continue
        try:
            mtime = filepath.stat().st_mtime
            path_str = str(filepath)
            updated_mtimes[path_str] = mtime
            if path_str in _INDEXED_MTIMES and _INDEXED_MTIMES[path_str] == mtime:
                continue

            node = parse_file(path_str)
            nodes.append(node)
        except InvalidNodeSchemaError as exc:
            logger.warning("Skipping invalid node file %s: %s", filepath.name, exc)
        except Exception as exc:
            logger.error("Failed to parse node file %s: %s", filepath.name, exc)

    if not nodes:
        _INDEXED_MTIMES.update(updated_mtimes)
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

        # The node's emotional valence, flattened for Chroma (metadata values
        # must be scalars). Without these four numbers in the index there is
        # nothing for the mood to weigh against at query time, and the
        # projection in nodes/spec-emotion-vocabulary.html stays what it has
        # always been: a table nothing reads.
        for key, value in _node_valence(node).items():
            metadata[f"valence_{key}"] = value

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
            _INDEXED_MTIMES.update(updated_mtimes)
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

    # One mood is read per query, not per candidate: the emotional state is a
    # property of Tala at this moment, not of the node being scored, and
    # re-reading it inside the loop would let it change mid-ranking.
    try:
        from python.astrology.valence import mood_alignment
    except Exception:
        mood_alignment = None  # type: ignore[assignment]
    mood_emotion: str | None = None
    mood_available = mood_alignment is not None

    for node_id, distance, metadata in zip(ids, distances, metadatas):
        # ChromaDB cosine distance: smaller is closer. similarity = 1 - distance
        raw_similarity = max(0.0, 1.0 - distance)

        weight_relevance = float(metadata.get("weight_relevance", 1.0))
        weight_confidence = float(metadata.get("weight_confidence", 1.0))

        # Multiply semantic score by HTML-NN node weights
        final_score = raw_similarity * weight_relevance * weight_confidence

        # ...and tilt by how well this node's emotional colouring suits the
        # mood the astro engine currently reports. This is the consumer the
        # emotion vocabulary node has always claimed to have: before it, the
        # engine reached the system prompt and nothing that weighted memory.
        if mood_available:
            node_valence = {
                k: float(metadata.get(f"valence_{k}", 0.5))
                for k in ("joy", "caution", "curiosity", "neutral")
            }
            aligned = mood_alignment(node_valence)
            if aligned is None:
                # No mood to compare against. Leave the score untouched and say
                # so once, rather than silently scoring as though the mood were
                # neutral -- a state nobody measured must not look measured.
                mood_available = False
            else:
                affinity, mood_emotion = aligned
                final_score *= 1.0 + MOOD_WEIGHT * (affinity - 0.5) * 2.0

        if final_score >= threshold:
            scored_candidates.append((node_id, final_score))

    if mood_emotion:
        logger.debug(
            "retrieval: ranking tilted by current emotional state %r (weight %.2f)",
            mood_emotion, MOOD_WEIGHT,
        )
    elif mood_alignment is not None:
        logger.debug("retrieval: no emotional state available; ranking untilted")

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
