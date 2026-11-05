/* Drafts are text-only form data. Tokens and rendered markup are never persisted. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TsehDraftStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const VERSION = 1;
  // Bounds are in UTF-16 code units (the unit used by JavaScript strings).
  const MAX_SERIALIZED_LENGTH = 256 * 1024;
  const MAX_TEXT_LENGTH = 16384;
  const FIELD_NAMES = ['name', 'base_price', 'output_quantity', 'output_unit', 'output_input_unit',
    'green_mode', 'green_value', 'orange_mode', 'orange_value'];
  const ROW_NAMES = ['component', 'quantity', 'unit', 'quantity_unit'];
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const failure = error => ({ok: false, error});

  function copyText(source, target, name, required) {
    if (!own(source, name)) return required ? 'invalid_shape' : null;
    const value = source[name];
    if (typeof value !== 'string') return 'invalid_shape';
    if (value.length > MAX_TEXT_LENGTH) return 'too_large';
    target[name] = value;
    return null;
  }

  function sanitize(payload) {
    if (!record(payload) || !record(payload.fields) || !Array.isArray(payload.rows)) {
      return failure('invalid_shape');
    }
    if (payload.rows.length > 100) return failure('too_large');
    const draft = {fields: {}, rows: []};
    for (const name of FIELD_NAMES) {
      const error = copyText(payload.fields, draft.fields, name, false);
      if (error) return failure(error);
    }
    for (const source of payload.rows) {
      if (!record(source)) return failure('invalid_shape');
      const row = {};
      for (const name of ROW_NAMES) {
        const error = copyText(source, row, name, true);
        if (error) return failure(error);
      }
      const error = copyText(source, row, 'componentLabel', false);
      if (error) return failure(error);
      draft.rows.push(row);
    }
    for (const name of ['originRevision', 'originEpoch', 'submittedRequest']) {
      const error = copyText(payload, draft, name, false);
      if (error) return failure(error);
    }
    return {ok: true, draft};
  }

  function shouldRestore(draft, context) {
    if (!draft) return false;
    if (!context || !context.isPost) return true;
    return !(typeof context.requestId === 'string' && context.requestId.length > 0 &&
      context.requestId === draft.submittedRequest);
  }

  function createStore(storage, userScope, path) {
    const validScope = typeof userScope === 'string' && userScope.length > 0 &&
      userScope.length <= MAX_TEXT_LENGTH && typeof path === 'string' && path.length > 0 &&
      path.length <= MAX_TEXT_LENGTH;
    // A tuple avoids collisions between account/path pairs containing separators.
    const key = validScope ? 'tseh:draft:v1:' + JSON.stringify([userScope, path]) : null;
    return {
      save(payload) {
        if (!validScope) return failure('invalid_shape');
        let serialized;
        try {
          const result = sanitize(payload);
          if (!result.ok) return result;
          serialized = JSON.stringify({version: VERSION, payload: result.draft});
          if (serialized.length > MAX_SERIALIZED_LENGTH) return failure('too_large');
        } catch (_) {
          return failure('invalid_shape');
        }
        try {
          storage.setItem(key, serialized);
          return {ok: true};
        } catch (_) {
          return failure('storage_unavailable');
        }
      },
      load() {
        if (!validScope) return failure('invalid_shape');
        let serialized;
        try {
          serialized = storage.getItem(key);
        } catch (_) {
          return failure('storage_unavailable');
        }
        if (serialized === null) return {ok: true, draft: null};
        if (typeof serialized !== 'string') return failure('invalid_shape');
        if (serialized.length > MAX_SERIALIZED_LENGTH) return failure('too_large');
        let envelope;
        try {
          envelope = JSON.parse(serialized);
        } catch (_) {
          return failure('corrupt_json');
        }
        if (!record(envelope) || !own(envelope, 'version')) return failure('invalid_shape');
        if (envelope.version !== VERSION) return failure('unsupported_version');
        return sanitize(envelope.payload);
      },
      clear() {
        if (!validScope) return failure('invalid_shape');
        try {
          storage.removeItem(key);
          return {ok: true};
        } catch (_) {
          return failure('storage_unavailable');
        }
      },
    };
  }

  return {createStore, shouldRestore};
});
