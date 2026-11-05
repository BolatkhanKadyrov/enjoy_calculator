const assert = require('node:assert/strict');
const {createStore, shouldRestore} = require('../tseh/static/tseh/draft-store.js');

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(String(key), String(value)); }
  removeItem(key) { this.values.delete(key); }
}
const row = () => ({component: 'ingredient:00000000-0000-0000-0000-000000000001',
  quantity: ' 0,004000000001 ', unit: 'kg', quantity_unit: 'kg', componentLabel: 'Мука 🥐'});
const draft = () => ({fields: {name: 'Цех «Әже» 🥟\n', base_price: 'not yet a number',
  output_quantity: '1.000000000000001', output_unit: 'kg', output_input_unit: 'g',
  green_mode: 'custom', green_value: ',40', orange_mode: 'default', orange_value: ''},
  rows: [row()], originRevision: '42', originEpoch: '7a7a124c-13bc-4ed9-b01b-8862e4f7e9b3',
  submittedRequest: '0538cdb4-bd9b-4d22-9c62-4bde9ef6c5eb'});
let checks = 0;
function check(name, callback) {
  try { callback(); checks += 1; }
  catch (error) { error.message = `${name}: ${error.message}`; throw error; }
}

check('exact text, unicode and unvalidated numeric input round trip', () => {
  const storage = new MemoryStorage();
  const store = createStore(storage, 'user-1', '/dishes/new/');
  assert.deepEqual(store.load(), {ok: true, draft: null});
  const payload = draft();
  assert.deepEqual(store.save(payload), {ok: true});
  assert.deepEqual(store.load(), {ok: true, draft: payload});
  payload.fields.name = 'changed after save';
  assert.equal(store.load().draft.fields.name, 'Цех «Әже» 🥟\n');
  const loaded = store.load().draft;
  loaded.rows[0].quantity = 'changed after load';
  assert.equal(store.load().draft.rows[0].quantity, ' 0,004000000001 ');
});

check('zero rows are a draft, not absence', () => {
  const store = createStore(new MemoryStorage(), '1', '/preparations/new/');
  assert.deepEqual(store.save({fields: {}, rows: []}), {ok: true});
  assert.deepEqual(store.load(), {ok: true, draft: {fields: {}, rows: []}});
});

check('account, path, separator collision and storage isolation', () => {
  const storage = new MemoryStorage();
  const a = createStore(storage, 'a:b', '/c');
  const b = createStore(storage, 'a', 'b:/c');
  const account = createStore(storage, 'another-user', '/c');
  const path = createStore(storage, 'a:b', '/different');
  a.save(draft());
  for (const other of [b, account, path, createStore(new MemoryStorage(), 'a:b', '/c')]) {
    assert.deepEqual(other.load(), {ok: true, draft: null});
  }
  b.save({fields: {name: 'B'}, rows: []});
  assert.deepEqual(a.clear(), {ok: true});
  assert.deepEqual(a.load(), {ok: true, draft: null});
  assert.equal(b.load().draft.fields.name, 'B');
});

check('tokens, HTML containers and unknown fields stripped at every level', () => {
  const storage = new MemoryStorage();
  const store = createStore(storage, '1', '/new/');
  const payload = draft();
  payload.token = 'secret-token';
  payload.csrfmiddlewaretoken = 'secret-csrf';
  payload.preview_token = 'secret-preview';
  payload.html = '<input token="secret">';
  payload.fields.token = 'secret-field';
  payload.fields.unexpected = 42;
  payload.rows[0].token = 'secret-row';
  payload.rows[0].html = '<script>never retained</script>';
  assert.equal(store.save(payload).ok, true);
  assert.deepEqual(store.load().draft, draft());
  assert.ok(![...storage.values.values()][0].includes('secret'));
  // Known text values stay literal; rendering must use value/textContent, never innerHTML.
  const literal = {fields: {name: '<b>literal name</b>'}, rows: []};
  store.save(literal);
  assert.deepEqual(store.load().draft, literal);
});

check('invalid shape never overwrites existing valid draft', () => {
  const store = createStore(new MemoryStorage(), '1', '/new/');
  store.save(draft());
  const invalid = [null, [], {}, {fields: {}, rows: null}, {fields: [], rows: []},
    {fields: {name: 1}, rows: []}, {fields: {}, rows: [{}]},
    {fields: {}, rows: [null]}, {fields: {}, rows: [{...row(), quantity: 1}]},
    {fields: {}, rows: [], originRevision: 1}, {fields: {}, rows: [], originEpoch: 1},
    {fields: {}, rows: [], submittedRequest: false}];
  for (const payload of invalid) {
    assert.deepEqual(store.save(payload), {ok: false, error: 'invalid_shape'});
    assert.deepEqual(store.load().draft, draft());
  }
});

