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

// Execute the service's actual JavaScript methods with observable properties.
// This catches transient invalid states before a later clock read repairs them.
const serviceSource = fs.readFileSync(path.join(__dirname, 'Service.qml'), 'utf8');
function serviceFunction(name) {
  const start = serviceSource.indexOf('    function ' + name + '(');
  assert.ok(start >= 0, 'Missing Service.qml function: ' + name);
  const opening = serviceSource.indexOf('{', start);
  let depth = 0, quote = '', escaped = false, lineComment = false, blockComment = false;
  for (let i = opening; i < serviceSource.length; i++) {
    const c = serviceSource[i], next = serviceSource[i + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && next === '/') { blockComment = false; i++; } continue; }
    if (quote) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === quote) quote = '';
      continue;
    }
    if (c === '/' && next === '/') { lineComment = true; i++; continue; }
    if (c === '/' && next === '*') { blockComment = true; i++; continue; }
    if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return serviceSource.slice(start, i + 1);
  }
  throw new Error('Unterminated Service.qml function: ' + name);
}

function serviceHarness() {
  const state = { snapshot: reading, status: present, uptime: 600 };
  const service = {
    Snapshot: snapshot, bootId: 'boot', destroying: false,
    settings: { address, pythonPath: '/example/venv/bin/python', cachePath: '/example/reading.json' },
    pendingSnapshot: null, pendingStatus: null, pendingClockRetries: 0,
    lastError: 'previous error', trace: [], followups: [], reloads: 0,
    setupBusy: false, dependenciesReady: true, configured: true,
    setupState: { phase: 'ready' },
    checkSetup() {}, reconcile() {}, setSetupPhase() {},
  };
  Object.defineProperty(service, 'address', { get: () => service.settings.address });
  Object.defineProperty(service, 'desiredSignature', {
    get: () => [service.settings.address, service.settings.pythonPath, service.settings.cachePath].join('|'),
  });
  service.uptimeFile = { reload() { service.reloads++; } };
  service.Qt = { callLater(callback) { service.followups.push(callback); } };
  for (const property of ['snapshot', 'status', 'uptime']) {
    Object.defineProperty(service, property, {
      get: () => state[property],
      set(value) {
        state[property] = value;
        service.trace.push({
          cached: snapshot.render(state.snapshot, service.address, service.bootId, state.uptime, state.status, true).text,
          display: snapshot.render(state.snapshot, service.address, service.bootId, state.uptime, state.status).text,
          validStatus: !!snapshot.validStatus(state.status, service.address, service.bootId, state.uptime),
        });
      },
    });
  }
  vm.createContext(service);
  for (const name of ['acceptLine', 'clearPending', 'applyPending', 'updateUptime', 'unavailable', 'configure']) {
    vm.runInContext(serviceFunction(name), service);
  }
  return service;
}

const refreshing = { ...present, refreshing: true, last_attempt: 600.004 };
const freshReading = { ...reading, co2: 1300, seen_boottime: 600.006 };
const event = (type, value) => JSON.stringify({ type, [type]: value });

{
  const service = serviceHarness();
  service.acceptLine(event('status', refreshing));
  service.acceptLine(event('snapshot', freshReading));
  assert.equal(service.status, present, 'Keep displayed status until a current clock is available');
  assert.equal(service.snapshot, reading, 'Keep displayed measurement until a current clock is available');
  assert.equal(service.uptime, 600, 'An event must never advance the clock itself');
  assert.equal(service.reloads, 2);
  service.updateUptime(600); // /proc rounds down below both full-precision timestamps.
  assert.equal(service.snapshot.co2, 1300);
  assert.equal(service.status.refreshing, true);
  assert.equal(service.uptime, 600.01);
  assert.ok(service.trace.every(frame => frame.cached !== '' && frame.display !== '' && frame.validStatus),
    'No intermediate property update may blank a valid reading or status');
  assert.equal(service.pendingSnapshot, null);
  assert.equal(service.pendingStatus, null);
  service.updateUptime(599.99);
  assert.equal(service.uptime, 600.01, 'A late clock callback must not move time backward');
}

{
  const service = serviceHarness();
  service.acceptLine(event('snapshot', { ...freshReading, seen_boottime: 600.05 }));
  service.updateUptime(600); // This read began before the newest event arrived.
  assert.equal(service.snapshot, reading);
  assert.equal(service.followups.length, 1);
  service.followups.shift()();
  assert.equal(service.reloads, 2, 'Request one newer clock read for an in-flight-read race');
  service.updateUptime(600);
  assert.equal(service.followups.length, 0, 'An unresolved future timestamp must not create a polling loop');
  service.updateUptime(600.05);
  assert.equal(service.snapshot.co2, 1300);
  assert.ok(service.trace.every(frame => frame.cached !== ''));
}

{
  const service = serviceHarness();
  service.acceptLine(event('snapshot', { ...freshReading, seen_boottime: 1000000 }));
  service.acceptLine(event('status', { ...refreshing, last_attempt: 1000000 }));
  service.updateUptime(600);
  assert.equal(service.snapshot, reading);
  assert.equal(service.status, present);
  assert.equal(service.followups.length, 0, 'Absurd future data must not drive extra clock reads');
  assert.equal(service.lastError, 'previous error');
  service.acceptLine(event('snapshot', { ...freshReading, address: '11:22:33:44:55:66' }));
  service.acceptLine(event('status', { ...refreshing, boot_id: 'other-boot' }));
  assert.equal(service.reloads, 2, 'Foreign identity must be rejected before requesting a clock read');
}

{
  const service = serviceHarness();
  service.acceptLine(event('snapshot', freshReading));
  service.acceptLine(event('status', refreshing));
  service.unavailable();
  service.updateUptime(600);
  assert.equal(service.pendingSnapshot, null);
  assert.equal(service.pendingStatus, null);
  assert.equal(service.snapshot, reading, 'Worker restart must not publish an old queued measurement');
  assert.equal(service.status.present, false);
  assert.equal(snapshot.render(service.snapshot, address, 'boot', service.uptime, service.status, true).text, 'CO₂ 1173 ppm');
}

{
  const service = serviceHarness();
  service.acceptLine(event('snapshot', freshReading));
  service.acceptLine(event('status', refreshing));
  service.configure({ ...service.settings, pythonPath: '/example/new-venv/bin/python' });
  assert.equal(service.pendingSnapshot, null, 'Changing worker configuration clears queued data');
  assert.equal(service.pendingStatus, null);
  service.pendingSnapshot = { ...freshReading, address: '11:22:33:44:55:66' };
  service.pendingStatus = { ...refreshing, boot_id: 'old-boot' };
  service.applyPending();
  assert.equal(service.pendingSnapshot, null);
  assert.equal(service.pendingStatus, null);
  assert.equal(service.snapshot, reading);
  assert.equal(service.status, present);
}

console.log('Native snapshot and service clock checks passed.');
