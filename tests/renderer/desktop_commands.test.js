const fs = require('fs');
const vm = require('vm');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../../renderer/renderer.js'), 'utf8');
const start = source.indexOf('    const desktopCommand = text.match(');
const code = source.slice(start, source.indexOf('    // Handle Slash Commands', start));
async function run(text, reply) {
  const invoke = jest.fn().mockResolvedValue(reply);
  const chat = {appendMessage: jest.fn()};
  await vm.runInNewContext('(async function(text){' + code + '}).call(chat,text)', {invoke, chat, text});
  return {invoke, chat};
}
test('desktop CPU command preserves the task and explicit final text', async () => {
  const {invoke} = await run('/desktop-cpu Fill the field and verify --expect "PASS: 482"', {success:true,output:{evidence:'PASS: 482'}});
  expect(invoke).toHaveBeenCalledWith('python:rpc', {method:'desktop.run',params:{task:'Fill the field and verify',cpu_only:true,success_text:'PASS: 482',_timeoutMs:1800000}});
});
test('stop bypasses inference', async () => {
  const {invoke} = await run('/desktop-stop', {success:true});
  expect(invoke).toHaveBeenCalledWith('python:rpc', {method:'desktop.stop',params:{}});
});
test('failed desktop runs remain failures in chat', async () => {
  const {chat} = await run('/desktop test', {success:false,error:'Control disappeared'});
  expect(chat.appendMessage.mock.calls.at(-1)[2]).toBe('Desktop task failed: Control disappeared');
});
test('failed stop request is reported, not thrown', async () => {
  const invoke = jest.fn().mockRejectedValue(new Error('Python harness WebSocket is not connected.'));
  const chat = {appendMessage: jest.fn()};
  await vm.runInNewContext('(async function(text){' + code + '}).call(chat,text)', {invoke, chat, text: '/desktop-stop'});
  expect(chat.appendMessage.mock.calls.at(-1)[2]).toBe('Could not request desktop stop: Python harness WebSocket is not connected.');
});
