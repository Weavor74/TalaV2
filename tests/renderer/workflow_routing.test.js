const fs = require('fs');
const vm = require('vm');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../../renderer/renderer.js'), 'utf8');
const routing = source.slice(source.indexOf('    const lowerText = text.toLowerCase();'), source.indexOf('    if (isActionPrompt) {'));
function classify(text) {
  return vm.runInNewContext(routing + '\n({isActionPrompt, isDirectToolIntent})', {text});
}
test('a workflow request stays in chat even when it says return', () => {
  expect(classify('Run my saved workflow QA with inputs numbers=[11,4,-3] and return the result').isActionPrompt).toBe(false);
});
test('return does not accidentally match turn', () => {
  expect(classify('Please return the answer').isActionPrompt).toBe(false);
  expect(classify('Create a new component').isActionPrompt).toBe(true);
});

test('desktop task stays in chat instead of entering the code planner', () => {
  expect(classify('Use desktop_run to create a note in Notepad').isActionPrompt).toBe(false);
  expect(classify('Control my computer and change the window').isActionPrompt).toBe(false);
});
