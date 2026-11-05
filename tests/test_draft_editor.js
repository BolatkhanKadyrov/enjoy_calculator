/* Controller behavior tests, not a browser substitute. No layout, native navigation,
 * constraint validation or app.js event wiring is simulated here. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const storeAPI = require('../tseh/static/tseh/draft-store.js');
const context = vm.createContext({URL});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../tseh/static/tseh/draft-editor.js'), 'utf8'), context);
const controller = context.TsehDraftEditor;

class Element {
  constructor(tag, props = {}) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this._value = '';
    Object.assign(this, props);
  }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  get selectedOptions() { return this.options.filter(option => option.value === this.value); }
  get value() { return this._value; }
  set value(value) {
    value = String(value);
    this._value = this.tagName === 'SELECT' && !this.options.some(option => option.value === value) ? '' : value;
  }
  append(...children) {
    for (const child of children) {
      if (child.tagName === '#FRAGMENT') this.append(...child.children);
      else this.children.push(child);
    }
  }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelectorAll(selector) {
    const match = node => {
      if (selector.startsWith('.')) return (node.className || '').split(' ').includes(selector.slice(1));
      const attr = selector.match(/^(\w+)?\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (!attr) throw Error(`Fixture does not implement selector: ${selector}`);
      if (attr[1] && node.tagName !== attr[1].toUpperCase()) return false;
      const property = attr[2].startsWith('data-')
        ? node.dataset[attr[2].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())]
        : node[attr[2]];
      return attr[3] === undefined ? property !== undefined : property === attr[3];
    };
    return this.all().filter(match);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  fire(type, properties = {}) { for (const listener of this.listeners[type] || []) listener({target: this, ...properties}); }
}

const input = (name, value, type = 'text') => new Element('input', {name, value, type});
const select = (name, value, choices) => {
  const element = new Element('select', {name});
  element.append(...choices.map(([value, textContent]) => new Element('option', {value, textContent})));
  element.value = value;
  return element;
};
const choices = [['', 'Выберите компонент'], ['ingredient:a', 'Мука'], ['preparation:b', 'Тесто']];
const rowData = (quantity = '1', component = 'ingredient:a') => ({component, quantity, unit: 'kg', quantity_unit: 'kg'});
function row(index, data = rowData(), deleted = false) {
  const prefix = `form-${index}`;
  const element = new Element('div', {className: 'recipe-line', dataset: {prefix}, hidden: deleted});
  element.append(
    select(`${prefix}-component`, data.component, choices),
    input(`${prefix}-quantity`, data.quantity),
    select(`${prefix}-unit`, data.unit, [['kg', 'кг'], ['g', 'г']]),
    input(`${prefix}-quantity_unit`, data.quantity_unit, 'hidden'),
    input(`${prefix}-DELETE`, deleted ? 'on' : '', 'hidden'),
  );
  return element;
}

class Storage {
  constructor() { this.data = new Map(); this.fail = new Set(); }
  getItem(key) { if (this.fail.has('get')) throw Error('denied'); return this.data.get(key) ?? null; }
  setItem(key, value) { if (this.fail.has('set')) throw Error('quota exceeded'); this.data.set(key, value); }
  removeItem(key) { if (this.fail.has('remove')) throw Error('denied'); this.data.delete(key); }
}

const route = '/dishes/new/';
const account = 'team-1';
const store = storage => storeAPI.createStore(storage, account, route);
function fixture({storage = new Storage(), isPost = false, request = '', recipe = true,
  fields = {}, rows = [row(0)], preview = false, receipt = null} = {}) {
  const main = new Element('main', {id: 'main'});
  const form = new Element('form', {id: 'recipe-form', dataset: {
    draftUser: account, draftRevision: '9', draftEpoch: 'current-epoch',
    draftRequest: request, draftSubmitted: String(isPost),
  }});
  const status = new Element('p', {id: 'draft-status'});
  const lines = new Element('div', {id: 'recipe-lines'});
  lines.append(...rows);
  const template = new Element('template', {id: 'empty-line', innerHTML: '<div data-prefix="form-__prefix__"></div>'});
  const refresh = new Element('button', {dataset: {refreshDraft: ''}, hidden: true});
  const fieldsWithDefaults = {name: 'Server name', base_price: '100.00', ...fields};
  form.append(...Object.entries(fieldsWithDefaults).map(([name, value]) => input(name, value)),
    input('token', 'current-server-revision-token', 'hidden'), input('csrfmiddlewaretoken', 'current-csrf', 'hidden'),
    input('form-TOTAL_FORMS', String(rows.length), 'hidden'), input('form-INITIAL_FORMS', String(rows.length), 'hidden'),
    lines, status, template, refresh);
  const save = new Element('button', {value: 'save', textContent: 'Сохранить'});
  const stale = new Element('p', {id: 'preview-stale', hidden: true});
  if (preview) form.append(input('preview_token', 'fresh-server-preview', 'hidden'), save, stale);
  form.elements = {namedItem: name => form.all().find(element => element.name === name) || null};
  if (recipe) main.append(form);
  if (receipt) main.append(new Element('script', {id: 'draft-saved-receipt', textContent: JSON.stringify(receipt)}));
  let parsedTemplates = 0;
  const doc = {
    getElementById: id => [main, ...main.all()].find(element => element.id === id) || null,
    createDocumentFragment: () => new Element('#fragment'),
    createElement(tag) {
      if (tag !== 'template') return new Element(tag);
      // Only stand in for parsing the fixed server row template. The controller
      // must set draft values through DOM properties; they are never parsed here.
      return {
        content: null,
        set innerHTML(html) {
          const match = /^<div data-prefix="form-(\d+)"><\/div>$/.exec(html);
          assert.ok(match, 'only the trusted fixture template may be parsed');
          parsedTemplates++;
          this.content = new Element('#fragment');
          this.content.append(row(Number(match[1]), rowData('', '')));
        },
      };
    },
  };
  const win = new Element('window');
  const navigation = [];
  win.sessionStorage = storage;
  win.TsehDraftStore = storeAPI;
  win.crypto = {randomUUID: () => 'request-from-submit'};
  win.location = {href: `http://localhost${route}?draft_saved=receipt#anchor`, pathname: route,
    assign: target => navigation.push({target, saved: store(storage).load()}),
    replace: target => navigation.push({target, replace: true, saved: store(storage).load()})};
  const history = [];
  win.history = {replaceState: (_, __, url) => history.push(url)};
  return {win, doc, form, status, lines, refresh, save, stale, storage, navigation, history,
    field: name => form.elements.namedItem(name), start: () => controller.init(win, doc),
    get parsedTemplates() { return parsedTemplates; }};
}

let checks = 0;
function check(name, test) { test(); checks++; console.log(`OK ${name}`); }

check('capture preserves exact text and includes new rows but excludes deleted rows', () => {
  const f = fixture({fields: {name: ' Әже 🥟\n', base_price: 'not a number'},
    rows: [row(0, rowData(' 0,004000000001 ')), row(1, rowData('discard'), true), row(2, rowData('.5', 'preparation:b'))]});
  f.start(); f.form.fire('input');
  const draft = store(f.storage).load().draft;
  assert.equal(draft.fields.name, ' Әже 🥟\n');
  assert.equal(draft.fields.base_price, 'not a number');
  assert.deepEqual(draft.rows.map(r => r.quantity), [' 0,004000000001 ', '.5']);
  assert.equal(draft.rows[1].component, 'preparation:b');
  assert.equal(draft.fields.token, undefined);
  assert.equal(draft.fields.csrfmiddlewaretoken, undefined);
});

check('GET reload rebuilds edited rows and never restores server approvals', () => {
  const storage = new Storage();
  store(storage).save({fields: {name: 'Saved draft', base_price: ',5'},
    rows: [rowData(' 0,004000000001 '), rowData('unfinished', 'preparation:b')],
    originRevision: '3', originEpoch: 'old-epoch'});
  const f = fixture({storage, preview: true}); f.start();
  assert.equal(f.field('name').value, 'Saved draft');
  assert.equal(f.field('base_price').value, ',5');
  assert.equal(f.field('form-0-quantity').value, ' 0,004000000001 ');
  assert.equal(f.field('form-1-quantity').value, 'unfinished');
  assert.equal(f.field('form-TOTAL_FORMS').value, '2');
  assert.equal(f.field('form-INITIAL_FORMS').value, '0');
  assert.equal(f.field('token').value, 'current-server-revision-token');
  assert.equal(f.field('csrfmiddlewaretoken').value, 'current-csrf');
  assert.equal(f.field('preview_token').value, '');
  assert.equal(f.save.disabled, true);
  assert.equal(f.stale.hidden, false);
  assert.equal(f.form.dataset.dirty, 'true');
  assert.match(f.status.textContent, /Исходные данные изменились/);
});

check('deleted-last-row draft restores an empty composition', () => {
  const storage = new Storage(); store(storage).save({fields: {name: 'Keep my name'}, rows: []});
  const f = fixture({storage}); f.start();
  assert.equal(f.lines.children.length, 0);
  assert.equal(f.field('form-TOTAL_FORMS').value, '0');
  assert.equal(f.field('name').value, 'Keep my name');
});

check('unavailable component is retained as inert text with a warning', () => {
  const storage = new Storage();
  const label = '<img src=x onerror=alert(1)> Сырьё';
  store(storage).save({fields: {}, rows: [{...rowData('1', 'ingredient:removed'), componentLabel: label}]});
  const f = fixture({storage}); f.start();
  const component = f.field('form-0-component');
  assert.equal(component.value, 'ingredient:removed');
  assert.equal(component.selectedOptions[0].textContent, `${label} — недоступен`);
  assert.equal(component.selectedOptions[0].children.length, 0);
  assert.equal(f.parsedTemplates, 1);
  assert.match(f.status.textContent, /компонентов больше недоступен/);
});

check('matching POST response has authority over pre-submit draft', () => {
  const storage = new Storage();
  store(storage).save({fields: {name: 'Before POST'}, rows: [rowData('1000')], submittedRequest: 'post-1'});
  const f = fixture({storage, isPost: true, request: 'post-1', preview: true,
    fields: {name: 'Server normalized'}, rows: [row(0, rowData('1'))]});
  f.start();
  assert.equal(f.field('name').value, 'Server normalized');
  assert.equal(f.field('form-0-quantity').value, '1');
  assert.equal(f.field('preview_token').value, 'fresh-server-preview');
  assert.equal(f.save.disabled, false);
  assert.equal(store(storage).load().draft.rows[0].quantity, '1');
});

check('newer edits win when F5 repeats an older POST', () => {
  const storage = new Storage();
  store(storage).save({fields: {name: 'Before submit'}, rows: [rowData('1')], submittedRequest: 'post-1'});
  const response = fixture({storage, isPost: true, request: 'post-1', preview: true}); response.start();
  response.field('name').value = 'Typed after preview';
  response.field('form-0-quantity').value = ' 2,500 ';
  response.form.fire('input'); response.win.fire('pagehide');
  const reload = fixture({storage, isPost: true, request: 'post-1', preview: true}); reload.start();
  assert.equal(reload.field('name').value, 'Typed after preview');
  assert.equal(reload.field('form-0-quantity').value, ' 2,500 ');
  assert.equal(reload.field('preview_token').value, '');
  assert.equal(reload.save.disabled, true);
});

check('submit and validation failure retain draft until matching success receipt', () => {
  const f = fixture(); f.start(); f.form.fire('input'); f.form.fire('submit'); f.win.fire('pagehide');
  assert.equal(f.field('_draft_request').value, 'request-from-submit');
  assert.equal(store(f.storage).load().draft.submittedRequest, 'request-from-submit');
  const rejected = fixture({storage: f.storage, isPost: true, request: 'request-from-submit', fields: {name: 'Invalid but preserved'}});
  rejected.start();
  assert.equal(store(f.storage).load().draft.fields.name, 'Invalid but preserved');
  rejected.form.fire('submit');
  const detail = fixture({storage: f.storage, recipe: false,
    receipt: {user: account, path: route, request: 'request-from-submit'}});
  detail.start();
  assert.equal(store(f.storage).load().draft, null);
  assert.equal(detail.history[0], `${route}#anchor`);
});

check('unrelated success receipt cannot clear newer unsent edits', () => {
  const storage = new Storage();
  const newer = {fields: {name: 'Newer local'}, rows: [], submittedRequest: ''};
  store(storage).save(newer);
  const detail = fixture({storage, recipe: false, receipt: {user: account, path: route, request: 'old-request'}});
  detail.start();
  assert.equal(store(storage).load().draft.fields.name, 'Newer local');
});

check('refresh saves exact current values before navigation', () => {
  const f = fixture(); f.start(); f.field('name').value = 'Do not lose this'; f.refresh.fire('click');
  assert.equal(f.refresh.hidden, false);
  assert.equal(f.navigation.length, 1);
  assert.equal(f.navigation[0].target, route);
  assert.equal(f.navigation[0].saved.draft.fields.name, 'Do not lose this');
});

check('quota failure blocks refresh and keeps current form values', () => {
  const f = fixture(); f.start(); f.storage.fail.add('set');
  f.field('name').value = 'Unsaved work'; f.refresh.fire('click');
  assert.equal(f.navigation.length, 0);
  assert.equal(f.field('name').value, 'Unsaved work');
  assert.equal(f.status.dataset.state, 'error');
  assert.match(f.status.textContent, /Не обновляйте/);
});

check('unreadable storage is not overwritten and refresh is blocked', () => {
  const storage = new Storage(); store(storage).save({fields: {name: 'Original'}, rows: []});
  const before = [...storage.data.entries()]; storage.fail.add('get');
  const f = fixture({storage}); f.start(); storage.fail.delete('get');
  f.field('name').value = 'New input'; f.form.fire('input'); f.refresh.fire('click');
  assert.deepEqual([...storage.data.entries()], before);
  assert.equal(f.navigation.length, 0);
  assert.equal(f.status.dataset.state, 'error');
});

check('explicit reset aborts on clear failure and successful reset suppresses pagehide rewrite', () => {
  const f = fixture(); f.start(); f.form.fire('input');
  f.storage.fail.add('remove');
  assert.equal(f.form.tsehDraft.clear(), false);
  assert.ok(store(f.storage).load().draft);
  f.storage.fail.delete('remove');
  assert.equal(f.form.tsehDraft.clear(), true);
  f.win.fire('pagehide');
  assert.equal(store(f.storage).load().draft, null);
});

check('BFCache return after confirmed save cannot resurrect the old draft', () => {
  const cached = fixture(); cached.start(); cached.form.fire('input'); cached.form.fire('submit'); cached.win.fire('pagehide');
  const detail = fixture({storage: cached.storage, recipe: false,
    receipt: {user: account, path: route, request: 'request-from-submit'}});
  detail.start();
  assert.equal(store(cached.storage).load().draft, null);
  cached.win.fire('pageshow', {persisted: true});
  cached.win.fire('pagehide');
  assert.equal(cached.navigation.length, 1);
  assert.equal(cached.navigation[0].replace, true);
  assert.equal(cached.navigation[0].target, route);
  assert.equal(store(cached.storage).load().draft, null);
});

check('ordinary pageshow leaves the editor and existing draft alone', () => {
  const f = fixture(); f.start(); f.win.fire('pageshow', {persisted: false});
  f.field('name').value = 'Still editable'; f.form.fire('input');
  assert.equal(f.navigation.length, 0);
  assert.equal(store(f.storage).load().draft.fields.name, 'Still editable');
});

check('receipt clear failure keeps the backup and visibly warns without undoing server success', () => {
  const storage = new Storage();
  store(storage).save({fields: {name: 'Saved server-side'}, rows: [], submittedRequest: 'success-1'});
  storage.fail.add('remove');
  const f = fixture({storage, recipe: false, receipt: {user: account, path: route, request: 'success-1'}});
  f.start();
  assert.equal(store(storage).load().draft.fields.name, 'Saved server-side');
  assert.match(f.doc.getElementById('main').children[0].textContent, /сохранена в базе.*не смог удалить/);
  assert.equal(f.history.length, 1);
});

check('unsupported stored copy needs explicit reset and is not silently replaced', () => {
  const storage = new Storage(); store(storage).save({fields: {}, rows: []});
  const key = [...storage.data.keys()][0];
  storage.data.set(key, '{"version":99,"payload":{"fields":{},"rows":[]}}');
  const f = fixture({storage}); f.start(); f.form.fire('input');
  assert.equal(f.form.dataset.dirty, 'true');
  assert.equal(JSON.parse(storage.data.get(key)).version, 99);
  assert.equal(f.form.tsehDraft.clear(), true);
  assert.equal(storage.data.has(key), false);
});

check('denied sessionStorage getter blocks refresh rather than discarding input', () => {
  const f = fixture();
  Object.defineProperty(f.win, 'sessionStorage', {get() { throw Error('SecurityError'); }});
  f.start(); f.field('name').value = 'Keep on page'; f.refresh.fire('click');
  assert.equal(f.navigation.length, 0);
  assert.equal(f.field('name').value, 'Keep on page');
  assert.equal(f.status.dataset.state, 'error');
});

check('HTTP randomUUID fallback still records a request before submission', () => {
  const f = fixture();
  f.win.crypto = {getRandomValues(bytes) { bytes.fill(17); return bytes; }};
  f.start(); f.form.fire('submit');
  const request = f.field('_draft_request').value;
  assert.match(request, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(store(f.storage).load().draft.submittedRequest, request);
});

check('matching invalid-choice POST preserves missing component and server-normalized quantity through F5', () => {
  const storage = new Storage();
  const label = 'Удалённая заготовка';
  store(storage).save({fields: {name: 'Submitted'}, rows: [{...rowData('1000', 'preparation:removed'), componentLabel: label}], submittedRequest: 'invalid-1'});
  const serverRow = row(0, rowData('1', ''));
  const serverError = new Element('ul', {className: 'errorlist', textContent: 'Выберите корректный вариант.'});
  serverRow.append(serverError);
  const response = fixture({storage, isPost: true, request: 'invalid-1', rows: [serverRow]});
  response.start();
  assert.equal(response.field('form-0-component').value, 'preparation:removed');
  assert.match(response.field('form-0-component').selectedOptions[0].textContent, /Удалённая заготовка/);
  assert.equal(response.field('form-0-quantity').value, '1');
  assert.equal(response.lines.querySelector('.errorlist'), serverError, 'server validation errors remain intact');
  const reload = fixture({storage, isPost: true, request: 'invalid-1', rows: [row(0, rowData('1', ''))]});
  reload.start();
  assert.equal(reload.field('form-0-component').value, 'preparation:removed');
  assert.match(reload.field('form-0-component').selectedOptions[0].textContent, /Удалённая заготовка/);
  assert.equal(reload.field('form-0-quantity').value, '1');
});

check('reset then BFCache return starts a fresh editable GET whose later draft survives F5', () => {
  const cached = fixture(); cached.start();
  cached.field('name').value = 'Discarded old draft'; cached.form.fire('input');
  assert.equal(cached.form.tsehDraft.clear(), true);
  cached.win.fire('pagehide');
  cached.win.fire('pageshow', {persisted: true});
  cached.win.fire('pagehide');
  assert.equal(cached.navigation[0].replace, true);
  assert.equal(store(cached.storage).load().draft, null);
  const fresh = fixture({storage: cached.storage}); fresh.start();
  assert.equal(fresh.field('name').value, 'Server name');
  fresh.field('name').value = 'New draft after reset';
  fresh.field('form-0-quantity').value = ',125';
  fresh.form.fire('input'); fresh.win.fire('pagehide');
  const reload = fixture({storage: cached.storage}); reload.start();
  assert.equal(reload.field('name').value, 'New draft after reset');
  assert.equal(reload.field('form-0-quantity').value, ',125');
});

console.log(`${checks} draft editor behavior checks passed (fake DOM; no browser validation)`);
