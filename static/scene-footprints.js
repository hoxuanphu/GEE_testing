(() => {
  'use strict';

  class Controller {
    constructor(map, L) {
      this.map = map;
      this.L = L;
      // Keep scene boundaries below the existing ROI and its editing handles.
      map.createPane('scene-footprints').style.zIndex = '390';
      this.layers = L.featureGroup().addTo(map);
      this.hovered = this.focused = this.selected = null;
    }

    bind(row, button, footprint) {
      if (!['Polygon', 'MultiPolygon'].includes(footprint?.type) || !footprint.coordinates?.length) return false;
      const layer = this.L.geoJSON(footprint, {
        pane: 'scene-footprints', interactive: false, style: this.style(false, false),
      }).addTo(this.layers);
      const scene = {layer, button};
      button.setAttribute('aria-pressed', 'false');
      row.addEventListener('pointerenter', () => this.setState('hovered', scene));
      row.addEventListener('pointerleave', () => this.setState('hovered', null));
      row.addEventListener('focusin', () => this.setState('focused', scene));
      row.addEventListener('focusout', event => {
        if (!row.contains(event.relatedTarget)) this.setState('focused', null);
      });
      button.addEventListener('click', () => this.setState('selected', this.selected === scene ? null : scene));
      return true;
    }

    style(active, selected) {
      return {
        color: active || selected ? '#22c55e' : '#2563eb',
        weight: selected ? 4 : active ? 3 : 2,
        opacity: active || selected ? 1 : 0.8,
        fill: false,
      };
    }

    setState(field, scene) {
      const previous = this[field];
      if (previous === scene) return;
      const oldActive = this.hovered || this.focused;
      this[field] = scene;
      const active = this.hovered || this.focused;
      for (const entry of new Set([previous, oldActive, active, this.selected])) {
        if (!entry) continue;
        const selected = entry === this.selected;
        entry.layer.setStyle(this.style(entry === active, selected));
        entry.button.setAttribute('aria-pressed', String(selected));
      }
      const foreground = active || this.selected;
      foreground?.layer.bringToFront();
    }

    clear() {
      this.selected?.button.setAttribute('aria-pressed', 'false');
      this.hovered = this.focused = this.selected = null;
      this.layers.clearLayers();
    }

    fit(roi) {
      const bounds = this.layers.getBounds();
      if (!bounds.isValid()) return;
      if (roi && [roi.south, roi.west, roi.north, roi.east].every(Number.isFinite)) {
        bounds.extend([roi.south, roi.west]);
        bounds.extend([roi.north, roi.east]);
      }
      this.map.invalidateSize({pan: false});
      this.map.fitBounds(bounds, {padding: [24, 24], maxZoom: 12, animate: false});
    }
  }

  window.SceneFootprints = Object.freeze({Controller});
})();
