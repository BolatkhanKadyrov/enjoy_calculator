/* Local recipe drafts; calculations and permission to save remain server-owned. */
(function (root) {
  'use strict';
  const FIELD_NAMES = ['name', 'base_price', 'output_quantity', 'output_unit', 'output_input_unit',
    'green_mode', 'green_value', 'orange_mode', 'orange_value'];
  const ROW_NAMES = ['component', 'quantity', 'unit', 'quantity_unit'];

  function init(win, doc) {
    const api = win.TsehDraftStore;
    if (!api) return;
    let storage;
    try { storage = win.sessionStorage; } catch (_) { storage = null; }
    const receiptNode = doc.getElementById('draft-saved-receipt');
    if (receiptNode) {
      try {
        const receipt = JSON.parse(receiptNode.textContent);
        const store = api.createStore(storage, receipt.user, receipt.path);
        const loaded = store.load();
        if (loaded.ok && loaded.draft?.submittedRequest === receipt.request) {
          if (!store.clear().ok) {
            const warning = doc.createElement('p');
            warning.className = 'notice';
            warning.textContent = 'Рецептура сохранена в базе, но браузер не смог удалить локальный черновик. При повторном открытии формы сбросьте его.';
            doc.getElementById('main').prepend(warning);
          }
        }
        const url = new URL(win.location.href);
        url.searchParams.delete('draft_saved');
        win.history.replaceState(null, '', url.pathname + url.search + url.hash);
      } catch (_) { /* A malformed receipt cannot clear a draft. */ }
    }

    const form = doc.getElementById('recipe-form');
    if (!form) return;
    const lines = doc.getElementById('recipe-lines');
    const status = doc.getElementById('draft-status');
    const template = doc.getElementById('empty-line');
    const store = api.createStore(storage, form.dataset.draftUser, win.location.pathname);
    let originRevision = form.dataset.draftRevision;
    let originEpoch = form.dataset.draftEpoch;
    let pendingRequest = '';
    let suspended = false;
    let blocked = false;
    let unavailableComponent = false;
    const field = name => form.elements.namedItem(name);
    const show = (text, error = false) => {
      if (status.textContent !== text) status.textContent = text;
      status.dataset.state = error ? 'error' : 'saved';
    };
    const warning = () => {
      let text = '';
      if (originRevision !== form.dataset.draftRevision || originEpoch !== form.dataset.draftEpoch) {
        text += ' Исходные данные изменились. Проверьте состав и пересчитайте перед сохранением.';
      }
      if (unavailableComponent) text += ' Один из компонентов больше недоступен. Замените или удалите его.';
      return text;
    };
    const capture = () => ({
      fields: Object.fromEntries(FIELD_NAMES.filter(name => field(name)).map(name => [name, field(name).value])),
      rows: [...lines.querySelectorAll('.recipe-line')].filter(row => !row.hidden).map(row => {
        const value = name => row.querySelector(`[name="${row.dataset.prefix}-${name}"]`);
        const data = Object.fromEntries(ROW_NAMES.map(name => [name, value(name)?.value || '']));
        data.componentLabel = value('component')?.selectedOptions[0]?.textContent || '';
        return data;
      }),
      originRevision, originEpoch, submittedRequest: pendingRequest,
    });
    const persist = () => {
      if (suspended) return false;
      if (blocked || !store.save(capture()).ok) {
        show('Браузер не смог сохранить черновик. Не обновляйте и не закрывайте страницу: текущий ввод может потеряться. Попробуйте рассчитать и сохранить рецептуру.', true);
        return false;
      }
      show('Черновик сохранён в этой вкладке. В базу изменения ещё не записаны.' + warning());
      return true;
    };
    const setValue = (element, value, label) => {
      if (!element || typeof value !== 'string') return;
      if (element.tagName === 'SELECT' && value && ![...element.options].some(option => option.value === value)) {
        const option = doc.createElement('option');
        option.value = value;
        option.textContent = (label || value) + ' — недоступен';
        element.append(option);
        unavailableComponent = true;
      }
      element.value = value;
    };
    const restore = draft => {
      for (const [name, value] of Object.entries(draft.fields)) setValue(field(name), value);
      const fragment = doc.createDocumentFragment();
      draft.rows.forEach((row, index) => {
        const holder = doc.createElement('template');
        // Only the trusted server template is parsed as HTML. Draft values use DOM properties.
        holder.innerHTML = template.innerHTML.replaceAll('__prefix__', String(index));
        for (const name of ROW_NAMES) {
          setValue(holder.content.querySelector(`[name="form-${index}-${name}"]`), row[name], row.componentLabel);
        }
        fragment.append(holder.content);
      });
      lines.replaceChildren(fragment);
      field('form-TOTAL_FORMS').value = String(draft.rows.length);
      field('form-INITIAL_FORMS').value = '0';
      originRevision = draft.originRevision || '';
      originEpoch = draft.originEpoch || '';
      // A restored edit never carries approval for the currently rendered preview.
      if (field('preview_token')) field('preview_token').value = '';
      const save = form.querySelector('button[value="save"]');
      if (save) { save.disabled = true; (save.querySelector('.button-label') || save).textContent = 'Сначала пересчитайте'; }
      const stale = doc.getElementById('preview-stale');
      if (stale) stale.hidden = false;
      form.dataset.dirty = 'true';
      show('Черновик восстановлен. Проверьте данные и рассчитайте перед сохранением.' + warning());
    };
    const loaded = store.load();
    if (!loaded.ok) {
      blocked = true; // Preserve an unreadable/unsupported copy until explicit reset.
      form.dataset.dirty = 'true';
      show('Не удалось прочитать локальный черновик. Он не перезаписан. Не обновляйте страницу с новым вводом; сохраните рецептуру или явно сбросьте черновик.', true);
    } else if (api.shouldRestore(loaded.draft, {
      isPost: form.dataset.draftSubmitted === 'true', requestId: form.dataset.draftRequest,
    })) {
      restore(loaded.draft);
    } else if (form.dataset.draftSubmitted === 'true') {
      // This is the response to our latest submit, including server-side unit normalization.
      // On a later F5 repeating this POST, the local copy wins over the older POST body.
      // Django renders an invalid choice without a selected option. Keep its label/value
      // visible until the user replaces it, without discarding the server's row errors.
      if (loaded.draft) {
        [...lines.querySelectorAll('.recipe-line')].filter(row => !row.hidden).forEach((row, index) => {
          const selected = row.querySelector(`[name="${row.dataset.prefix}-component"]`);
          const saved = loaded.draft.rows[index];
          if (selected && !selected.value && saved?.component &&
              ![...selected.options].some(option => option.value === saved.component)) {
            setValue(selected, saved.component, saved.componentLabel);
          }
        });
      }
      if (loaded.draft && !field('preview_token')?.value) {
        originRevision = loaded.draft.originRevision || '';
        originEpoch = loaded.draft.originEpoch || '';
      }
      persist();
    } else {
      show('Черновик сохраняется в этой вкладке при вводе. Его можно восстановить после F5; закрытие вкладки может удалить черновик.');
    }
    const edited = () => {
      form.dataset.dirty = 'true';
      pendingRequest = '';
      persist();
    };
    form.addEventListener('input', edited);
    form.addEventListener('change', edited);
    form.addEventListener('submit', () => {
      if (typeof win.crypto.randomUUID === 'function') pendingRequest = win.crypto.randomUUID();
      else {
        const bytes = win.crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
        pendingRequest = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      }
      let requestField = field('_draft_request');
      if (!requestField) {
        requestField = doc.createElement('input');
        requestField.type = 'hidden';
        requestField.name = '_draft_request';
        form.append(requestField);
      }
      requestField.value = pendingRequest;
      persist(); // Never clear on submit: validation, network or authorization may fail.
    });
    win.addEventListener('pagehide', () => {
      if (form.dataset.dirty === 'true') persist();
    });
    win.addEventListener('pageshow', event => {
      if (event.persisted) {
        // A cached editor may predate a confirmed save. Read the current draft and server
        // token through GET, without writing that stale cached DOM back into storage.
        suspended = true;
        win.location.replace(win.location.pathname);
      }
    });
    const refresh = form.querySelector('[data-refresh-draft]');
    refresh.hidden = false;
    refresh.addEventListener('click', () => {
      pendingRequest = '';
      if (persist()) win.location.assign(win.location.pathname);
    });
    form.tsehDraft = {
      clear() {
        if (!store.clear().ok) {
          show('Браузер не смог удалить черновик. Сброс отменён; данные в форме сохранены.', true);
          return false;
        }
        suspended = true;
        return true;
      },
    };
  }
  root.TsehDraftEditor = {init};
})(typeof globalThis !== 'undefined' ? globalThis : this);
