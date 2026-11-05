/* Progressive enhancement: the native select remains the submitted source of truth. */
(function (root) {
  'use strict';
  function bind(doc, win) {
    doc.querySelectorAll('select[id$="-component"]').forEach(select => {
      if (select.dataset.pickerBound) return;
      select.dataset.pickerBound = '1';
      const wrapper = doc.createElement('div');
      wrapper.className = 'component-picker';
      select.parentNode.insertBefore(wrapper, select);
      wrapper.appendChild(select);
      const input = doc.createElement('input');
      input.type = 'text';
      input.id = select.id + '-search';
      input.autocomplete = 'off';
      input.placeholder = 'Найти компонент…';
      input.required = select.required;
      input.disabled = select.disabled;
      input.setAttribute('role', 'combobox');
      input.setAttribute('aria-autocomplete', 'list');
      input.setAttribute('aria-expanded', 'false');
      for (const attr of ['aria-describedby', 'aria-invalid']) {
        if (select.hasAttribute(attr)) input.setAttribute(attr, select.getAttribute(attr));
      }
      const labels = Array.from(select.labels || []);
      labels.forEach(label => { label.htmlFor = input.id; });
      const popup = doc.createElement('div');
      popup.className = 'component-options';
      popup.id = select.id + '-options';
      popup.setAttribute('role', 'listbox');
      if (labels[0]) popup.setAttribute('aria-label', labels[0].textContent.trim());
      input.setAttribute('aria-controls', popup.id);
      const status = doc.createElement('p');
      status.className = 'component-search-status';
      status.setAttribute('role', 'status');
      wrapper.appendChild(input);
      const menu = doc.createElement('div');
      menu.className = 'component-menu';
      menu.appendChild(popup);
      menu.appendChild(status);
      wrapper.appendChild(menu);
      select.hidden = true;
      select.required = false;
      const entries = Array.from(select.options).filter(option => option.value !== '').map((option, index) => ({
        value: option.value, text: option.label || option.textContent,
        group: option.parentNode?.tagName === 'OPTGROUP' ? option.parentNode : null,
        disabled: option.disabled || (option.parentNode?.tagName === 'OPTGROUP' && option.parentNode.disabled),
        id: select.id + '-option-' + index,
      }));
      let visible = [], active = -1, opened = false;
      const sync = () => {
        input.value = select.value ? (select.selectedOptions[0]?.label || select.selectedOptions[0]?.textContent || '') : '';
        input.setCustomValidity(input.required && !select.value ? 'Выберите компонент из списка.' : '');
      };
      function close() {
        opened = false;
        menu.hidden = true;
        popup.hidden = true;
        status.hidden = true;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        active = -1;
        sync();
      }
      function activate(index) {
        active = index;
        visible.forEach((entry, i) => { entry.button.classList.toggle('is-active', i === index); });
        if (index >= 0) {
          input.setAttribute('aria-activedescendant', visible[index].id);
          visible[index].button.scrollIntoView?.({block: 'nearest'});
        } else input.removeAttribute('aria-activedescendant');
      }
      function choose(entry) {
        if (entry.disabled) return;
        close();
        select.value = entry.value;
        select.dispatchEvent(new win.Event('change', {bubbles: true}));
        // Earlier unit listeners may reject the change and restore the previous value.
        sync();
      }
      function open(query = '') {
        opened = true;
        const bounds = input.getBoundingClientRect?.();
        wrapper.classList.toggle('opens-up', Boolean(bounds && win.innerHeight - bounds.bottom < 240 && bounds.top > 240));
        popup.replaceChildren();
        const needle = query.trim().toLocaleLowerCase('ru');
        visible = entries.filter(entry => entry.text.toLocaleLowerCase('ru').includes(needle));
        let group = null, container = popup;
        visible.forEach(entry => {
          if (entry.group !== group) {
            group = entry.group;
            container = popup;
            if (group) {
              container = doc.createElement('div');
              container.setAttribute('role', 'group');
              container.setAttribute('aria-label', group.label);
              const heading = doc.createElement('div');
              heading.className = 'component-group-label';
              heading.textContent = group.label;
              container.appendChild(heading);
              popup.appendChild(container);
            }
          }
          const button = doc.createElement('button');
          button.type = 'button';
          button.tabIndex = -1;
          button.id = entry.id;
          button.setAttribute('role', 'option');
          button.setAttribute('aria-selected', String(entry.value === select.value));
          button.disabled = Boolean(entry.disabled);
          button.textContent = entry.text;
          button.addEventListener('pointerdown', event => event.preventDefault());
          button.addEventListener('click', () => choose(entry));
          container.appendChild(button);
          entry.button = button;
        });
        activate(needle ? visible.findIndex(entry => !entry.disabled) : -1);
        menu.hidden = false;
        popup.hidden = false;
        status.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        status.textContent = visible.length ? 'Найдено: ' + visible.length : 'Компоненты не найдены.';
      }
      input.addEventListener('focus', () => { open(); input.select?.(); });
      input.addEventListener('click', () => { if (!opened) open(); });
      input.addEventListener('input', event => { event.stopPropagation(); open(input.value); });
      input.addEventListener('change', event => event.stopPropagation());
      input.addEventListener('blur', close);
      input.addEventListener('keydown', event => {
        if (event.isComposing) return;
        if (event.key === 'Escape') { if (opened) event.preventDefault(); close(); return; }
        if (event.key === 'Tab') { close(); return; }
        if (event.key === 'Enter' && opened) {
          event.preventDefault();
          if (active >= 0) choose(visible[active]);
          return;
        }
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        if (!opened && ['Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        if (!opened) open();
        const indices = visible.map((entry, i) => entry.disabled ? -1 : i).filter(i => i >= 0);
        if (!indices.length) return;
        const position = indices.indexOf(active);
        let next;
        if (event.key === 'Home') next = indices[0];
        else if (event.key === 'End') next = indices[indices.length - 1];
        else if (event.key === 'ArrowDown') next = indices[Math.min(position + 1, indices.length - 1)];
        else next = indices[position < 0 ? indices.length - 1 : Math.max(position - 1, 0)];
        activate(next);
      });
      select.addEventListener('change', sync);
      close();
    });
  }
  root.TsehComponentPicker = {bind};
})(typeof globalThis !== 'undefined' ? globalThis : this);
