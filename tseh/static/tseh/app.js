/* Presentation only. All amounts, validation and confirmations live on the server. */
document.addEventListener('DOMContentLoaded', () => {
  const resetDialog = document.getElementById('reset-dialog');
  let resetDestination = null;
  let resetTrigger = null;
  let resetForm = null;
  const performReset = (form, destination) => {
    if (!form.tsehDraft || form.tsehDraft.clear()) window.location.assign(destination);
  };
  const requestReset = (form, link, trigger) => {
    if (form.dataset.dirty !== 'true') {
      performReset(form, link.href);
      return;
    }
    if (!resetDialog || typeof resetDialog.showModal !== 'function') {
      if (window.confirm(link.dataset.confirmMessage || 'Сбросить изменения? Несохранённые изменения будут потеряны.')) performReset(form, link.href);
      return;
    }
    resetDestination = link.href;
    resetTrigger = trigger;
    resetForm = form;
    const isRecipe = form.id === 'recipe-form';
    document.getElementById('reset-dialog-title').textContent = isRecipe ? 'Сбросить черновик?' : 'Сбросить изменения?';
    document.getElementById('reset-dialog-message').textContent = isRecipe
      ? 'Все несохранённые поля, состав и предварительный расчёт будут удалены. Откроется исходная форма.'
      : 'Несохранённые изменения будут потеряны. Форма загрузится заново.';
    const confirmLabel = document.querySelector('#reset-confirm .button-label') || document.getElementById('reset-confirm');
    confirmLabel.textContent = isRecipe ? 'Сбросить черновик' : 'Сбросить изменения';
    resetDialog.showModal();
    document.getElementById('reset-cancel').focus();
  };
  document.querySelectorAll('form[data-draft-submitted]').forEach(form => {
    form.dataset.dirty = form.dataset.draftSubmitted === 'true' ? 'true' : 'false';
    const markDirty = () => { form.dataset.dirty = 'true'; };
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);
    form.querySelectorAll('details.reset-action').forEach(details => {
      const summary = details.querySelector('summary');
      const link = details.querySelector('[data-reset-form]');
      if (!summary || !link) return;
      summary.addEventListener('click', event => {
        event.preventDefault();
        requestReset(form, link, summary);
      });
      link.addEventListener('click', event => {
        event.preventDefault();
        requestReset(form, link, summary);
      });
    });
  });
  if (resetDialog) {
    document.getElementById('reset-cancel').addEventListener('click', () => resetDialog.close());
    document.getElementById('reset-confirm').addEventListener('click', () => {
      if (resetDestination) {
        performReset(resetForm, resetDestination);
        resetDialog.close();
      }
    });
    resetDialog.addEventListener('close', () => {
      resetTrigger?.focus();
      resetDestination = null;
      resetTrigger = null;
      resetForm = null;
    });
  }
  // Restore before unit handlers capture their original units or bind dynamic rows.
  window.TsehDraftEditor?.init(window, document);
  const bindUnits = () => {
    window.TsehRecipeUnits.bind(document, window);
    window.TsehComponentPicker?.bind(document, window);
  };
  bindUnits();
  const recipe = document.getElementById('recipe-form');
  const lines = document.getElementById('recipe-lines');
  const add = document.getElementById('add-line');
  const lineStatus = document.getElementById('line-status');
  const updateLineActions = () => {
    if (!lines) return;
    const visible = [...lines.querySelectorAll('.recipe-line')].filter(row => !row.hidden);
    visible.forEach((row, index) => {
      row.querySelector('.remove-line').setAttribute('aria-label', `Удалить компонент ${index + 1}`);
    });
    add.disabled = visible.length >= 100;
    document.getElementById('line-limit').hidden = visible.length < 100;
    document.getElementById('line-empty').hidden = visible.length > 0;
  };
  const markRecipeChanged = () => recipe.dispatchEvent(new Event('input'));
  if (lines) lines.addEventListener('click', event => {
    const button = event.target.closest('.remove-line');
    if (!button) return;
    event.preventDefault();
    const row = button.closest('.recipe-line');
    const deletion = row.querySelector('input[name$="-DELETE"]');
    deletion.value = 'on';
    row.querySelectorAll('input, select, textarea, button').forEach(field => {
      if (field !== deletion) field.disabled = true;
    });
    row.hidden = true;
    updateLineActions();
    lineStatus.textContent = 'Компонент убран из состава. Изменения ещё не сохранены.';
    markRecipeChanged();
    const remaining = [...lines.querySelectorAll('.recipe-line')].filter(item => !item.hidden);
    const next = remaining.find(item => row.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_FOLLOWING) || remaining.at(-1);
    (next?.querySelector('[role="combobox"]') || next?.querySelector('select') || add).focus();
  });
  if (add) add.addEventListener('click', event => {
    event.preventDefault();
    const total = document.getElementById('id_form-TOTAL_FORMS');
    const count = Number(total.value);
    const vacant = [...lines.querySelectorAll('.recipe-line')].find(row => row.hidden);
    if (!vacant && count >= 100) { updateLineActions(); return; }
    const index = vacant ? Number(vacant.dataset.prefix.replace('form-', '')) : count;
    const template = document.getElementById('empty-line').innerHTML.replaceAll('__prefix__', String(index));
    if (vacant) {
      vacant.insertAdjacentHTML('beforebegin', template);
      vacant.remove();
    } else {
      lines.insertAdjacentHTML('beforeend', template);
      total.value = count + 1;
    }
    bindUnits();
    updateLineActions();
    lineStatus.textContent = 'Добавлена строка компонента.';
    (document.getElementById(`id_form-${index}-component-search`) || document.getElementById(`id_form-${index}-component`)).focus();
    markRecipeChanged();
  });
  updateLineActions();
  const invalidatePreview = () => {
    const save = recipe.querySelector('button[value="save"]');
    if (save) { save.disabled = true; (save.querySelector('.button-label') || save).textContent = 'Сначала пересчитайте'; }
    const stale = document.getElementById('preview-stale');
    if (stale) stale.hidden = false;
  };
  if (recipe) {
    recipe.addEventListener('input', invalidatePreview);
    recipe.addEventListener('change', invalidatePreview);
  }
  const thresholdValues = ['id_green', 'id_orange', 'id_green_value', 'id_orange_value']
    .map(id => document.getElementById(id)).filter(Boolean);
  const updateThresholdFields = () => {
    for (const name of ['green', 'orange']) {
      const mode = document.getElementById(`id_${name}_mode`);
      const value = document.getElementById(`id_${name}_value`);
      if (mode && value) value.disabled = mode.value !== 'custom';
    }
    // Warning only; authoritative validation and all financial arithmetic stay on the server.
    const over = thresholdValues.some(field => !field.disabled && Number(field.value.replace(',', '.')) > 100);
    let warning = document.getElementById('threshold-input-warning');
    if (!warning && thresholdValues.length) {
      warning = document.createElement('p');
      warning.id = 'threshold-input-warning';
      warning.className = 'notice';
      warning.setAttribute('role', 'status');
      warning.textContent = 'Порог выше 100%: блюдо может попасть в зелёную или оранжевую зону, даже если его себестоимость превышает отпускную цену после скидки.';
      thresholdValues[0].closest('.form-grid').after(warning);
    }
    if (warning) warning.hidden = !over;
  };
  thresholdValues.forEach(field => field.addEventListener('input', updateThresholdFields));
  ['green', 'orange'].forEach(name => document.getElementById(`id_${name}_mode`)?.addEventListener('change', updateThresholdFields));
  updateThresholdFields();
  document.querySelectorAll('[data-scenario-row]').forEach(row => {
    const mode = row.querySelector('select[name$="-mode"]');
    const updateMode = () => {
      row.querySelectorAll('[data-scenario-mode]').forEach(group => {
        group.hidden = group.dataset.scenarioMode !== mode.value;
        if (!group.hidden && group.tagName === 'DETAILS') group.open = true;
      });
    };
    mode.addEventListener('change', updateMode);
    updateMode();
  });
  document.querySelectorAll('form[data-preview-form]').forEach(form => {
    const invalidate = () => {
      const confirm = form.querySelector('button[value="confirm"]');
      if (!confirm) return;
      confirm.disabled = true;
      (confirm.querySelector('.button-label') || confirm).textContent = 'Сначала пересчитайте';
      const stale = form.querySelector('#price-preview-stale');
      if (stale) stale.hidden = false;
    };
    form.addEventListener('input', invalidate);
    form.addEventListener('change', invalidate);
  });
  const firstError = document.querySelector('.errorlist');
  const result = firstError || document.getElementById('preview') || document.getElementById('price-preview');
  if (result) {
    result.setAttribute('tabindex', '-1');
    result.focus();
  }
});
