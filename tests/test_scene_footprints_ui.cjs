const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

test('scene rows highlight only their own footprint and reset on page replacement', () => {
  const layers = [];
  const group = {addTo() { return this; }, clearLayers() { layers.length = 0; }};
  const L = {
    featureGroup: () => group,
    geoJSON(geometry, options) {
      return {
        geometry, style: options.style,
        addTo() { layers.push(this); return this; },
        setStyle(style) { this.style = style; },
        bringToFront() {},
      };
    },
  };
  const file = path.resolve(__dirname, '../static/scene-footprints.js');
  assert.ok(fs.existsSync(file), 'the catalogue needs a footprint controller');
  const window = {};
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {window});
  const controller = new window.SceneFootprints.Controller({createPane: () => ({style: {}})}, L);
  function addScene(lon) {
    const row = new EventTarget();
    const button = new EventTarget();
    const attrs = new Map();
    button.setAttribute = (name, value) => attrs.set(name, value);
    row.contains = target => target === button;
    controller.bind(row, button, {type: 'Polygon', coordinates: [
      [[lon, 20], [lon + 1, 20], [lon + 1, 21], [lon, 20]],
    ]});
    return {row, button, attrs};
  }
  const first = addScene(105);
  const second = addScene(106);
  const normal = layers[0].style.color;

  second.row.dispatchEvent(new Event('pointerenter'));
  assert.equal(layers[0].style.color, normal);
  assert.notEqual(layers[1].style.color, normal);
  second.row.dispatchEvent(new Event('pointerleave'));
  assert.equal(layers[1].style.color, normal);

  first.row.dispatchEvent(new Event('focusin'));
  assert.notEqual(layers[0].style.color, normal);
  first.button.dispatchEvent(new Event('click'));
  first.row.dispatchEvent(new Event('focusout'));
  assert.equal(first.attrs.get('aria-pressed'), 'true');
  assert.notEqual(layers[0].style.color, normal);
  second.button.dispatchEvent(new Event('click'));
  assert.equal(first.attrs.get('aria-pressed'), 'false');
  assert.equal(layers[0].style.color, normal);
  assert.notEqual(layers[1].style.color, normal);

  controller.clear();
  assert.equal(layers.length, 0);
  assert.equal(second.attrs.get('aria-pressed'), 'false');
  assert.equal(addScene(107).attrs.get('aria-pressed'), 'false');
  assert.equal(layers[0].style.color, normal);
});
