/**
 * Request-side limits for the chain node's HTTP routes: a per-IP ceiling on
 * anonymous transaction submission and a capped body reader.
 */
'use strict';

// Sliding-window per-IP limiter for non-admin transaction submission. The
// mempool mines instantly, so every accepted tx costs disk and a block; an
// internet-reachable node needs a ceiling per stranger.
const txSubmissionHits = new Map();
const TX_RATE_LIMIT = 30;
const TX_RATE_WINDOW_MS = 60 * 1000;

function txRateLimited(ip) {
  const now = Date.now();
  const hits = (txSubmissionHits.get(ip) || []).filter(t => now - t < TX_RATE_WINDOW_MS);
  if (hits.length >= TX_RATE_LIMIT) {
    txSubmissionHits.set(ip, hits);
    return true;
  }
  hits.push(now);
  txSubmissionHits.set(ip, hits);
  // Keep the map from growing one entry per strange IP forever.
  if (txSubmissionHits.size > 10000) {
    for (const [key, times] of txSubmissionHits) {
      if (!times.some(t => now - t < TX_RATE_WINDOW_MS)) txSubmissionHits.delete(key);
    }
  }
  return false;
}

// Default cap is 1MB. The old blanket 100MB limit was a memory-DoS lever on
// an internet-reachable node: anonymous callers could hold open concurrent
// 100MB bodies. Routes that legitimately carry more (chain reconcile, which
// ships a full chain and is admin-gated BEFORE the body is read) pass their
// own cap.
function readRequestBody(req, maxBytes = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > maxBytes) {
        reject(new Error('Request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

module.exports = { txRateLimited, readRequestBody };
