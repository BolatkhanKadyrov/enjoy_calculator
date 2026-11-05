/* Exact text conversion: never pass a quantity through a binary floating-point number. */
(function (root) {
  const units = {g: ['mass', -3], kg: ['mass', 0], ml: ['volume', -3], l: ['volume', 0], pc: ['count', 0]};
  const compatible = (from, to) => Boolean(units[from] && units[to] && units[from][0] === units[to][0]);
  function convert(value, from, to) {
    if (!compatible(from, to)) return null;
    const text = String(value).replace(/[\s\u00a0]/g, '').replace(',', '.').replace(/^\./, '0.').replace(/^\+\./, '+0.');
    const match = text.match(/^\+?(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
    if (!match) return null;
    const exponent = Number(match[3] || '0');
    if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) return null;
    const digits = match[1] + (match[2] || '');
    const point = match[1].length + exponent + units[from][1] - units[to][1];
    let result;
    if (point <= 0) result = '0.' + '0'.repeat(-point) + digits;
    else if (point >= digits.length) result = digits + '0'.repeat(point - digits.length);
    else result = digits.slice(0, point) + '.' + digits.slice(point);
    let [whole, fraction = ''] = result.split('.');
    whole = whole.replace(/^0+(?=\d)/, '');
    fraction = fraction.replace(/0+$/, '');
    return whole + (fraction ? ',' + fraction : '');
  }
  root.TsehUnits = {convert, compatible};
  if (typeof module !== 'undefined') module.exports = {convert, compatible};
})(globalThis);
