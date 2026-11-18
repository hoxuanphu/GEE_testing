const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/s2-layers.js', 'utf8');
const html = fs.readFileSync('static/index.html', 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));

function fixture() {
  const elements = new Map();
  class Element {
    constructor() { this.children = []; this.listeners = {}; this.value = ''; }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.append(node); }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); }
  }
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('dataset').value = 'sentinel2'; get('selection_mode').value = 'point';
  const context = {document:{getElementById:get, createElement:() => new Element()}, window:{}, Event};
  vm.createContext(context); vm.runInContext(source, context);
  return {get, api:context.window.S2Layers, context, inputs:get('s2AuxiliaryBands').children.map(label => label.children[0])};
}

test('14 labelled layers default on; individual and bulk changes emit updated export selection', () => {
  const f = fixture();
  assert.equal(f.inputs.length, 14);
  assert.equal(f.api.request().s2_auxiliary_bands.length, 14);
  for (const label of f.get('s2AuxiliaryBands').children) assert.equal(label.htmlFor, label.children[0].id);
  let changes = 0;
  f.inputs[0].addEventListener('change', () => changes++);
  f.get('s2AuxiliaryNone').dispatchEvent(new Event('click'));
  assert.deepEqual(plain(f.api.request()), {s2_auxiliary_bands:[]});
  assert.equal(f.get('s2AuxiliaryCount').textContent, '0/14 lớp phụ');
  f.inputs[2].checked = true; f.inputs[2].dispatchEvent(new Event('change'));
  assert.deepEqual(plain(f.api.request()), {s2_auxiliary_bands:['SCL']});
  f.get('s2AuxiliaryAll').dispatchEvent(new Event('click'));
  assert.equal(f.api.request().s2_auxiliary_bands.length, 14);
  assert.equal(changes, 2);
});

test('selection survives dataset/mode changes and controls hide for other sources and matched-frame', () => {
  const f = fixture();
  f.get('s2AuxiliaryNone').dispatchEvent(new Event('click'));
  f.get('dataset').value = 'landsat'; f.api.updateVisibility();
  assert.equal(f.get('s2AuxiliarySelection').hidden, true);
  assert.deepEqual(plain(f.api.request()), {});
  f.get('dataset').value = 'sentinel2'; f.get('selection_mode').value = 'reference'; f.api.updateVisibility();
  assert.equal(f.get('s2AuxiliarySelection').hidden, true);
  f.get('selection_mode').value = 'rectangle';
  f.context.window.SelectionState = {getSource:() => 'reference'}; f.api.updateVisibility();
  assert.equal(f.get('s2AuxiliarySelection').hidden, true);
  f.context.window.SelectionState.getSource = () => 'manual'; f.api.updateVisibility();
  assert.equal(f.get('s2AuxiliarySelection').hidden, false);
  assert.deepEqual(plain(f.api.request()), {s2_auxiliary_bands:[]});
});

test('RGB + NIR requests four bands and restores custom layers after switching back', () => {
  const f = fixture();
  f.get('s2AuxiliaryNone').dispatchEvent(new Event('click'));
  f.inputs[2].checked = true;
  f.inputs[2].dispatchEvent(new Event('change'));
  const preset = f.get('s2BandPreset');
  preset.value = 'rgbnir';
  preset.dispatchEvent(new Event('change'));
  assert.deepEqual(plain(f.api.request()), {s2_band_preset:'rgbnir'});
  assert.equal(f.get('s2AuxiliaryOptions').hidden, true);
  preset.value = 'custom';
  preset.dispatchEvent(new Event('change'));
  assert.equal(f.get('s2AuxiliaryOptions').hidden, false);
  assert.deepEqual(plain(f.api.request()), {s2_auxiliary_bands:['SCL']});
});

test('optical scene request passes explicit empty selection, retains geometry and ignores non-S2 datasets', () => {
  const f = fixture();
  f.context.$ = f.get;
  f.context.rotationValue = () => 0;
  for (const id of ['start_date', 'end_date']) f.get(id).value = '2026-01-01';
  const start = html.indexOf('function opticalSceneBody(');
  const end = html.indexOf('let pendingCompositeBody', start);
  const balanceStart = html.indexOf('function colorBalanceAvailable(');
  const balanceEnd = html.indexOf('function renderCompositePlan(', balanceStart);
  vm.runInContext(html.slice(balanceStart, balanceEnd), f.context);
  vm.runInContext(html.slice(start, end), f.context);
  f.get('s2AuxiliaryNone').dispatchEvent(new Event('click'));
  const body = plain(f.context.opticalSceneBody('point', '', '', 107, 10));
  assert.deepEqual(body.s2_auxiliary_bands, []);
  assert.equal(body.lon, 107); assert.equal(body.lat, 10);
  f.get('dataset').value = 'landsat';
  assert.equal('s2_auxiliary_bands' in f.context.opticalSceneBody('point', '', '', 107, 10), false);
});
