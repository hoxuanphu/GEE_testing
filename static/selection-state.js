(() => {
  'use strict';
  const copy = value => value == null ? null : JSON.parse(JSON.stringify(value));
  let reference = null, manual = null, source = null;
  const platforms = {s1gee:null, sentinel2:null};
  globalThis.SelectionState = Object.freeze({
    setReferenceDraft(data) { reference = copy(data); },
    getReferenceDraft() { return copy(reference); },
    activateReferenceRoi() { source = reference ? 'reference' : null; return source; },
    activateManualRoi() { source = 'manual'; },
    getSource() { return source; },
    clearReferenceDraft() { reference = null; if (source === 'reference') source = null; },
    setManualDraft(data) { manual = copy(data); },
    getManualDraft() { return copy(manual); },
    setPlatform(dataset, value) {
      const allowed = dataset === 's1gee' ? ['A','B','C','D'] : dataset === 'sentinel2' ? ['A','B','C'] : [];
      if (value !== null && !allowed.includes(value)) throw new Error('Vệ tinh không hợp lệ với nguồn dữ liệu.');
      platforms[dataset] = value;
    },
    getPlatform(dataset) { return platforms[dataset] ?? null; }
  });
})();
