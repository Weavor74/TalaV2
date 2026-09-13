/**
 * renderer/desktop_commands.js
 * ============================
 * The /desktop, /desktop-cpu and /desktop-stop chat commands, out of
 * renderer.js's sendMessage. A plain (non-module) script loaded via a classic
 * <script> tag in index.html before renderer.js — the thinking_blocks.js
 * pattern — so `handleDesktopChatCommand` is a shared global and this file is
 * testable in plain Node/Jest without loading all of renderer.js. Uses the
 * global `invoke` from helpers.js.
 *
 * Returns true when the text was a desktop command (handled here, chat should
 * stop), false when ordinary message handling should continue.
 *
 * @param {{appendMessage: Function}} chat  the AgentChat instance
 * @param {string} text                     the raw chat input
 * @returns {Promise<boolean>}
 */
async function handleDesktopChatCommand(chat, text) {
  const desktopCommand = text.match(/^\/desktop(-cpu)?\s+([\s\S]+)$/i);
  if (desktopCommand) {
    const expectation = desktopCommand[2].match(/\s+--expect\s+"([^"]+)"\s*$/);
    const task = expectation ? desktopCommand[2].slice(0, expectation.index) : desktopCommand[2];
    chat.appendMessage('agent', 'Tala', 'Running the local desktop agent. Use /desktop-stop to stop it.');
    let result;
    try {
      result = await invoke('python:rpc', {method: 'desktop.run', params: {task, cpu_only: Boolean(desktopCommand[1]), success_text: expectation?.[1] || '', _timeoutMs: 1800000}});
    } catch (error) {
      chat.appendMessage('agent', 'Tala', `Desktop task failed: ${error.message || error}`);
      return true;
    }
    const output = result?.output;
    chat.appendMessage('agent', 'Tala', result?.success
      ? `Desktop agent reports completion from visual evidence: ${output?.evidence || 'No evidence returned.'}`
      : `Desktop task failed: ${result?.error || output?.error || 'No result returned.'}`);
    return true;
  }

  if (text.trim().toLowerCase() === '/desktop-stop') {
    const result = await invoke('python:rpc', {method: 'desktop.stop', params: {}}).catch((error) => ({error: error?.message || String(error)}));
    chat.appendMessage('agent', 'Tala', result?.success ? 'Desktop stop requested.' : `Could not request desktop stop: ${result?.error || 'no response from the harness'}`);
    return true;
  }

  return false;
}
