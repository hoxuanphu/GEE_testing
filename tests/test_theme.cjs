const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '..', 'static', 'theme.js'), 'utf8');

function fixture({ saved = null, dark = false, blocked = false } = {}) {
  const events = {};
  const select = { value: '', addEventListener: (name, handler) => { events[`select:${name}`] = handler; } };
  const media = { matches: dark, addEventListener: (name, handler) => { events[`media:${name}`] = handler; } };
  const root = { dataset: {} };
  const writes = [];
  const context = {
    window: { matchMedia: () => media, addEventListener: (name, handler) => { events[`window:${name}`] = handler; } },
    document: {
      documentElement: root,
      getElementById: () => select,
      addEventListener: (name, handler) => { events[`document:${name}`] = handler; },
    },
    localStorage: {
      getItem() { if (blocked) throw new Error('Storage blocked'); return saved; },
      setItem(key, value) { if (blocked) throw new Error('Storage blocked'); writes.push({ key, value }); },
    },
  };
  vm.runInNewContext(script, context);
  return { root, media, select, writes, ready: () => events['document:DOMContentLoaded'](),
    change(value) { select.value = value; events['select:change'](); },
    system(dark) { media.matches = dark; events['media:change'](); },
    resume(dark) { media.matches = dark; events['document:visibilitychange'](); },
    storage(key, newValue) { events['window:storage']({ key, newValue }); },
  };
}

test('applies saved theme before DOM readiness and restores selector', () => {
  const f = fixture({ saved: 'dark' });
  assert.equal(f.root.dataset.theme, 'dark');
  f.ready();
  assert.equal(f.select.value, 'dark');
});

test('system is the default and follows OS changes', () => {
  const f = fixture({ dark: true });
  assert.equal(f.root.dataset.theme, 'dark');
  f.ready();
  assert.equal(f.select.value, 'system');
  f.system(false);
  assert.equal(f.root.dataset.theme, 'light');
});

test('manual choice persists and ignores OS changes until system is selected', () => {
  const f = fixture();
  f.ready();
  f.change('dark');
  assert.equal(f.root.dataset.theme, 'dark');
  assert.deepEqual(f.writes[0], { key: 'satellite-workspace-theme', value: 'dark' });
  f.system(false);
  assert.equal(f.root.dataset.theme, 'dark');
  f.change('system');
  assert.equal(f.root.dataset.theme, 'light');
  f.system(true);
  assert.equal(f.root.dataset.theme, 'dark');
});

test('returning to a background tab resynchronizes system but preserves manual mode', () => {
  const f = fixture();
  f.ready();
  f.resume(true);
  assert.equal(f.root.dataset.theme, 'dark');
  f.change('light');
  f.resume(true);
  assert.equal(f.root.dataset.theme, 'light');
});

test('invalid stored preference falls back to system', () => {
  const f = fixture({ saved: 'invalid', dark: false });
  f.ready();
  assert.equal(f.select.value, 'system');
  assert.equal(f.root.dataset.theme, 'light');
});

test('blocked storage does not prevent switching', () => {
  const f = fixture({ blocked: true });
  f.ready();
  f.change('dark');
  assert.equal(f.root.dataset.theme, 'dark');
});

test('storage events synchronize preference across tabs and clearing restores system', () => {
  const f = fixture();
  f.ready();
  f.storage('unrelated', 'dark');
  assert.equal(f.root.dataset.theme, 'light');
  f.storage('satellite-workspace-theme', 'dark');
  assert.equal(f.root.dataset.theme, 'dark');
  assert.equal(f.select.value, 'dark');
  f.storage(null, null);
  assert.equal(f.select.value, 'system');
  assert.equal(f.root.dataset.theme, 'light');
});
