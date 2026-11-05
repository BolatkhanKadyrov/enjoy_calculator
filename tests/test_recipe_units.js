/* Real unit controller + exact conversion module. A small event-bubbling DOM
 * fixture tests state/notifications only, not native browser layout or dialogs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const units = require('../tseh/static/tseh/units.js');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../tseh/static/tseh/recipe-units.js'), 'utf8'), context);

class Event {
  constructor(type, {bubbles = false} = {}) { this.type = type; this.bubbles = bubbles; this.stopped = false; }
  stopPropagation() { this.stopped = true; }
}
class Element {
  constructor(id, tag = 'input', value = '') {
    this.id = id; this.tagName = tag.toUpperCase(); this.value = value;
    this.dataset = {}; this.listeners = {}; this.options = [];
    this.parentNode = null; this.validityMessage = ''; this.validityReports = 0;
  }
  get selectedOptions() { return this.options.filter(option => option.value === this.value); }
  addEventListener(type, handler, options = {}) { (this.listeners[type] ||= []).push({handler, once: options.once}); }
  dispatchEvent(event) {
    event.target ||= this;
    for (const entry of [...(this.listeners[event.type] || [])]) {
      entry.handler(event);
      if (entry.once) this.listeners[event.type].splice(this.listeners[event.type].indexOf(entry), 1);
    }
    if (event.bubbles && !event.stopped && this.parentNode) this.parentNode.dispatchEvent(event);
  }
  setCustomValidity(message) { this.validityMessage = message; }
  reportValidity() { this.validityReports++; }
}
const choices = [
  ['', undefined], ['ingredient:flour', 'g'], ['ingredient:other-flour', 'g'],
  ['ingredient:water', 'ml'], ['packaging:box', 'pc'], ['preparation:dough', 'g'],
  ['ingredient:missing-metadata', undefined],
];
function fixture({quantity = '', unit = 'kg', component = '', basis = unit, includeBasis = true} = {}) {
  const elements = new Map();
  const form = new Element('recipe-form', 'form'); elements.set(form.id, form);
  const notifications = [];
  const confirmCalls = [];
  let confirmResult = true;
  function add(element) { element.parentNode = form; elements.set(element.id, element); return element; }
  function addRow(index, options = {}) {
    const prefix = `id_form-${index}`;
    const selectedUnit = options.unit ?? unit;
    const row = {
      component: add(new Element(`${prefix}-component`, 'select', options.component ?? component)),
      quantity: add(new Element(`${prefix}-quantity`, 'input', options.quantity ?? quantity)),
      unit: add(new Element(`${prefix}-unit`, 'select', selectedUnit)),
      basis: includeBasis ? add(new Element(`${prefix}-quantity_unit`, 'input', options.basis ?? basis)) : null,
    };
    row.component.options = choices.map(([value, unit]) => ({value, dataset: unit ? {unit} : {}}));
    return row;
  }
  const row = addRow(0);
  const state = target => ({component: target.component.value, quantity: target.quantity.value,
    unit: target.unit.value, basis: target.basis?.value});
  for (const type of ['input', 'change']) form.addEventListener(type, event => {
    notifications.push({event: event.type, ...state(row)});
  });
  const doc = {
    getElementById: id => elements.get(id) || null,
    querySelectorAll(selector) {
      if (selector === 'select[id$="-unit"], select#id_output_unit') {
        return [...elements.values()].filter(e => e.tagName === 'SELECT' && (e.id.endsWith('-unit') || e.id === 'id_output_unit'));
      }
      if (selector === 'select[id$="-component"]') {
        return [...elements.values()].filter(e => e.tagName === 'SELECT' && e.id.endsWith('-component'));
      }
      throw Error(`Unsupported fixture selector: ${selector}`);
    },
  };
  const win = {TsehUnits: units, Event, confirm(message) { confirmCalls.push(message); return confirmResult; }};
  const bind = () => context.TsehRecipeUnits.bind(doc, win);
  const select = (element, value) => {
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
  };
  return {row, form, add, addRow, state, bind, select, notifications, confirmCalls,
    confirm: value => { confirmResult = value; }};
}

let checks = 0;
function check(name, run) { run(); checks++; console.log(`OK ${name}`); }

check('initial and repeated binding preserve persisted/manual/draft kg values without notification', () => {
  const f = fixture({component: 'ingredient:flour', quantity: ' 0,004000000001 ', unit: 'kg'});
  const before = f.state(f.row); f.bind(); f.bind();
  assert.deepEqual(f.state(f.row), before);
  assert.equal(f.notifications.length, 0);
  assert.equal(f.confirmCalls.length, 0);
});

for (const [component, unit] of [['ingredient:flour', 'g'], ['ingredient:water', 'ml'], ['packaging:box', 'pc']]) {
  check(`empty component selection suggests ${unit} with one completed notification`, () => {
    const f = fixture(); f.bind(); f.select(f.row.component, component);
    assert.deepEqual(f.state(f.row), {component, quantity: '', unit, basis: unit});
    assert.deepEqual(f.notifications, [{event: 'input', ...f.state(f.row)}]);
    assert.equal(f.confirmCalls.length, 0);
  });
}

for (const [quantity, from, component, to, expected] of [
  ['0,004000000001', 'kg', 'ingredient:flour', 'g', '4,000000001'],
  ['999999999999.123456789012', 'kg', 'preparation:dough', 'g', '999999999999123,456789012'],
  ['0.000000000000001', 'l', 'ingredient:water', 'ml', '0,000000000001'],
  [',5', 'l', 'ingredient:water', 'ml', '500'],
]) {
  check(`automatic ${from} to ${to} preserves exact quantity ${quantity}`, () => {
    const f = fixture({quantity, unit: from}); f.bind(); f.select(f.row.component, component);
    assert.deepEqual(f.state(f.row), {component, quantity: expected, unit: to, basis: to});
    assert.deepEqual(f.notifications, [{event: 'input', ...f.state(f.row)}]);
    assert.equal(f.confirmCalls.length, 0);
  });
}

check('invalid same-dimensional quantity rolls back automatic component selection atomically', () => {
  const f = fixture({component: 'ingredient:missing-metadata', quantity: 'not yet numeric', unit: 'kg'});
  f.bind(); const before = f.state(f.row); f.select(f.row.component, 'ingredient:flour');
  assert.deepEqual(f.state(f.row), before);
  assert.equal(f.notifications.length, 0);
  assert.equal(f.row.quantity.validityReports, 1);
  assert.ok(f.row.quantity.validityMessage);
  f.row.quantity.value = ',5'; f.row.quantity.dispatchEvent(new Event('input', {bubbles: true}));
  assert.equal(f.row.quantity.validityMessage, '');
  f.notifications.length = 0;
  f.select(f.row.component, 'ingredient:flour');
  assert.equal(f.row.quantity.value, '500');
  assert.equal(f.notifications.length, 1);
});

check('manual invalid quantity conversion restores previous unit and basis without notifications', () => {
  const f = fixture({quantity: 'bad', unit: 'kg'}); f.bind();
  f.select(f.row.unit, 'g');
  assert.equal(f.row.unit.value, 'kg');
  assert.equal(f.row.basis.value, 'kg');
  assert.equal(f.row.quantity.value, 'bad');
  assert.equal(f.notifications.length, 0);
});

check('cancelled dimensional change preserves component, unit, quantity and basis', () => {
  const f = fixture({component: 'ingredient:flour', quantity: ' 2,500 ', unit: 'kg'});
  f.bind(); f.confirm(false); const before = f.state(f.row);
  f.select(f.row.component, 'ingredient:water');
  assert.deepEqual(f.state(f.row), before);
  assert.equal(f.notifications.length, 0);
  assert.equal(f.confirmCalls.length, 1);
  assert.match(f.confirmCalls[0], /2,500/);
  // The cancelled attempt must not change either controller's previous-value cache.
  f.select(f.row.component, 'ingredient:other-flour');
  assert.equal(f.row.quantity.value, '2500');
  assert.equal(f.row.unit.value, 'g');
  assert.equal(f.notifications.length, 1);
});

check('accepted dimensional change clears quantity before one final notification', () => {
  const f = fixture({component: 'ingredient:flour', quantity: '3', unit: 'kg'});
  f.bind(); f.confirm(true); f.select(f.row.component, 'packaging:box');
  assert.deepEqual(f.state(f.row), {component: 'packaging:box', quantity: '', unit: 'pc', basis: 'pc'});
  assert.deepEqual(f.notifications, [{event: 'input', ...f.state(f.row)}]);
  assert.equal(f.confirmCalls.length, 1);
});

check('automatic choice updates previous-unit cache for the next manual conversion', () => {
  const f = fixture({quantity: '0,004000000001', unit: 'kg'}); f.bind();
  f.select(f.row.component, 'ingredient:flour');
  assert.equal(f.row.quantity.value, '4,000000001');
  f.select(f.row.unit, 'kg');
  assert.equal(f.row.quantity.value, '0,004000000001');
  assert.equal(f.row.basis.value, 'kg');
  assert.deepEqual(f.notifications.map(event => event.quantity), ['4,000000001', '0,004000000001']);
});

check('dynamic new row binds once and notifies observers only after complete mutation', () => {
  const f = fixture(); f.bind();
  const added = f.addRow(1, {quantity: '0.2', unit: 'l', basis: 'l'});
  const observed = [];
  f.form.addEventListener('input', () => observed.push(f.state(added)));
  f.bind(); f.bind(); f.select(added.component, 'ingredient:water');
  assert.deepEqual(observed, [{component: 'ingredient:water', quantity: '200', unit: 'ml', basis: 'ml'}]);
  assert.equal(f.notifications.length, 1);
});

check('preparation output unit retains existing exact conversion behavior', () => {
  const f = fixture();
  const quantity = f.add(new Element('id_output_quantity', 'input', '.000000000001'));
  const unit = f.add(new Element('id_output_unit', 'select', 'g'));
  const basis = f.add(new Element('id_output_input_unit', 'input', 'g'));
  const observed = [];
  f.form.addEventListener('input', () => observed.push([quantity.value, unit.value, basis.value]));
  f.bind(); f.select(unit, 'kg');
  assert.deepEqual(observed, [['0,000000000000001', 'kg', 'kg']]);
});

for (const target of ['', 'ingredient:missing-metadata']) {
  check(`choice ${target || '(blank)'} without metadata leaves quantity/unit/basis intact`, () => {
    const f = fixture({component: 'ingredient:flour', quantity: ' 1,25 ', unit: 'kg'}); f.bind();
    f.select(f.row.component, target);
    assert.deepEqual(f.state(f.row), {component: target, quantity: ' 1,25 ', unit: 'kg', basis: 'kg'});
    assert.equal(f.notifications.length, 1);
    assert.equal(f.confirmCalls.length, 0);
  });
}

console.log(`${checks} recipe unit controller checks passed (fake DOM; no browser validation)`);
