# File: `base_node.html`

## Overview

The base Jinja2 HTML template that all Neural Nodes inherit from.

## File Contents

```html
<!DOCTYPE html>
<!--
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  TALA COGNITIVE ENGINE ROOM — HTML NEURAL NODE TEMPLATE             ║
  ║  Schema: neural-node/v1                                             ║
  ║  Milestone: 2 — Neural Node Filesystem                              ║
  ╚══════════════════════════════════════════════════════════════════════╝

  USAGE
  -----
  1. Copy this file to your node directory.
  2. Rename it to reflect the node's ID, e.g. "concept-trust.html".
  3. Fill in every <!-- REPLACE: ... --> placeholder.
  4. Do NOT remove any required attributes or structural elements.

  REQUIRED ATTRIBUTES (parser will raise InvalidNodeSchemaError if missing)
  -------------------------------------------------------------------------
  data-node-id              : Stable, unique, kebab-case identifier.
  data-node-type            : Categorical type (see valid types below).
  data-execution-authority  : Float in [0.0, 1.0]. IMMUTABLE after authoring.

  VALID NODE TYPES
  ----------------
  concept | persona | rule | tool | memory | event | entity | goal | constraint

  AUTHORITY BOUNDARY LAW
  ----------------------
  data-execution-authority is set ONCE at authoring time and is permanently
  immutable. No activation score, propagation cascade, or external signal
  may alter it. Attempting to do so raises AuthorityBoundaryViolation.

    0.00 – 0.25  : Observational / read-only
    0.26 – 0.50  : Advisory / soft-influence
    0.51 – 0.75  : Operational / can trigger actions
    0.76 – 1.00  : Governing / can modify other nodes

  RELATIONSHIP TYPES (data-relationship-type on <a> elements)
  -----------------------------------------------------------
  depends-on | extends | contradicts | authored-by | supersedes |
  references | implements | governed-by | owns | peer-of
-->
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v1">
  <meta name="tala-milestone" content="2">

  <!--
    SECTION 1: IDENTITY & GOVERNANCE
    These meta tags are parsed by the Tala indexer for fast lookups.
    They MUST match the data-* attributes on <article>.
  -->
  <meta name="tala-node-id"   content="<!-- REPLACE: your-node-id -->">
  <meta name="tala-node-type" content="<!-- REPLACE: concept -->">
  <meta name="tala-authority" content="<!-- REPLACE: 0.00 -->">
  <meta name="tala-version"   content="1.0.0">
  <meta name="tala-created"   content="<!-- REPLACE: 2026-01-01T00:00:00Z (ISO-8601 UTC) -->">
  <meta name="tala-author"    content="<!-- REPLACE: agent-id or human author -->">

  <title>Neural Node: <!-- REPLACE: your-node-id --></title>

  <style>
    /* Minimal styling — the renderer applies its own theme. */
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; }
    article { border: 1px solid #ccc; border-radius: 6px; padding: 1.5rem; }
    section  { margin-top: 1.5rem; }
    nav ul   { list-style: none; padding: 0; }
    nav li   { margin: 0.4rem 0; }
    .math-weights { background: #f9f9f9; padding: 1rem; border-radius: 4px; font-family: monospace; }
    code { background: #eee; padding: 0.1rem 0.3rem; border-radius: 3px; }
  </style>
</head>

<body>

<!--
  ════════════════════════════════════════════════════════════════════════
  ROOT ARTICLE ELEMENT — The parser locates this element first.
  ALL THREE data-* attributes are REQUIRED.
  ════════════════════════════════════════════════════════════════════════
-->
<article
  data-node-id="<!-- REPLACE: your-node-id -->"
  data-node-type="<!-- REPLACE: concept -->"
  data-execution-authority="<!-- REPLACE: 0.00 -->"
  data-weight-relevance="<!-- REPLACE: 0.50 (optional, default=0.50) -->">


  <!-- ──────────────────────────────────────────────────────────────────
       SECTION 1: IDENTITY & GOVERNANCE
       Rendered in the node inspector header panel.
  ─────────────────────────────────────────────────────────────────── -->
  <header id="node-identity">
    <h1 id="node-title"><!-- REPLACE: Human-readable node title --></h1>

    <dl>
      <dt>Node ID</dt>
      <dd><code><!-- REPLACE: your-node-id --></code></dd>

      <dt>Type</dt>
      <dd><code><!-- REPLACE: concept --></code></dd>

      <dt>Execution Authority</dt>
      <dd>
        <code><!-- REPLACE: 0.00 --></code>
        <!--
          REMINDER: This value is IMMUTABLE (Authority Boundary Law).
          0.00–0.25 = Observational | 0.26–0.50 = Advisory
          0.51–0.75 = Operational  | 0.76–1.00 = Governing
        -->
      </dd>

      <dt>Version</dt>
      <dd><code>1.0.0</code></dd>

      <dt>Created</dt>
      <dd><time datetime="<!-- REPLACE: 2026-01-01T00:00:00Z -->">
        <!-- REPLACE: Human-readable creation date -->
      </time></dd>
    </dl>
  </header>


  <!-- ──────────────────────────────────────────────────────────────────
       SECTION 2: MATH WEIGHTS
       Documents the numeric weights used by the activation engine.
       These are informational — the activation engine reads from
       data-* attributes and the JSON state block, not from this section.
  ─────────────────────────────────────────────────────────────────── -->
  <section id="math-weights">
    <h2>Activation Math Weights</h2>
    <p>
      These weights feed into the Tala activation formula:<br>
      <code>activation = semantic_match × relevance_weight × confidence_weight
        × freshness_weight × provider_trust_multiplier × governance_multiplier</code>
    </p>

    <div class="math-weights">
      <table>
        <thead>
          <tr>
            <th>Weight</th>
            <th>Value</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>weight_relevance</td>
            <td><!-- REPLACE: 0.50 --></td>
            <td>Intrinsic importance in the graph (set at authoring time).</td>
          </tr>
          <tr>
            <td>confidence_weight</td>
            <td><!-- REPLACE: 1.00 --></td>
            <td>Epistemic confidence in this node's content.</td>
          </tr>
          <tr>
            <td>freshness_weight</td>
            <td><!-- REPLACE: 1.00 --></td>
            <td>Computed at query-time; reflects recency of last update.</td>
          </tr>
          <tr>
            <td>provider_trust_multiplier</td>
            <td><!-- REPLACE: 1.00 --></td>
            <td>Trust score of the content provider.</td>
          </tr>
          <tr>
            <td>governance_multiplier</td>
            <td><!-- REPLACE: 0.00 --></td>
            <td>Derived from execution_authority at query-time.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>


  <!-- ──────────────────────────────────────────────────────────────────
       SECTION 3: SEMANTIC MEMORY
       The parser extracts text from this section (id="semantic-memory")
       for embedding and keyword analysis.
       Write a dense, factual description of what this node represents.
  ─────────────────────────────────────────────────────────────────── -->
  <section id="semantic-memory">
    <h2>Semantic Memory</h2>

    <!--
      REPLACE: Write 1–5 paragraphs describing this node's knowledge.
      Be specific and use domain terminology — this text is vectorised.
      Avoid filler phrases like "This node is about...".
    -->
    <p><!-- REPLACE: Core definition of this concept/rule/tool/etc. --></p>

    <p><!-- REPLACE: Context, background, and nuance (optional). --></p>

    <p><!-- REPLACE: Edge cases, exceptions, or caveats (optional). --></p>
  </section>


  <!-- ──────────────────────────────────────────────────────────────────
       SECTION 4: RELATIONSHIP ANCHORS
       Each <a> must have data-relationship-type set.
       The parser collects all <a data-relationship-type="..."> elements.

       Valid relationship types:
         depends-on | extends | contradicts | authored-by | supersedes |
         references | implements | governed-by | owns | peer-of
  ─────────────────────────────────────────────────────────────────── -->
  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
      <!--
        REPLACE: Add one <li> per relationship.
        href: relative path to the linked node's .html file.
        data-relationship-type: one of the valid types listed above.
        Text content: human-readable label for this link.
      -->
      <li>
        <a href="<!-- REPLACE: ./related-node.html -->"
           data-relationship-type="<!-- REPLACE: depends-on -->">
          <!-- REPLACE: Human-readable label for this relationship -->
        </a>
      </li>

      <!--
        Add more <li> entries as needed. Remove this comment block
        and the example above when you have real relationships.
      -->
    </ul>
  </nav>


  <!-- ──────────────────────────────────────────────────────────────────
       SECTION 5: JSON STATE BLOCK
       SECURITY: This block is parsed ONLY with json.loads().
                 eval(), exec(), and ast.literal_eval() are NEVER used.
       TYPE:     The root value MUST be a JSON object {}.
                 JSON arrays, strings, and numbers are REJECTED.

       Required keys:
         title        (string)  : Display title matching <h1>
         tags         (array)   : Taxonomy tags for this node

       Optional keys (extend freely):
         description  (string)  : One-line summary
         sections     (array)   : List of {title, content, tags} objects
         metadata     (object)  : Arbitrary key-value metadata
         config       (object)  : Runtime configuration for tool nodes
  ─────────────────────────────────────────────────────────────────── -->
  <script type="application/json" id="node-state">
  {
    "title": "<!-- REPLACE: Your Node Title -->",
    "description": "<!-- REPLACE: One-line description of this node -->",
    "tags": [
      "<!-- REPLACE: tag-one -->",
      "<!-- REPLACE: tag-two -->"
    ],
    "sections": [
      {
        "title": "<!-- REPLACE: Section heading -->",
        "content": "<!-- REPLACE: Section body text -->",
        "tags": ["<!-- REPLACE: section-tag -->"]
      }
    ],
    "metadata": {
      "author": "<!-- REPLACE: agent-id or human name -->",
      "created": "<!-- REPLACE: 2026-01-01T00:00:00Z -->",
      "version": "1.0.0",
      "schema": "neural-node/v1"
    }
  }
  </script>


</article><!-- end <article data-node-id="..."> -->

</body>
</html>

```
