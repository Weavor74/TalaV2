/**
 * renderer/thinking_blocks.js
 * ============================
 * Parses <thinking>...</thinking> block(s) out of raw model output for the
 * chat UI. Deliberately a small, standalone, DOM-free, plain (non-module)
 * script -- loaded via a classic <script> tag in index.html *before*
 * renderer.js (which is `type="module"`), so `parseThinkingBlocks` becomes
 * a real global both scripts share, and so this one pure function is
 * testable in plain Node/Jest via require() without needing to load all of
 * renderer.js (which has top-level DOM/Electron-IPC calls and cannot be
 * require()'d outside a browser context) or configure an ESM test pipeline
 * just for this file.
 *
 * A single displayed chat message can carry content from more than one
 * generation pass -- e.g. the harness's auto-continue/multi-turn tool
 * execution concatenates a follow-up generation onto the same message
 * after a tool call -- and each pass can wrap its own deliberation in its
 * own <thinking> block.
 *
 * Regression this fixes: the previous version matched only the *first*
 * <thinking>...</thinking> block (no /g flag on the regex), so a second,
 * fully-closed block later in the same message leaked straight into the
 * visible chat bubble as raw text instead of being hidden in the
 * collapsible "Thought Process" section. Confirmed live: a real chat
 * response with two closed <thinking> blocks showed the second one's raw
 * contents directly in the main chat bubble.
 */
function parseThinkingBlocks(content) {
  let finalContent = content;
  const thinkingParts = [];

  const thinkingRegex = /<thinking>([\s\S]*?)<\/thinking>/gi;
  finalContent = content.replace(thinkingRegex, (_match, inner) => {
    thinkingParts.push(inner.trim());
    return "";
  }).trim();

  // Small/local fallback models sometimes open a <thinking> block and never
  // close it (they drift off mid-reasoning). Without a matching close tag
  // there's nothing to safely resume "real" reply text from, so treat
  // everything from the open tag onward as thinking rather than dumping the
  // raw, unclosed tag + reasoning into the chat as if it were the actual
  // response. Checked against whatever remains after stripping closed
  // blocks above, so an earlier closed block can't mask a genuinely
  // unclosed one later in the same message.
  const openIdx = finalContent.search(/<thinking>/i);
  if (openIdx !== -1) {
    thinkingParts.push(finalContent.slice(openIdx + "<thinking>".length).trim());
    finalContent = finalContent.slice(0, openIdx).trim();
  }

  return {
    finalContent,
    thinkingText: thinkingParts.filter(Boolean).join("\n\n"),
  };
}

// Classic-script global for the Electron renderer (browser context: module
// is undefined, so this branch is skipped and the top-level `function`
// declaration above is what attaches `parseThinkingBlocks` to `window`).
// CommonJS export for Jest/Node (test context).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { parseThinkingBlocks };
}
