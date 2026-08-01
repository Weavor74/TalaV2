# File: `system-semantic-rag-federated-search.html`

## Overview

Instructs the agent on how to use vector search and semantic memory tools.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id" content="system-semantic-rag-federated-search">
  <meta name="tala-node-type" content="documentation">
  <meta name="tala-version" content="1.0.0">
  <meta name="tala-author" content="tala-system">
  <title>Neural Node: Semantic RAG & Federated Search Architecture</title>
</head>
<body>
<article
  data-node-id="system-semantic-rag-federated-search"
  data-node-type="documentation"
  data-schema-version="neural-node/v2"
  data-execution-authority="0.00"
  data-memory-authority="0.00"
  data-provider-authority="0.00"
  data-wallet-authority="0.00"
  data-tool-authority="0.00"
  data-weight-relevance="1.00"
  data-weight-confidence="1.00"
  data-governance-status="context-only"
  data-doc-target="semantic-rag-federated-search"
  data-source="local"
  data-trust-tier="system"
  data-privacy="public">

  <header id="node-identity">
    <h1 id="node-title">Semantic RAG & Federated Search Architecture</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>system-semantic-rag-federated-search</code></dd>
      <dt>Type</dt>
      <dd><code>documentation</code></dd>
      <dt>Governance Status</dt>
      <dd><code>context-only</code></dd>
      <dt>Memory Description</dt>
      <dd>Documentation of the Vector Database (ChromaDB) and Federated P2P Search mechanism.</dd>
    </dl>
  </header>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>Tala utilizes a local ChromaDB vector database to provide Semantic RAG across the HTML Neural Node (HTML-NN) ecosystem. When nodes are indexed, their text and metadata (including weights and trust tiers) are embedded. When a search occurs, the cosine similarity of the vectors is calculated and multiplied by the node's `weight_confidence` and `weight_relevance` to produce a final contextual activation score.</p>
    <p>Additionally, Tala supports Federated P2P Search. Trusted remote peers can send a JSON-RPC 2.0 request (`p2p.semantic_search`) to query the local ChromaDB. The results are securely filtered using `access_rights` checks (e.g., ensuring `trust_tier` is not `internal`) to ensure private memory remains isolated.</p>
  </section>

  <script type="application/json" id="node-state" data-cognitive-block="state">
{
  "title": "Semantic RAG & Federated Search Architecture",
  "description": "Explains how ChromaDB empowers Hybrid Semantic Search and P2P Federated memory sharing.",
  "tags": [
    "html-nn",
    "architecture",
    "rag",
    "chromadb",
    "vector-database",
    "p2p",
    "federated-search"
  ]
}
  </script>
</article>
</body>
</html>

```
