(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const preset = get('s2BandPreset');
  const bands = ['AOT', 'WVP', 'SCL', 'TCI_R', 'TCI_G', 'TCI_B', 'MSK_CLDPRB', 'MSK_SNWPRB',
    'QA10', 'QA20', 'QA60', 'MSK_CLASSI_OPAQUE', 'MSK_CLASSI_CIRRUS', 'MSK_CLASSI_SNOW_ICE'];
  const checkboxes = bands.map(band => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.id = `s2_aux_${band}`; input.value = band; input.checked = true;
    label.htmlFor = input.id;
    const name = document.createElement('span'); name.textContent = band;
    label.append(input, name);
    get('s2AuxiliaryBands').appendChild(label);
    return input;
  });
  function updateCount() {
    get('s2AuxiliaryCount').textContent = `${checkboxes.filter(input => input.checked).length}/14 lớp phụ`;
  }
  checkboxes.forEach(input => input.addEventListener('change', updateCount));
  function selectAll(checked) {
    checkboxes.forEach(input => { input.checked = checked; });
    // Use the same invalidation path as an individual checkbox change.
    checkboxes[0].dispatchEvent(new Event('change', {bubbles:true}));
  }
  get('s2AuxiliaryAll').addEventListener('click', () => selectAll(true));
  get('s2AuxiliaryNone').addEventListener('click', () => selectAll(false));
  function updatePreset() {
    get('s2AuxiliaryOptions').hidden = preset.value === 'rgbnir';
  }
  preset.addEventListener('change', updatePreset);
  function updateVisibility() {
    const mode = get('selection_mode').value;
    const matched = mode === 'reference' || mode === 'rectangle' && window.SelectionState?.getSource() === 'reference';
    get('s2AuxiliarySelection').hidden = get('dataset').value !== 'sentinel2' || matched;
  }
  window.S2Layers = Object.freeze({
    request: () => {
      if (get('dataset').value !== 'sentinel2') return {};
      if (preset.value === 'rgbnir') return {s2_band_preset:'rgbnir'};
      return {s2_auxiliary_bands: checkboxes.filter(input => input.checked).map(input => input.value)};
    },
    updateVisibility
  });
  updateCount();
  updatePreset();
  updateVisibility();
})();
