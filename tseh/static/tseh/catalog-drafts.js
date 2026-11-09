/* Form behavior and recovery only. Saving, validation and publication are server-owned. */
document.addEventListener('DOMContentLoaded', () => {
  'use strict';
  const api = window.TsehCatalogDraftStore;
  if (!api) return;
  let storage = null;
  try { storage = window.sessionStorage; } catch (_) { /* Display fallback below. */ }
  const receiptNode = document.getElementById('draft-saved-receipt');
  if (receiptNode) {
    try {
      const receipt = JSON.parse(receiptNode.textContent);
      const store = api.createStore(storage, receipt.user, receipt.path);
      const loaded = store.load();
      if (loaded.ok && loaded.draft?.submittedRequest === receipt.request && !store.clear()) {
        const notice = document.createElement('p');
        notice.className = 'notice';
        notice.textContent = 'Черновик сохранён на сервере, но локальную копию удалить не удалось. При повторном открытии проверьте, какую версию восстанавливаете.';
        document.getElementById('main').prepend(notice);
      }
    } catch (_) { /* Unreadable acknowledgement must never clear entered text. */ }
  }
  const form = document.getElementById('catalog-draft-form');
  if (!form) return;
  const store = api.createStore(storage, form.dataset.user, window.location.pathname);
  const status = document.getElementById('catalog-draft-status');
  const lines = document.getElementById('catalog-draft-lines');
  const template = document.getElementById('catalog-draft-empty-line');
  const add = document.getElementById('catalog-draft-add-line');
  const recovery = document.getElementById('catalog-draft-recovery');
  const field = name => form.elements.namedItem(name);
  const activeRows = () => [...(lines?.querySelectorAll('.draft-edit-line') || [])].filter(row => !row.hidden);
  let blocked = false, suspended = false, pendingRequest = '', dirty = false;
  let originRevision = form.dataset.version;
  const show = text => { status.textContent = text; };
  const capture = () => ({
    fields: Object.fromEntries(api.fields.filter(name => field(name)).map(name => [name,
      field(name).type === 'checkbox' ? (field(name).checked ? 'on' : '') : field(name).value])),
    rows: activeRows().map(row => {
      const get = name => row.querySelector(`[name="${row.dataset.prefix}-${name}"]`);
      const result = Object.fromEntries(api.rowFields.filter(name => name !== 'componentLabel').map(name => [name, get(name)?.value || '']));
      result.componentLabel = get('component')?.selectedOptions[0]?.textContent || '';
      return result;
    }),
    revision: originRevision, submittedRequest: pendingRequest,
  });
  const persist = () => {
    if (suspended) return false;
    if (blocked || !store.save(capture())) {
      show('Не удалось сохранить ввод в этой вкладке. Не обновляйте страницу до нажатия «Сохранить черновик». Серверная запись пока не изменена.');
      return false;
    }
    show('Ввод сохранён в этой вкладке и восстановится после F5. Чтобы вернуться к нему после закрытия вкладки, нажмите «Сохранить черновик».');
    return true;
  };
  const setValue = (element, value, label) => {
    if (!element || typeof value !== 'string') return;
    if (element.type === 'checkbox') { element.checked = value === 'on'; return; }
    if (element.tagName === 'SELECT' && value && ![...element.options].some(option => option.value === value)) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label || 'Сохранённое значение — требуется проверка';
      element.append(option);
    }
    element.value = value;
  };
  const newRow = (index, data) => {
    const holder = document.createElement('template');
    // Only a trusted server-authored template becomes HTML; user content is assigned as text/value.
    holder.innerHTML = template.innerHTML.replaceAll('__prefix__', String(index));
    const row = holder.content.querySelector('.draft-edit-line');
    row.hidden = false;
    api.rowFields.filter(name => name !== 'componentLabel').forEach(name => {
      setValue(row.querySelector(`[name="lines-${index}-${name}"]`), data?.[name] || '', name === 'component' ? data?.componentLabel : '');
    });
    row.querySelector('[name$="-DELETE"]').value = '';
    return row;
  };
  const updateRows = () => {
    if (!lines) return;
    const visible = activeRows();
    visible.forEach((row, index) => {
      row.querySelector('[data-line-number]').textContent = String(index + 1);
      const button = row.querySelector('[data-remove-draft-line]');
      button.hidden = false;
      button.setAttribute('aria-label', `Убрать компонент ${index + 1}`);
    });
    add.disabled = visible.length >= 100;
    document.getElementById('catalog-draft-limit').hidden = visible.length < 100;
    window.TsehComponentPicker?.bind(document, window);
  };
  const restore = draft => {
    for (const [name, value] of Object.entries(draft.fields)) setValue(field(name), value);
    if (lines) {
      lines.replaceChildren(...draft.rows.map((row, index) => newRow(index, row)));
      field('lines-TOTAL_FORMS').value = String(draft.rows.length);
      field('lines-INITIAL_FORMS').value = '0';
    }
    pendingRequest = '';
    dirty = true;
    updateRows();
    show('Несохранённый ввод восстановлен из этой вкладки. Проверьте данные и нажмите «Сохранить черновик», чтобы записать их на сервер.');
  };
  const loaded = store.load();
  const recoveryAction = loaded.ok ? api.recoveryAction(loaded.draft, {
    revision: form.dataset.version, isPost: form.dataset.isPost === 'true', requestId: form.dataset.request,
  }) : 'none';
  if (!loaded.ok) {
    blocked = true;
    show('Локальная копия недоступна или не читается. Она не перезаписана. Сохраняйте данные кнопкой перед обновлением страницы.');
    recovery.hidden = false;
    recovery.querySelector('p').textContent = 'Браузер не смог прочитать локальную копию. Показанные поля можно сохранить на сервер. Удаление локальной копии не меняет серверные данные.';
    recovery.querySelector('[data-restore-local]').hidden = true;
    const discard = recovery.querySelector('[data-discard-local]');
    discard.querySelector('.button-label').textContent = 'Удалить локальную копию';
    discard.addEventListener('click', () => {
      if (!window.confirm('Удалить нечитаемую локальную копию? Восстановить её после удаления будет невозможно. Показанные поля и серверные данные останутся.')) return;
      if (!store.clear()) { show('Браузер не разрешает удаление локальной копии. Сохраните данные кнопкой.'); return; }
      blocked = false;
      recovery.hidden = true;
      show('Локальная копия удалена. Показанные поля и серверные данные не изменились.');
    });
  } else if (form.dataset.isPost === 'true' && (!loaded.draft || recoveryAction === 'server')) {
    // Server validation and row errors win over browser recovery after a POST.
    // Persist that displayed response without restoring an older local submission.
    dirty = true;
    originRevision = loaded.draft?.revision || originRevision;
    persist();
  } else if (loaded.draft) {
    if (recoveryAction === 'restore') restore(loaded.draft);
    else {
      blocked = true;
      recovery.hidden = false;
      show('Показана актуальная серверная версия. Решите, восстанавливать ли прежний несохранённый ввод.');
      recovery.querySelector('[data-restore-local]').addEventListener('click', () => {
        blocked = false;
        restore(loaded.draft);
        recovery.hidden = true;
        field('name').focus();
      });
      recovery.querySelector('[data-discard-local]').addEventListener('click', () => {
        if (!store.clear()) { show('Не удалось удалить локальную копию. Серверная версия сохранена.'); return; }
        blocked = false;
        recovery.hidden = true;
        show('Показана сохранённая серверная версия.');
      });
    }
  } else show('При вводе данные сохраняются в этой вкладке. «Сохранить черновик» записывает их на сервер.');
  updateRows();
  const edited = () => { dirty = true; pendingRequest = ''; persist(); };
  form.addEventListener('input', edited);
  form.addEventListener('change', edited);
  if (add) {
    add.type = 'button';
    add.addEventListener('click', () => {
      if (activeRows().length >= 100) return;
      const vacant = [...lines.querySelectorAll('.draft-edit-line')].find(row => row.hidden);
      const total = field('lines-TOTAL_FORMS');
      const index = vacant ? Number(vacant.dataset.prefix.replace('lines-', '')) : Number(total.value);
      const row = newRow(index);
      if (vacant) vacant.replaceWith(row);
      else { lines.append(row); total.value = String(index + 1); }
      updateRows(); edited();
      (row.querySelector('[role="combobox"]') || row.querySelector('select')).focus();
      document.getElementById('catalog-draft-line-status').textContent = 'Строка добавлена. Изменения ещё не сохранены на сервере.';
    });
    lines.addEventListener('click', event => {
      const button = event.target.closest('[data-remove-draft-line]');
      if (!button) return;
      const row = button.closest('.draft-edit-line');
      const deletion = row.querySelector('[name$="-DELETE"]');
      deletion.value = 'on';
      row.querySelectorAll('input, textarea, select, button').forEach(element => { if (element !== deletion) element.disabled = true; });
      row.hidden = true;
      updateRows(); edited();
      document.getElementById('catalog-draft-line-status').textContent = 'Компонент убран. Изменения ещё не сохранены на сервере.';
      (activeRows().at(-1)?.querySelector('[role="combobox"]') || add).focus();
    });
  }
  form.addEventListener('submit', () => {
    pendingRequest = window.crypto.randomUUID();
    let request = field('_draft_request');
    if (!request) { request = document.createElement('input'); request.type = 'hidden'; request.name = '_draft_request'; form.append(request); }
    request.value = pendingRequest;
    persist(); // Clear only after the server's matching successful-save receipt.
  });
  window.addEventListener('pagehide', () => { if (dirty) persist(); });
  window.addEventListener('pageshow', event => {
    if (event.persisted) { suspended = true; window.location.replace(window.location.pathname); }
  });
});
