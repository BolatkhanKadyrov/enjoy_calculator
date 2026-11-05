/* Observable controller behavior in a small DOM fixture; not browser/layout coverage. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({});
for (const file of ['recipe-units.js', 'component-picker.js']) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../tseh/static/tseh', file), 'utf8'), context);
}
class Event {
  constructor(type, options = {}) { this.type = type; Object.assign(this, options); }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
}
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = []; this.dataset = {};
    this.attributes = {}; this.listeners = {}; this.value = ''; this.textContent = '';
    this.className = ''; this.id = ''; this.required = false; this.disabled = false;
    this.classList = {toggle: (name, enabled) => {
      const tokens = new Set(this.className.split(' ').filter(Boolean));
      enabled ? tokens.add(name) : tokens.delete(name); this.className = [...tokens].join(' ');
    }};
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
    child.parentNode = this; this.children.push(child); return child;
  }
  insertBefore(child, before) { child.parentNode = this; this.children.splice(this.children.indexOf(before), 0, child); }
  replaceChildren() { this.children.forEach(c => { c.parentNode = null; }); this.children = []; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  get options() { return descendants(this).filter(e => e.tagName === 'OPTION'); }
  get selectedOptions() { return this.options.filter(e => e.value === this.value); }
  get labels() { return descendants(this.doc.root).filter(e => e.tagName === 'LABEL' && e.htmlFor === this.id); }
  addEventListener(type, handler, options = {}) { (this.listeners[type] ||= []).push({handler, once: options.once}); }
  dispatchEvent(event) {
    event.target ||= this;
    for (const listener of [...(this.listeners[event.type] || [])]) {
      listener.handler(event);
      if (listener.once) this.listeners[event.type].splice(this.listeners[event.type].indexOf(listener), 1);
    }
    if (event.bubbles && !event.stopped && this.parentNode) this.parentNode.dispatchEvent(event);
    return !event.defaultPrevented;
  }
  setCustomValidity(text) { this.validityMessage = text; }
  reportValidity() {}
  scrollIntoView() {}
  select() { this.textSelected = true; }
}
function descendants(node) { return node.children.flatMap(c => [c, ...descendants(c)]); }
function fixture({component = 'flour', quantity = '0,2500', unit = 'kg'} = {}) {
  const doc = {
    createElement(tag) { return new Element(tag, this); },
    getElementById(id) { return [this.root, ...descendants(this.root)].find(e => e.id === id); },
    querySelectorAll(selector) {
      const selects = descendants(this.root).filter(e => e.tagName === 'SELECT');
      if (selector === 'select[id$="-component"]') return selects.filter(e => e.id.endsWith('-component'));
      if (selector === 'select[id$="-unit"], select#id_output_unit') return selects.filter(e => e.id.endsWith('-unit') || e.id === 'id_output_unit');
      throw Error(selector);
    },
  };
  doc.root = doc.createElement('form'); doc.root.id = 'recipe-form';
  const notifications = [], confirmations = [];
  let confirmResult = true;
  const win = {Event, TsehUnits: require('../tseh/static/tseh/units.js'), confirm(message) {
    confirmations.push(message);
    assert.equal(doc.getElementById('id_form-0-component-options').hidden, true);
    return confirmResult;
  }};
  function add(tag, parent, id = '') { const e = doc.createElement(tag); e.id = id; parent.appendChild(e); return e; }
  function addRow(index) {
    const field = add('div', doc.root); field.className = 'field';
    const label = add('label', field); label.htmlFor = `id_form-${index}-component`; label.textContent = 'Компонент';
    const select = add('select', field, label.htmlFor); select.value = component; select.required = true; select.name = `form-${index}-component`;
    select.setAttribute('aria-describedby', 'component-help');
    function option(parent, value, text, unit) {
      const e = add('option', parent); e.value = value; e.textContent = text; e.label = text;
      if (unit) e.dataset.unit = unit; return e;
    }
    option(select, '', 'Выберите компонент');
    const ingredients = add('optgroup', select); ingredients.label = 'Ингредиенты';
    option(ingredients, 'flour', 'Мука пшеничная', 'g'); option(ingredients, 'milk', 'Молоко', 'ml');
    const preparations = add('optgroup', select); preparations.label = 'Заготовки';
    option(preparations, 'dough', 'Тесто с мукой', 'g');
    option(preparations, 'missing', '<img src=x onerror=alert(1)> недоступен', 'g');
    const q = add('input', doc.root, `id_form-${index}-quantity`); q.value = quantity;
    const u = add('select', doc.root, `id_form-${index}-unit`); u.value = unit;
    const basis = add('input', doc.root, `id_form-${index}-quantity_unit`); basis.value = unit;
    return {field, label, select, q, u, basis};
  }
  const row = addRow(0);
  const state = () => [row.select.value, row.q.value, row.u.value, row.basis.value];
  for (const type of ['input', 'change']) doc.root.addEventListener(type, () => notifications.push(state()));
  const bind = () => { context.TsehRecipeUnits.bind(doc, win); context.TsehComponentPicker.bind(doc, win); };
  bind();
  const input = doc.getElementById(row.select.id + '-search');
  const popup = doc.getElementById(row.select.id + '-options');
  const options = () => descendants(popup).filter(e => e.getAttribute('role') === 'option');
  function fire(target, type, properties = {}) { const e = new Event(type, {bubbles: true, ...properties}); target.dispatchEvent(e); return e; }
  const type = text => { input.value = text; fire(input, 'input'); };
  const key = key => fire(input, 'keydown', {key});
  return {doc, win, row, input, popup, options, state, notifications, confirmations, bind, addRow, fire, type, key,
    confirm: value => { confirmResult = value; }};
}
let checks = 0;
function check(name, run) { run(); checks++; console.log('OK ' + name); }
check('binding preserves native submission, metadata, order, selected draft text and labels', () => {
  const f = fixture(); const options = f.row.select.options.slice(); f.bind();
  assert.deepEqual(f.state(), ['flour', '0,2500', 'kg', 'kg']);
  assert.deepEqual(f.row.select.options, options); assert.equal(f.notifications.length, 0);
  assert.equal(f.row.select.name, 'form-0-component'); assert.equal(f.row.select.hidden, true);
  assert.equal(f.row.select.required, false); assert.equal(f.input.required, true);
  assert.equal(f.input.name, undefined); assert.equal(f.input.value, 'Мука пшеничная');
  assert.equal(f.row.label.htmlFor, f.input.id); assert.equal(f.input.getAttribute('aria-describedby'), 'component-help');
  assert.equal(f.row.field.children.filter(e => e.className === 'component-picker').length, 1);
});
check('Russian substring filtering preserves groups and never emits dirty notifications', () => {
  const f = fixture(); f.fire(f.input, 'focus'); assert.equal(f.options().length, 4);
  assert.equal(f.input.textSelected, true);
  f.type('МУК'); assert.deepEqual(f.options().map(e => e.textContent), ['Мука пшеничная', 'Тесто с мукой']);
  assert.deepEqual(f.popup.children.map(e => e.getAttribute('aria-label')), ['Ингредиенты', 'Заготовки']);
  f.fire(f.input, 'change'); assert.equal(f.notifications.length, 0); assert.equal(f.row.select.value, 'flour');
  f.type('нет такого'); assert.equal(f.options().length, 0);
  assert.equal(f.popup.parentNode.children.at(-1).textContent, 'Компоненты не найдены.');
  assert.equal(f.key('Enter').defaultPrevented, true); assert.equal(f.row.select.value, 'flour');
});
check('opening all does not implicitly select; Escape, Tab and blur discard query', () => {
  const f = fixture(); f.fire(f.input, 'focus'); assert.equal(f.key('Enter').defaultPrevented, true);
  assert.equal(f.row.select.value, 'flour'); f.key('Escape'); assert.equal(f.input.value, 'Мука пшеничная');
  assert.equal(f.input.getAttribute('aria-expanded'), 'false');
  f.type('query'); assert.equal(f.key('Tab').defaultPrevented, undefined); assert.equal(f.input.value, 'Мука пшеничная');
  f.type('query'); f.fire(f.input, 'blur'); assert.equal(f.input.value, 'Мука пшеничная');
  assert.equal(f.notifications.length, 0);
});
check('keyboard selection converts exact quantity then emits one completed notification', () => {
  const f = fixture(); f.type('Тесто'); f.key('ArrowDown');
  assert.equal(f.input.getAttribute('aria-activedescendant'), f.options()[0].id);
  f.key('Enter'); assert.deepEqual(f.state(), ['dough', '250', 'g', 'g']);
  assert.deepEqual(f.notifications, [['dough', '250', 'g', 'g']]); assert.equal(f.input.value, 'Тесто с мукой');
});
check('cancel incompatible selection keeps component quantity unit basis and all other rows', () => {
  const f = fixture(); const second = f.addRow(1); f.bind(); f.confirm(false);
  f.type('Молоко'); const button = f.options()[0];
  assert.equal(f.fire(button, 'pointerdown').defaultPrevented, true); f.fire(button, 'click');
  assert.deepEqual(f.state(), ['flour', '0,2500', 'kg', 'kg']); assert.equal(f.input.value, 'Мука пшеничная');
  assert.equal(f.notifications.length, 0); assert.equal(f.confirmations.length, 1);
  assert.equal(second.q.value, '0,2500'); assert.equal(second.select.value, 'flour');
});
check('accept incompatible selection clears quantity and notifies only final state', () => {
  const f = fixture(); f.type('Молоко'); f.key('End'); f.key('Enter');
  assert.deepEqual(f.state(), ['milk', '', 'ml', 'ml']); assert.deepEqual(f.notifications, [['milk', '', 'ml', 'ml']]);
  assert.equal(f.input.value, 'Молоко');
});
check('Home End arrows expose active options; closed Home retains text caret behavior', () => {
  const f = fixture(); f.fire(f.input, 'focus'); f.key('End');
  assert.equal(f.input.getAttribute('aria-activedescendant'), f.options().at(-1).id);
  f.key('ArrowUp'); assert.equal(f.input.getAttribute('aria-activedescendant'), f.options().at(-2).id);
  f.key('Home'); assert.equal(f.input.getAttribute('aria-activedescendant'), f.options()[0].id);
  f.key('Escape'); assert.equal(f.key('Home').defaultPrevented, undefined);
});
check('empty selection has an empty search placeholder; trimmed query activates result without committing', () => {
  const f = fixture({component: '', quantity: ''});
  assert.equal(f.input.value, ''); assert.equal(f.input.placeholder, 'Найти компонент…');
  assert.equal(f.input.validityMessage, 'Выберите компонент из списка.');
  f.type('  МОЛОКО  '); assert.equal(f.options().length, 1); assert.equal(f.row.select.value, '');
  assert.equal(f.notifications.length, 0); f.key('Enter'); assert.equal(f.row.select.value, 'milk');
  assert.equal(f.input.validityMessage, ''); assert.equal(f.popup.parentNode.hidden, true);
});
check('unavailable labels render as plain text; new rows bind once; external changes sync', () => {
  const f = fixture({component: 'missing'}); assert.equal(f.input.value, '<img src=x onerror=alert(1)> недоступен');
  f.type('<img'); assert.equal(f.options()[0].children.length, 0);
  assert.equal(f.options()[0].textContent, '<img src=x onerror=alert(1)> недоступен');
  const row = f.addRow(1); f.bind(); f.bind();
  assert.ok(f.doc.getElementById(row.select.id + '-search'));
  f.row.select.value = 'dough'; f.fire(f.row.select, 'change'); assert.equal(f.input.value, 'Тесто с мукой');
  assert.equal(f.notifications.length, 1);
});
check('invalid exact conversion restores visible selection without notifying draft', () => {
  const f = fixture({quantity: 'unfinished'}); f.type('Тесто'); f.key('ArrowDown'); f.key('Enter');
  assert.deepEqual(f.state(), ['flour', 'unfinished', 'kg', 'kg']);
  assert.equal(f.input.value, 'Мука пшеничная'); assert.equal(f.notifications.length, 0);
  assert.ok(f.row.q.validityMessage);
});
check('menu opens upward only with insufficient space below and sufficient space above', () => {
  const f = fixture(); f.win.innerHeight = 800;
  f.input.getBoundingClientRect = () => ({top: 600, bottom: 648}); f.fire(f.input, 'focus');
  assert.ok(f.input.parentNode.className.includes('opens-up'));
  f.key('Escape'); f.input.getBoundingClientRect = () => ({top: 100, bottom: 148}); f.fire(f.input, 'focus');
  assert.equal(f.input.parentNode.className.includes('opens-up'), false);
  f.key('Escape'); f.win.innerHeight = 350;
  f.input.getBoundingClientRect = () => ({top: 180, bottom: 228}); f.fire(f.input, 'focus');
  assert.equal(f.input.parentNode.className.includes('opens-up'), false);
});
console.log(`${checks} component search behavior checks passed (DOM fixture; browser unverified).`);
