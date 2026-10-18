/**
 * HTML-NN document serializer for the visual NodeEditor and onboarding seeds.
 * Classic script: loaded before renderer.js, which calls serializeNodeToHTML
 * as a global. Moved out of renderer.js to keep it under its max-lines pin.
 */
'use strict';

function nodeSerializerEscape(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function serializeNodeToHTML(node) {
  const nowStr = new Date().toISOString();
  const createdStr = node.created || nowStr;
  
  const relsHtml = node.relationships && node.relationships.length > 0
    ? node.relationships.map(rel => {
        const weightAttr = rel.weight !== undefined ? ` data-relationship-weight="${parseFloat(rel.weight).toFixed(2)}"` : '';
        return `      <li>
        <a href="${nodeSerializerEscape(rel.href)}" data-relationship-type="${nodeSerializerEscape(rel.type)}"${weightAttr}>
          ${nodeSerializerEscape(rel.label || rel.href)}
        </a>
      </li>`;
      }).join('\n')
    : '      <!-- No relationship links defined. -->';

  const stateBlock = {
    title: node.title,
    description: node.description || '',
    tags: node.tags || [],
    sections: [
      {
        title: 'Introduction',
        content: node.memory,
        tags: ['intro']
      }
    ],
    metadata: {
      author: node.author || 'human',
      created: createdStr,
      version: node.version || '1.0.0',
      schema: 'neural-node/v2'
    }
  };

  return `<!DOCTYPE html>
<!--
  TALA COGNITIVE ENGINE ROOM — HTML NEURAL NODE
  Generated visually by NodeEditor.
-->
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="tala-schema" content="neural-node/v2">
  <meta name="tala-node-id"   content="${nodeSerializerEscape(node.id)}">
  <meta name="tala-node-type" content="${nodeSerializerEscape(node.type)}">
  <meta name="tala-version"   content="${nodeSerializerEscape(stateBlock.metadata.version)}">
  <meta name="tala-created"   content="${nodeSerializerEscape(stateBlock.metadata.created)}">
  <meta name="tala-author"    content="${nodeSerializerEscape(stateBlock.metadata.author)}">

  <title>Neural Node: ${nodeSerializerEscape(node.title)}</title>
</head>
<body>
<article
  data-node-id="${nodeSerializerEscape(node.id)}"
  data-node-type="${nodeSerializerEscape(node.type)}"
  data-execution-authority="${nodeSerializerEscape(node.authority)}"
  data-memory-authority="${nodeSerializerEscape(node.memoryAuthority || '0.00')}"
  data-weight-relevance="${nodeSerializerEscape(node.relevance)}">

  <header id="node-identity">
    <h1 id="node-title">${nodeSerializerEscape(node.title)}</h1>
    <dl>
      <dt>Node ID</dt>
      <dd><code>${nodeSerializerEscape(node.id)}</code></dd>
      <dt>Type</dt>
      <dd><code>${nodeSerializerEscape(node.type)}</code></dd>
      <dt>Execution Authority</dt>
      <dd><code>${nodeSerializerEscape(node.authority)}</code></dd>
      <dt>Memory Description</dt>
      <dd>${nodeSerializerEscape(node.description || '')}</dd>
      <dt>Version</dt>
      <dd><code>${nodeSerializerEscape(stateBlock.metadata.version)}</code></dd>
      <dt>Created</dt>
      <dd><time datetime="${nodeSerializerEscape(stateBlock.metadata.created)}">${nodeSerializerEscape(stateBlock.metadata.created.split('T')[0])}</time></dd>
    </dl>
  </header>

  <section id="math-weights">
    <h2>Activation Math Weights</h2>
    <div class="math-weights">
      <table>
        <tbody>
          <tr><td>weight_relevance</td><td>${nodeSerializerEscape(node.relevance)}</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section id="semantic-memory">
    <h2>Semantic Memory</h2>
    <p>${nodeSerializerEscape(node.memory)}</p>
  </section>

  <nav id="relationship-anchors" aria-label="Node relationships">
    <h2>Relationships</h2>
    <ul>
${relsHtml}
    </ul>
  </nav>

  <script type="application/json" id="node-state">
${JSON.stringify(stateBlock, null, 2)}
  </script>
</article>
</body>
</html>`;
}

if (typeof window !== 'undefined') window.serializeNodeToHTML = serializeNodeToHTML;
if (typeof module !== 'undefined') module.exports = { serializeNodeToHTML };
