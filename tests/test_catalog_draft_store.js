const assert = require('node:assert/strict');
const {createStore, recoveryAction} = require('../tseh/static/tseh/catalog-draft-store.js');
class Storage {
  constructor() { this.items = new Map(); }
  getItem(key) { return this.items.get(key) ?? null; }
  setItem(key, value) { this.items.set(key, value); }
  removeItem(key) { this.items.delete(key); }
}
const data = () => ({fields: {name: 'Проба Ә', pack_price: '', notes: 'Уточнить\nвыход', zero_price_confirmed: ''},
  rows: [{component: '', label: 'Исходное название', quantity: '30–50', unit: 'неизвестно', note: 'по вкусу'}],
  revision: '1', submittedRequest: 'request'});
const storage = new Storage();
const one = createStore(storage, '1', '/drafts/new/dish/');
assert.equal(one.save(data()), true);
assert.deepEqual(one.load().draft, data());
assert.deepEqual(createStore(storage, '2', '/drafts/new/dish/').load(), {ok: true, draft: null});
assert.deepEqual(createStore(storage, '1', '/drafts/new/ingredient/').load(), {ok: true, draft: null});
const secrets = data();
secrets.fields.token = 'must-not-persist';
secrets.fields.csrfmiddlewaretoken = 'must-not-persist';
secrets.fields.password = 'must-not-persist';
secrets.token = 'must-not-persist';
assert.equal(one.save(secrets), true);
assert.equal([...storage.items.values()].join('').includes('must-not-persist'), false);
const tooMany = data();
tooMany.rows = Array.from({length: 101}, () => ({}));
assert.equal(one.save(tooMany), false);
assert.deepEqual(one.load().draft, data());
assert.equal(one.save({fields: {name: 0}, rows: []}), false);
assert.equal(one.save({...data(), fields: {name: 'x'.repeat(40001)}}), false);
assert.equal(createStore(null, '1', '/drafts/new/dish/').load().ok, false);
assert.equal(createStore(null, '1', '/drafts/new/dish/').save(data()), false);
assert.equal(createStore(null, '1', '/drafts/new/dish/').clear(), false);
const key = [...storage.items.keys()][0];
storage.items.set(key, '{bad json');
assert.equal(one.load().ok, false);
assert.equal(storage.items.get(key), '{bad json');
storage.items.set(key, JSON.stringify({version: 999, draft: data()}));
assert.equal(one.load().ok, false);
assert.equal(one.clear(), true);
assert.deepEqual(one.load(), {ok: true, draft: null});
assert.equal(createStore(storage, '', '/drafts/new/dish/').save(data()), false);
assert.equal(recoveryAction({...data(), revision: 'epoch-A:1'}, {revision: 'epoch-B:1'}), 'choose');
assert.equal(recoveryAction({...data(), revision: 'epoch-A:new'}, {revision: 'epoch-B:new'}), 'choose');
assert.equal(recoveryAction({...data(), revision: 'epoch-A:1'}, {revision: 'epoch-A:1'}), 'restore');
assert.equal(recoveryAction(data(), {revision: '1', isPost: true, requestId: 'request'}), 'server');
assert.equal(recoveryAction({...data(), submittedRequest: ''}, {revision: '1', isPost: true, requestId: 'request'}), 'restore');
console.log('Catalog draft recovery storage checks passed');
