const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function state() {
  const context = vm.createContext({});
  const file = path.join(__dirname, '../static/selection-state.js');
  if (fs.existsSync(file)) vm.runInContext(fs.readFileSync(file, 'utf8'), context);
  return context.SelectionState;
}
test('reference draft keeps exact polygon and grid independently from manual ROI', () => {
  const s = state(); assert.ok(s, 'selection state controller is available');
  const draft = {reference_id:'eqc', metadata:{transform:[10,2,1,3,-10,2]}, footprint_geojson:{coordinates:[[[104,21.7],[104.1,21.8],[104,21.7]]]}, grid_options:{grid_mode:'match_grid'}};
  s.setReferenceDraft(draft); draft.metadata.transform[0] = 999;
  s.activateReferenceRoi(); assert.equal(s.getSource(), 'reference');
  s.setManualDraft({width_m:1200}); s.activateManualRoi();
  assert.equal(s.getManualDraft().width_m,1200);
  assert.equal(s.getReferenceDraft().metadata.transform[0],10);
  const copy=s.getReferenceDraft(); copy.grid_options.grid_mode='bad';
  s.activateReferenceRoi(); assert.equal(s.getReferenceDraft().grid_options.grid_mode,'match_grid');
  s.clearReferenceDraft(); assert.equal(s.getSource(),null); assert.equal(s.getManualDraft().width_m,1200);
});
test('dataset platform values are retained separately and ALL is null', () => {
  const s=state(); assert.ok(s, 'selection state controller is available');
  s.setPlatform('s1gee','D'); s.setPlatform('sentinel2','C');
  assert.equal(s.getPlatform('s1gee'),'D'); assert.equal(s.getPlatform('sentinel2'),'C');
  assert.throws(() => s.setPlatform('sentinel2','D'));
  s.setPlatform('sentinel2',null); assert.equal(s.getPlatform('sentinel2'),null);
});
