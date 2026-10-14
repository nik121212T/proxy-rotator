const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {join} = require('node:path');

test('priority checkbox selects round robin and each mode updates its explanation', () => {
  const modes = ['weighted', 'round_robin', 'random', 'sticky', 'timed'];
  const select = new EventTarget();
  select.value = 'weighted';
  Object.defineProperty(select, 'selectedOptions', {get() {
    return [{dataset: {description: `Description ${select.value}`}}];
  }});
  const checkbox = new EventTarget();
  checkbox.checked = false;
  const help = {textContent: ''};
  const nodes = {'strategy': select, 'rotation-pinned-only': checkbox, 'strategy-help': help};
  const document = Object.assign(new EventTarget(), {
    body: new EventTarget(),
    getElementById: id => nodes[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
  });
  runInNewContext(readFileSync(join(__dirname, '../app/ui/static/app.js'), 'utf8'), {
    document, window: new EventTarget(), htmx: {defineExtension() {}}, Event,
  });
  assert.equal(help.textContent, 'Description weighted');
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event('change'));
  assert.equal(select.value, 'round_robin');
  assert.equal(help.textContent, 'Description round_robin');
  for (const mode of modes) {
    select.value = mode;
    select.dispatchEvent(new Event('change'));
    assert.equal(help.textContent, `Description ${mode}`);
  }
  checkbox.checked = false;
  checkbox.dispatchEvent(new Event('change'));
  assert.equal(select.value, 'timed');
});
