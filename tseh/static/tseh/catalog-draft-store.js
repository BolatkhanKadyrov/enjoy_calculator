/* Local recovery for server drafts. Only allowlisted text is stored, never command tokens. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TsehCatalogDraftStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const fields = ['name', 'notes', 'source_text', 'unit', 'ingredient_kind', 'loss', 'supplier',
    'price_date', 'pack_size', 'pack_price', 'zero_price_confirmed', 'output_quantity', 'output_unit',
    'base_price', 'green_override', 'orange_override'];
  const rowFields = ['component', 'label', 'quantity', 'unit', 'note', 'componentLabel'];
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  function sanitize(value) {
    if (!record(value) || !record(value.fields) || !Array.isArray(value.rows) || value.rows.length > 100) return null;
    const result = {fields: {}, rows: []};
    function copy(source, target, names) {
      for (const name of names) {
        if (!Object.prototype.hasOwnProperty.call(source, name)) continue;
        if (typeof source[name] !== 'string' || source[name].length > 40000) return false;
        target[name] = source[name];
      }
      return true;
    }
    if (!copy(value.fields, result.fields, fields) || !copy(value, result, ['revision', 'submittedRequest'])) return null;
    for (const source of value.rows) {
      const row = {};
      if (!record(source) || !copy(source, row, rowFields)) return null;
      result.rows.push(row);
    }
    return result;
  }
  function createStore(storage, user, path) {
    const valid = typeof user === 'string' && user.length > 0 && typeof path === 'string' && path.startsWith('/');
    const key = 'tseh:catalog-draft:v1:' + JSON.stringify([user, path]);
    return {
      load() {
        try {
          if (!valid) return {ok: false};
          const raw = storage.getItem(key);
          if (raw === null) return {ok: true, draft: null};
          if (typeof raw !== 'string' || raw.length > 600000) return {ok: false};
          const envelope = JSON.parse(raw);
          if (!record(envelope) || envelope.version !== 1) return {ok: false};
          const draft = sanitize(envelope.draft);
          return draft ? {ok: true, draft} : {ok: false};
        } catch (_) { return {ok: false}; }
      },
      save(value) {
        try {
          const draft = sanitize(value);
          if (!valid || !draft) return false;
          const raw = JSON.stringify({version: 1, draft});
          if (raw.length > 600000) return false;
          storage.setItem(key, raw);
          return true;
        } catch (_) { return false; }
      },
      clear() {
        try { if (!valid) return false; storage.removeItem(key); return true; }
        catch (_) { return false; }
      },
    };
  }
  function recoveryAction(draft, {revision, isPost = false, requestId = ''}) {
    if (!draft) return 'none';
    if (isPost && requestId && draft.submittedRequest === requestId) return 'server';
    if (draft.revision !== revision) return 'choose';
    return 'restore';
  }
  return {fields, rowFields, createStore, recoveryAction};
});