check('bounds on rows, text and total JSON size', () => {
  const storage = new MemoryStorage();
  const store = createStore(storage, '1', '/new/');
  assert.equal(store.save({fields: {}, rows: Array.from({length: 100}, row)}).ok, true);
  for (const payload of [
    {fields: {}, rows: Array.from({length: 101}, row)},
    {fields: {name: 'x'.repeat(16385)}, rows: []},
    {fields: {}, rows: Array.from({length: 100}, () => ({...row(), componentLabel: 'я'.repeat(3000)}))},
  ]) assert.deepEqual(store.save(payload), {ok: false, error: 'too_large'});
  const key = [...storage.values.keys()][0];
  storage.setItem(key, ' '.repeat(256 * 1024 + 1));
  assert.deepEqual(store.load(), {ok: false, error: 'too_large'});
});

check('malformed, unsupported and malformed schema restore errors', () => {
  const storage = new MemoryStorage();
  const store = createStore(storage, '1', '/new/');
  store.save(draft());
  const key = [...storage.values.keys()][0];
  for (const [raw, error] of [
    ['{bad', 'corrupt_json'], ['null', 'invalid_shape'], ['[]', 'invalid_shape'],
    ['{}', 'invalid_shape'], [JSON.stringify({version: 2, payload: draft()}), 'unsupported_version'],
    [JSON.stringify({version: '1', payload: draft()}), 'unsupported_version'],
    [JSON.stringify({version: 1, payload: {fields: {}, rows: [row(), null]}}), 'invalid_shape'],
  ]) {
    storage.setItem(key, raw);
    assert.deepEqual(store.load(), {ok: false, error});
    assert.equal(storage.getItem(key), raw); // An error does not silently discard data.
  }
  storage.setItem(key, JSON.stringify({version: 1, payload: {...draft(), token: 'untrusted'}}));
  assert.deepEqual(store.load().draft, draft());
});

check('quota and access errors cannot report success', () => {
  const throwing = {getItem() { throw new Error('denied'); },
    setItem() { throw new Error('quota'); }, removeItem() { throw new Error('denied'); }};
  for (const storage of [throwing, null, {}]) {
    const store = createStore(storage, '1', '/new/');
    assert.deepEqual(store.save(draft()), {ok: false, error: 'storage_unavailable'});
    assert.deepEqual(store.load(), {ok: false, error: 'storage_unavailable'});
    assert.deepEqual(store.clear(), {ok: false, error: 'storage_unavailable'});
  }
});

check('invalid scopes and hostile input do not escape as exceptions', () => {
  for (const [user, path] of [[null, '/'], ['', '/'], ['1', null], ['1', '']]) {
    const store = createStore(new MemoryStorage(), user, path);
    assert.deepEqual(store.save(draft()), {ok: false, error: 'invalid_shape'});
    assert.deepEqual(store.load(), {ok: false, error: 'invalid_shape'});
    assert.deepEqual(store.clear(), {ok: false, error: 'invalid_shape'});
  }
  const store = createStore(new MemoryStorage(), '1', '/');
  const hostile = {get fields() { throw new Error('bad getter'); }};
  assert.deepEqual(store.save(hostile), {ok: false, error: 'invalid_shape'});
});

check('restore decision distinguishes first POST response from replayed POST', () => {
  const payload = draft();
  assert.equal(shouldRestore(null, {isPost: false}), false);
  assert.equal(shouldRestore(undefined, {isPost: true, requestId: 'anything'}), false);
  assert.equal(shouldRestore(payload, {isPost: false, requestId: payload.submittedRequest}), true);
  assert.equal(shouldRestore(payload, {isPost: true, requestId: payload.submittedRequest}), false);
  assert.equal(shouldRestore(payload, {isPost: true, requestId: 'different-request'}), true);
  assert.equal(shouldRestore(payload, {isPost: true}), true);
  assert.equal(shouldRestore({...payload, submittedRequest: ''}, {isPost: true, requestId: ''}), true);
  const storage = new MemoryStorage();
  const store = createStore(storage, '1', '/dish/edit/');
  store.save(payload);
  assert.equal(shouldRestore(store.load().draft, {isPost: true, requestId: payload.submittedRequest}), false);
  // DOM consumes the matching reply, then clears only the request marker.
  const fresh = {...store.load().draft, submittedRequest: ''};
  fresh.fields.name = 'More recent unsaved edit';
  store.save(fresh);
  assert.equal(shouldRestore(store.load().draft, {isPost: true, requestId: payload.submittedRequest}), true);
  assert.equal(store.load().draft.originEpoch, payload.originEpoch);
  assert.equal(store.load().draft.fields.name, 'More recent unsaved edit');
});

console.log(`${checks} draft storage checks passed`);
