/**
 * tests/renderer/thinking_blocks.test.js
 * ========================================
 * Regression coverage for renderer/thinking_blocks.js.
 *
 * Caught live, in the actual main chat bubble (not a debug/diagnostics
 * view): a real response containing two separate, fully-closed
 * <thinking>...</thinking> blocks -- one from an initial generation pass,
 * one from a harness-auto-continued follow-up after a mid-generation tool
 * call -- showed the *second* block's raw contents directly in the visible
 * chat bubble instead of being hidden in the collapsible "Thought Process"
 * section. Root cause: the parsing regex had no /g flag, so
 * content.replace() only ever removed the first match.
 */
const { parseThinkingBlocks } = require('../../renderer/thinking_blocks.js');

describe('parseThinkingBlocks', () => {
  test('strips a single closed thinking block and returns its text separately', () => {
    const { finalContent, thinkingText } = parseThinkingBlocks(
      '<thinking>reasoning here</thinking>The actual reply.'
    );
    expect(finalContent).toBe('The actual reply.');
    expect(thinkingText).toBe('reasoning here');
  });

  test('strips ALL closed thinking blocks, not just the first (the confirmed live regression)', () => {
    const content =
      '<thinking>first pass reasoning</thinking>' +
      '<thinking>second pass reasoning after a tool call</thinking>' +
      'It seems the search did not find any matches.';

    const { finalContent, thinkingText } = parseThinkingBlocks(content);

    // The bug: finalContent used to still contain the second <thinking>
    // block's raw tags and text verbatim.
    expect(finalContent).not.toContain('<thinking>');
    expect(finalContent).not.toContain('second pass reasoning');
    expect(finalContent).toBe('It seems the search did not find any matches.');

    expect(thinkingText).toContain('first pass reasoning');
    expect(thinkingText).toContain('second pass reasoning after a tool call');
  });

  test('treats an unwrapped-but-later-unclosed trailing block as thinking too', () => {
    const content =
      '<thinking>closed block</thinking>' +
      'Some real reply text. ' +
      '<thinking>this one never closes and drifts off';

    const { finalContent, thinkingText } = parseThinkingBlocks(content);

    expect(finalContent).toBe('Some real reply text.');
    expect(thinkingText).toContain('closed block');
    expect(thinkingText).toContain('this one never closes and drifts off');
  });

  test('content with no thinking tags at all passes through unchanged', () => {
    const { finalContent, thinkingText } = parseThinkingBlocks('Just a normal reply.');
    expect(finalContent).toBe('Just a normal reply.');
    expect(thinkingText).toBe('');
  });

  test('a lone unclosed thinking block with no prior real content', () => {
    const { finalContent, thinkingText } = parseThinkingBlocks(
      '<thinking>never gets around to answering'
    );
    expect(finalContent).toBe('');
    expect(thinkingText).toBe('never gets around to answering');
  });
});
