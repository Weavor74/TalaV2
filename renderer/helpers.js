/**
 * renderer/helpers.js
 * ===================
 * The shared DOM/IPC helpers every renderer module leans on. Deliberately a
 * plain (non-module) script — loaded via a classic <script> tag in index.html
 * *before* renderer.js (which is `type="module"`), so `qs`, `fmtTime` and
 * `invoke` are real globals that renderer.js, thinking_blocks.js-style
 * companion scripts, and plain-Node Jest tests can all reach without an ESM
 * pipeline. Same precedent as thinking_blocks.js, and the first step of the
 * renderer.js cut-down: helpers had to move out first, because a classic
 * companion script cannot see bindings scoped inside the renderer module.
 */

/**
 * Safely query a single DOM element.
 * @param {string} selector
 * @param {Element} [root=document]
 * @returns {Element|null}
 */
function qs(selector, root = document) {
  return root.querySelector(selector);
}

/**
 * Format a timestamp as HH:MM:SS.
 * @param {Date} [d]
 * @returns {string}
 */
function fmtTime(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

/**
 * Invoke a whitelisted IPC channel with a payload.
 * Falls back gracefully if the contextBridge is not available (e.g., during tests).
 *
 * // Context Bridge Relay: Provides a type-safe boundary between the sandboxed UI (renderer)
 * // and the Node.js/Python backend. All OS-level operations (file I/O, database queries)
 * // must pass through this funnel to prevent malicious Neural Nodes from escaping the browser sandbox.
 *
 * @param {string} channel
 * @param {*} [data]
 * @returns {Promise<*>}
 */
async function invoke(channel, data) {
  if (window.tala && typeof window.tala.invoke === 'function') {
    return window.tala.invoke(channel, data);
  }
  console.warn('[renderer] window.tala not available — running in degraded mode');
  return null;
}

/* Helper: HTML-NN document parser */
function parseNodeFromHTML(htmlText, filename) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(htmlText, 'text/html');
  let article = doc.querySelector('article');
  
  if (!article) {
    article = doc.querySelector('.neural-node');
  }
  
  if (!article) {
    throw new Error('No <article> or .neural-node element found in node file.');
  }

  const id = article.getAttribute('data-node-id') || filename.replace('.html', '');
  const type = article.getAttribute('data-node-type') || 'concept';
  const authority = parseFloat(article.getAttribute('data-execution-authority')) || 0.00;
  const memoryAuthority = parseFloat(article.getAttribute('data-memory-authority')) || 0.00;
  const relevance = parseFloat(article.getAttribute('data-weight-relevance')) || 0.50;
  const trustLabel = article.getAttribute('data-trust-label') || 'unverified';
  
  const titleEl = doc.querySelector('#node-title') || doc.querySelector('h1') || doc.querySelector('title');
  const title = titleEl ? titleEl.textContent.trim() : id;

  const memoryEl = doc.querySelector('#semantic-memory p') || doc.querySelector('#semantic-memory');
  const memory = memoryEl ? memoryEl.textContent.trim() : '';

  const relLinks = doc.querySelectorAll('a[data-relationship-type]');
  const relationships = [];
  relLinks.forEach(link => {
    relationships.push({
      href: link.getAttribute('href') || '',
      type: link.getAttribute('data-relationship-type') || 'references',
      weight: parseFloat(link.getAttribute('data-relationship-weight')) || 0.50,
      label: link.textContent.trim()
    });
  });

  let description = '';
  const dlTerms = doc.querySelectorAll('#node-identity dl dt');
  dlTerms.forEach(dt => {
    if (dt.textContent.trim() === 'Memory Description') {
      const dd = dt.nextElementSibling;
      if (dd) description = dd.textContent.trim();
    }
  });

  let tags = [];
  let author = 'agent';
  let created = new Date().toISOString();
  let version = '1.0.0';

  const script = doc.querySelector('#node-state');
  if (script) {
    try {
      const stateObj = JSON.parse(script.textContent);
      tags = stateObj.tags || [];
      if (!description && stateObj.description) {
        description = stateObj.description;
      }
      if (stateObj.metadata) {
        author = stateObj.metadata.author || author;
        created = stateObj.metadata.created || created;
        version = stateObj.metadata.version || version;
      }
    } catch (e) {
      console.warn('[NodeParser] failed to parse JSON state block:', e);
    }
  }

  return {
    id,
    type,
    authority: authority.toFixed(2),
    memoryAuthority: memoryAuthority.toFixed(2),
    relevance: relevance.toFixed(2),
    trustLabel,
    title,
    memory,
    relationships,
    tags,
    description,
    author,
    created,
    version,
    filename
  };
}

