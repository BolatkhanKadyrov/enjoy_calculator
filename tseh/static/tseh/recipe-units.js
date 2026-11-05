/* Unit suggestions are presentation only. The server validates every component/quantity pair. */
(function (root) {
  'use strict';
  function bind(doc, win) {
    const units = win.TsehUnits;
    const labels = {g: 'г', kg: 'кг', ml: 'мл', l: 'л', pc: 'шт'};
    const changed = () => doc.getElementById('recipe-form')?.dispatchEvent(new win.Event('input'));
    doc.querySelectorAll('select[id$="-unit"], select#id_output_unit').forEach(select => {
      if (select.dataset.unitBound) return;
      select.dataset.unitBound = '1';
      let previous = select.value;
      const quantityId = select.id === 'id_output_unit' ? 'id_output_quantity' : select.id.replace(/-unit$/, '-quantity');
      const quantity = doc.getElementById(quantityId);
      const basisId = quantityId === 'id_output_quantity' ? 'id_output_input_unit' : quantityId.replace(/-quantity$/, '-quantity_unit');
      const basis = doc.getElementById(basisId);
      select.tsehSetUnit = (target, automatic = false) => {
        if (!Object.hasOwn(labels, target)) return false;
        if (target === previous) return true;
        const value = quantity?.value || '';
        const filled = value.trim() !== '';
        const compatible = units.compatible(previous, target);
        let converted = filled && compatible ? units.convert(value, previous, target) : null;
        if (filled && compatible && converted === null) {
          select.value = previous;
          quantity.setCustomValidity('Перед сменой компонента или единицы введите корректное количество.');
          quantity.reportValidity();
          quantity.addEventListener('input', () => quantity.setCustomValidity(''), {once: true});
          return false;
        }
        if (filled && !compatible && automatic) {
          if (!win.confirm(`Для нового компонента количество указывается в ${labels[target]}. Сейчас введено ${value} ${labels[previous]}. Сменить компонент и очистить количество?`)) return false;
          converted = '';
        }
        if (converted !== null) quantity.value = converted;
        select.value = target;
        if (basis) basis.value = target;
        previous = target;
        quantity?.setCustomValidity('');
        return true;
      };
      // Persist only the completed conversion, never new-unit/old-quantity intermediate input.
      select.addEventListener('input', event => event.stopPropagation());
      select.addEventListener('change', event => {
        event.stopPropagation();
        if (select.tsehSetUnit(select.value)) changed();
      });
    });
    doc.querySelectorAll('select[id$="-component"]').forEach(select => {
      if (select.dataset.componentUnitBound) return;
      select.dataset.componentUnitBound = '1';
      let previous = select.value;
      select.addEventListener('input', event => event.stopPropagation());
      select.addEventListener('change', event => {
        event.stopPropagation();
        if (select.value === previous) return;
        const target = select.selectedOptions[0]?.dataset.unit;
        const unit = doc.getElementById(select.id.replace(/-component$/, '-unit'));
        if (target && unit?.tsehSetUnit && !unit.tsehSetUnit(target, true)) {
          select.value = previous;
          return;
        }
        previous = select.value;
        // One notification after component, quantity, visible unit and hidden basis agree.
        changed();
      });
    });
  }
  root.TsehRecipeUnits = {bind};
})(typeof globalThis !== 'undefined' ? globalThis : this);
