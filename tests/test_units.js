const assert = require('node:assert/strict');
const {convert} = require('../tseh/static/tseh/units.js');
const cases = [
  ['2', 'kg', 'g', '2000'], ['2000', 'g', 'kg', '2'], ['0,004000000001', 'kg', 'g', '4,000000001'],
  ['100', 'ml', 'l', '0,1'], ['0.1', 'l', 'ml', '100'], ['1e-3', 'kg', 'g', '1'],
  ['999999999999.123456789012', 'kg', 'g', '999999999999123,456789012'],
  ['0.000000000001', 'g', 'kg', '0,000000000000001'], ['1', 'kg', 'l', null], ['n/a', 'g', 'kg', null],
  ['.5', 'kg', 'g', '500'], [',5', 'kg', 'g', '500'], ['+.5', 'kg', 'g', '500'],
];
for (const [value, from, to, expected] of cases) assert.equal(convert(value, from, to), expected);
for (const value of ['0,000000000000001', '0,004000000001', '999999999999,123456789012']) {
  assert.equal(convert(convert(value, 'kg', 'g'), 'g', 'kg'), value);
}
console.log(`${cases.length + 3} exact unit conversion checks passed`);
