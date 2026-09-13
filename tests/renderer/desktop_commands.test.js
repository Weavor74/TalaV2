const fs = require('fs');
const vm = require('vm');
const path = require('path');
// The command handler lives in its own classic script now (the
// thinking_blocks.js pattern), so the whole file runs in the VM — no more
// slicing renderer.js by string markers.
const code = fs.readFileSync(path.join(__dirname, '../../renderer/desktop_commands.js'), 'utf8');
async function run(text, reply) {
  const invoke = jest.fn().mockResolvedValue(reply);
  const chat = {appendMessage: jest.fn()};
  const handled = await vm.runInNewContext(code + '\nhandleDesktopChatCommand(chat, text)', {invoke, chat, text});
  return {invoke, chat, handled};
}
test('desktop CPU command preserves the task and explicit final text', async () => {
  const {invoke, handled} = await run('/desktop-cpu Fill the field and verify --expect "PASS: 482"', {success:true,output:{evidence:'PASS: 482'}});
  expect(handled).toBe(true);
  expect(invoke).toHaveBeenCalledWith('python:rpc', {method:'desktop.run',params:{task:'Fill the field and verify',cpu_only:true,success_text:'PASS: 482',_timeoutMs:1800000}});
});
test('stop bypasses inference', async () => {
  const {invoke, handled} = await run('/desktop-stop', {success:true});
  expect(handled).toBe(true);
  expect(invoke).toHaveBeenCalledWith('python:rpc', {method:'desktop.stop',params:{}});
});
test('failed desktop runs remain failures in chat', async () => {
  const {chat} = await run('/desktop test', {success:false,error:'Control disappeared'});
  expect(chat.appendMessage.mock.calls.at(-1)[2]).toBe('Desktop task failed: Control disappeared');
});
test('failed stop request is reported, not thrown', async () => {
  const invoke = jest.fn().mockRejectedValue(new Error('Python harness WebSocket is not connected.'));
  const chat = {appendMessage: jest.fn()};
  await vm.runInNewContext(code + '\nhandleDesktopChatCommand(chat, text)', {invoke, chat, text: '/desktop-stop'});
  expect(chat.appendMessage.mock.calls.at(-1)[2]).toBe('Could not request desktop stop: Python harness WebSocket is not connected.');
});
test('ordinary chat text is not handled here', async () => {
  const {invoke, handled} = await run('tell me about the desktop', {});
  expect(handled).toBe(false);
  expect(invoke).not.toHaveBeenCalled();
});
