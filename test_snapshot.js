const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const code = fs.readFileSync(path.join(__dirname, 'Snapshot.js'), 'utf8').replace(/^\.pragma library\s*/, '');
const snapshot = {};
vm.runInNewContext(code, snapshot);
const address = 'AA:BB:CC:DD:EE:FF';
const reading = {
  schema: 1, address, name: 'Aranet4 example', boot_id: 'boot', seen_boottime: 100,
  co2: 1173, temperature: 23.6, humidity: 62, pressure: 1021.1, battery: 5, sample_age: 290
};
const render = (value = reading, uptime = 105, boot = 'boot') => snapshot.render(value, address, boot, uptime);
assert.equal(render().text, 'CO₂ 1173 ppm');
assert.match(render().tooltip, /295 seconds ago/); // Five-minute samples remain visible while in range.
assert.match(render().tooltip, /23.6 °C \/ 74.5 °F/);
assert.equal(render(reading, 219.99).text, 'CO₂ 1173 ppm');
assert.equal(render(reading, 220).text, '');
assert.equal(render(reading, 9999).text, ''); // Resume after suspend.
assert.equal(render(reading, 105, 'other-boot').text, '');
assert.equal(render(reading, 99).text, '');
assert.equal(render({...reading, address: '11:22:33:44:55:66'}).text, '');
for (const co2 of [-1, 0, 32768, NaN, Infinity, '1173', true]) {
  assert.equal(render({...reading, co2}).text, '');
}
for (const raw of ['bad json', '[]', 'null', 'x'.repeat(9000)]) {
  assert.equal(snapshot.parse(raw), null);
}
assert.equal(snapshot.parse(JSON.stringify(reading)).co2, 1173);
assert.equal(render({...reading, name: '<img src=x>\n\u001b'}).tooltip.split('\n')[0], '<img src=x>');
assert.ok(!render({...reading, temperature: NaN, battery: -1}).tooltip.includes('Temperature:'));
assert.ok(!render({...reading, temperature: NaN, battery: -1}).tooltip.includes('Battery:'));

const present = {
  schema: 1, address, boot_id: 'boot', present: true, refreshing: false,
  last_attempt: 100, expires_boottime: 790
};
const absent = {...present, present: false};
assert.equal(snapshot.render(reading, address, 'boot', 600, present).text, 'CO₂ 1173 ppm');
assert.equal(snapshot.render(reading, address, 'boot', 790, present).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 105, absent).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 600, absent, true).text, 'CO₂ 1173 ppm');
assert.equal(snapshot.render(reading, address, 'boot', 600, null, true).text, 'CO₂ 1173 ppm');
assert.equal(snapshot.render(reading, address, 'other-boot', 600, present, true).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 600, {...present, expires_boottime: 1000000}).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 600, {...present, address: '11:22:33:44:55:66'}).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 105, {...present, last_attempt: 1000}).text, 'CO₂ 1173 ppm');
assert.equal(snapshot.render(reading, address, 'boot', 600, {...present, last_attempt: 1000}).text, '');
assert.equal(snapshot.render(reading, address, 'boot', 600, {...present, boot_id: 'other'}).text, '');
assert.equal(!!snapshot.validStatus({...present, present: 'yes'}, address, 'boot', 600), false);
assert.equal(!!snapshot.validStatus(present, '', '', 600), false);
console.log('Native snapshot checks passed.');
