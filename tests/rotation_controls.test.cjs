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
  let formExtension;
  runInNewContext(readFileSync(join(__dirname, '../app/ui/static/app.js'), 'utf8'), {
    document, window: new EventTarget(),
    htmx: {defineExtension(name, extension) { formExtension = extension; }}, Event,
    FormData: class { *[Symbol.iterator]() { yield ['interval_minutes', '5']; } },
  });
  assert.equal(help.textContent, 'Description weighted');
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event('change'));
  assert.equal(select.value, 'round_robin');
  assert.equal(help.textContent, 'Description round_robin');
  assert.equal(select.disabled, true);
  checkbox.checked = false;
  checkbox.dispatchEvent(new Event('change'));
  assert.equal(select.disabled, false);
  for (const mode of modes) {
    select.value = mode;
    select.dispatchEvent(new Event('change'));
    assert.equal(help.textContent, `Description ${mode}`);
  }
  checkbox.checked = false;
  checkbox.dispatchEvent(new Event('change'));
  assert.equal(select.value, 'timed');
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event('change'));
  const payload = JSON.parse(formExtension.encodeParameters(null, null, {
    id: 'strategy-form',
    querySelector: query => query.includes('pinned_only') ? checkbox : select,
  }));
  assert.equal(payload.strategy, 'round_robin');
  assert.equal(payload.pinned_only, true);
  assert.equal(payload.interval_minutes, 5);
});
