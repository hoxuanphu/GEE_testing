/* Local-metre rectangle geometry and a dependency-free Leaflet editor. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RectangleROI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const METRES = 111320, MAX_LAT = 85.05112878, MAX_SIZE = 90000;
  const fields = ['center_lon', 'center_lat', 'width_m', 'height_m', 'rotation_deg'];
  function number(value) {
    return value === null || value === undefined || String(value).trim() === '' ? NaN : Number(value);
  }
  function normalize(value) { return ((value % 360) + 360) % 360; }
  function point(rect, x, y) {
    const a = normalize(rect.rotation_deg) * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
    return [rect.center_lon + (x*c + y*s) / (METRES * Math.cos(rect.center_lat * Math.PI / 180)),
      rect.center_lat + (-x*s + y*c) / METRES];
  }
  function local(rect, lon, lat) {
    const east = (lon - rect.center_lon) * METRES * Math.cos(rect.center_lat * Math.PI / 180);
    const north = (lat - rect.center_lat) * METRES;
    const a = normalize(rect.rotation_deg) * Math.PI / 180;
    return [east * Math.cos(a) - north * Math.sin(a), east * Math.sin(a) + north * Math.cos(a)];
  }
  function geometry(rect) {
    const ring = [[-1,-1], [1,-1], [1,1], [-1,1]].map(([x,y]) => point(rect, x*rect.width_m/2, y*rect.height_m/2));
    return {type: 'Polygon', coordinates: [[...ring, [...ring[0]]]]};
  }
  function validate(value) {
    if (!value) return 'Vẽ hoặc nhập hình chữ nhật trước.';
    const r = Object.fromEntries(fields.map(key => [key, number(value[key])]));
    if (!fields.every(key => Number.isFinite(r[key]))) return 'Nhập đủ tọa độ, kích thước và góc hữu hạn.';
    if (Math.abs(r.center_lon) > 180 || Math.abs(r.center_lat) > MAX_LAT) return 'Tâm nằm ngoài miền kinh/vĩ độ hỗ trợ.';
    if (r.width_m <= 0 || r.height_m <= 0 || r.width_m > MAX_SIZE || r.height_m > MAX_SIZE) return 'Chiều rộng/cao phải > 0 và ≤ 90.000 m.';
    if (r.rotation_deg < 0 || r.rotation_deg > 360) return 'Góc xoay phải trong 0–360° (chiều kim đồng hồ).';
    if (geometry(r).coordinates[0].some(([lon,lat]) => Math.abs(lon) > 180 || Math.abs(lat) > MAX_LAT)) return 'Đỉnh ROI vượt ±180° kinh độ hoặc ±85,05112878° vĩ độ; không hỗ trợ qua đường đổi ngày.';
    return '';
  }
  function snapshot(value) {
    const error = validate(value);
    if (error) throw new Error(error);
    return Object.freeze(Object.fromEntries(fields.map(key => [key, number(value[key])])));
  }
  function bbox(rect) {
    const ring = geometry(snapshot(rect)).coordinates[0];
    return [Math.min(...ring.map(p => p[0])), Math.min(...ring.map(p => p[1])), Math.max(...ring.map(p => p[0])), Math.max(...ring.map(p => p[1]))];
  }
  function body(base, rect) {
    const result = {...base, ...snapshot(rect), selection_mode: 'rectangle'};
    for (const key of ['lon', 'lat', 'radius_m', 'province_id', 'commune_id']) delete result[key];
    return result;
  }
  function query(rect) { return new URLSearchParams(body({}, rect)).toString(); }
  function draw(start, end) {
    const center_lat = (start.lat + end.lat)/2;
    return {center_lon: (start.lng + end.lng)/2, center_lat,
      width_m: Math.abs(end.lng-start.lng)*METRES*Math.cos(center_lat*Math.PI/180),
      height_m: Math.abs(end.lat-start.lat)*METRES, rotation_deg: 0};
  }
  function edit(rect, handle, start, end) {
    const result = {...rect};
    if (handle === 'move') {
      result.center_lon += end.lng-start.lng; result.center_lat += end.lat-start.lat;
    } else if (handle === 'rotate') {
      const east = (end.lng-rect.center_lon)*METRES*Math.cos(rect.center_lat*Math.PI/180);
      const north = (end.lat-rect.center_lat)*METRES;
      if (Math.hypot(east, north) > 0.001) result.rotation_deg = normalize(Math.atan2(east, north)*180/Math.PI);
    } else {
      // Symmetric resizing keeps the center and clockwise axes fixed.
      const [x,y] = local(rect, end.lng, end.lat);
      if (handle.includes('e') || handle.includes('w')) result.width_m = Math.abs(x)*2;
      if (handle.includes('n') || handle.includes('s')) result.height_m = Math.abs(y)*2;
    }
    return result;
  }

  class Controller {
    constructor({map, L, element, onChange = () => {}}) {
      this.map = map; this.L = L; this.el = element; this.onChange = onChange;
      this.active = false; this.armed = false; this.value = null; this.gesture = null;
      this.group = L.layerGroup(); this.container = map.getContainer();
      this.listeners = [];
      this.onZoom = () => { if (this.active && !this.gesture) this.render(); };
      map.on('zoomend', this.onZoom);
      this.listen(this.container, 'pointerdown', event => this.down(event), true);
      this.listen(this.container, 'pointermove', event => this.move(event), true);
      this.listen(this.container, 'pointerup', event => this.finish(event, false), true);
      this.listen(this.container, 'pointercancel', event => this.finish(event, true), true);
      this.listen(this.container, 'lostpointercapture', event => this.finish(event, true), true);
      this.listen(this.container.ownerDocument, 'visibilitychange', () => {
        if (this.container.ownerDocument.hidden) this.cancel();
      });
      this.listen(this.container.ownerDocument, 'keydown', event => {
        if (event.key === 'Escape' && this.active) { this.cancel(); event.preventDefault(); }
      });
      for (const key of fields) this.listen(element('rect_' + key), 'input', () => {
        const value = Object.fromEntries(fields.map(field => [field, number(element('rect_' + field).value)]));
        this.cancel(); this.set(value);
      });
      this.listen(element('rectangleDraw'), 'click', () => this.arm());
      this.listen(element('rectangleClear'), 'click', () => { this.cancel(); this.set(null); });
      this.listen(element('rectangleFit'), 'click', () => this.fit());
      this.render();
    }
    listen(target, event, callback, capture = false) {
      target.addEventListener(event, callback, capture);
      this.listeners.push(() => target.removeEventListener(event, callback, capture));
    }
    set(value, notify = true) {
      this.value = value ? Object.fromEntries(fields.map(key => [key, number(value[key])])) : null;
      this.render();
      if (notify) this.onChange(this.value);
    }
    get() { return snapshot(this.value); }
    setActive(active) {
      this.cancel(); this.active = active;
      this.el('rectangleSelection').hidden = !active;
      if (active) { this.group.addTo(this.map); if (!this.value) this.arm(); }
      else this.map.removeLayer(this.group);
      this.render();
    }
    suspend() {
      if (this.interactions) return;
      this.interactions = {};
      for (const key of ['dragging', 'doubleClickZoom', 'scrollWheelZoom', 'boxZoom', 'keyboard', 'touchZoom']) {
        const handler = this.map[key];
        if (handler) { this.interactions[key] = handler.enabled(); handler.disable(); }
      }
    }
    restore() {
      if (!this.interactions) return;
      for (const [key, enabled] of Object.entries(this.interactions)) {
        if (enabled) this.map[key].enable(); else this.map[key].disable();
      }
      this.interactions = null;
    }
    arm() {
      if (!this.active) return;
      this.cancel(); this.armed = true; this.suspend(); this.render();
    }
    position(event) { return this.map.mouseEventToLatLng(event); }
    down(event) {
      if (!this.active || this.gesture || event.isPrimary === false || (event.button !== undefined && event.button !== 0)) return;
      if (event.target.closest && event.target.closest('.leaflet-control')) return;
      const target = event.target.closest ? event.target.closest('[data-roi-handle]') : null;
      const handle = this.armed ? 'draw' : target && target.dataset.roiHandle;
      if (!handle || (handle !== 'draw' && validate(this.value))) return;
      event.preventDefault(); event.stopImmediatePropagation();
      this.suspend();
      this.gesture = {id: event.pointerId, handle, start: this.position(event), original: this.value && {...this.value}};
      this.container.setPointerCapture(event.pointerId);
      this.onChange(this.value);
    }
    move(event) {
      const g = this.gesture;
      if (!g || event.pointerId !== g.id) return;
      event.preventDefault(); event.stopImmediatePropagation();
      const end = this.position(event);
      this.set(g.handle === 'draw' ? draw(g.start, end) : edit(g.original, g.handle, g.start, end));
    }
    finish(event, cancelled) {
      const g = this.gesture;
      if (!g || event.pointerId !== g.id) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!cancelled) {
        const end = this.position(event);
        this.set(g.handle === 'draw' ? draw(g.start, end) : edit(g.original, g.handle, g.start, end));
      }
      this.gesture = null; this.armed = false;
      if (this.container.hasPointerCapture(g.id)) this.container.releasePointerCapture(g.id);
      this.restore();
      if (cancelled) this.set(g.original); else this.render();
    }
    cancel() {
      if (this.gesture) {
        const g = this.gesture;
        this.gesture = null;
        if (this.container.hasPointerCapture(g.id)) this.container.releasePointerCapture(g.id);
        this.set(g.original);
      }
      this.armed = false; this.restore(); this.render();
    }
    fit() {
      if (!this.active || validate(this.value)) return;
      const [w,s,e,n] = bbox(this.value);
      this.map.fitBounds([[s,w],[n,e]], {padding:[35,35], maxZoom:17});
    }
    render() {
      const error = validate(this.value), r = this.value;
      this.el('rectangleError').textContent = error;
      this.el('rectangleDraw').textContent = this.armed ? 'Kéo trên bản đồ để vẽ (Esc: hủy)' : 'Vẽ / vẽ lại hình chữ nhật';
      this.container.classList.toggle('rectangle-drawing', this.active && this.armed);
      this.el('rectangleFit').disabled = !!error;
      for (const key of fields) {
        const input = this.el('rect_' + key);
        if (input !== this.container.ownerDocument.activeElement) input.value = r && Number.isFinite(r[key]) ? String(r[key]) : !r && key === 'rotation_deg' ? '0' : '';
      }
      this.el('rectangleStats').textContent = !error
        ? `${r.width_m.toFixed(1)} × ${r.height_m.toFixed(1)} m · ${(r.width_m*r.height_m/1e6).toFixed(4)} km² · Tâm ${r.center_lon.toFixed(6)}, ${r.center_lat.toFixed(6)} · ${r.rotation_deg.toFixed(2)}°`
        : '';
      this.group.clearLayers();
      if (!this.active || error) return;
      const latlng = ([lon,lat]) => [lat,lon];
      const polygon = this.L.polygon(geometry(r).coordinates[0].map(latlng), {color:'#fbbf24', weight:2, fillOpacity:0.13, bubblingMouseEvents:false}).addTo(this.group);
      const path = polygon.getElement();
      if (path) { path.dataset.roiHandle = 'move'; path.style.cursor = 'move'; }
      const handles = {sw:[-1,-1], s:[0,-1], se:[1,-1], e:[1,0], ne:[1,1], n:[0,1], nw:[-1,1], w:[-1,0], move:[0,0]};
      for (const [name,[x,y]] of Object.entries(handles)) this.handle(name, point(r,x*r.width_m/2,y*r.height_m/2));
      const top = point(r,0,r.height_m/2);
      // Place the rotation grip 35 screen pixels beyond the north-facing edge.
      const centerPx = this.map.latLngToContainerPoint(latlng(point(r,0,0)));
      const topPx = this.map.latLngToContainerPoint(latlng(top));
      const length = Math.hypot(topPx.x-centerPx.x,topPx.y-centerPx.y) || 1;
      const grip = this.map.containerPointToLatLng([topPx.x + 35*(topPx.x-centerPx.x)/length, topPx.y + 35*(topPx.y-centerPx.y)/length]);
      this.L.polyline([latlng(top),[grip.lat,grip.lng]], {color:'#38bdf8', weight:2, interactive:false}).addTo(this.group);
      this.handle('rotate', [grip.lng,grip.lat]);
    }
    handle(name, coordinates) {
      const titles = {move:'Di chuyển ROI', rotate:'Xoay ROI'};
      this.L.marker([coordinates[1],coordinates[0]], {
        icon:this.L.divIcon({className:'rectangle-grip', iconSize:[18,18], iconAnchor:[9,9],
          html:`<span data-roi-handle="${name}" title="${titles[name] || 'Đổi kích thước (giữ tâm)'}" class="rectangle-handle ${name}">${name === 'rotate' ? '↻' : name === 'move' ? '+' : ''}</span>`}),
        interactive:true, keyboard:false, bubblingMouseEvents:false
      }).addTo(this.group);
    }
    destroy() { this.cancel(); this.map.off('zoomend', this.onZoom); this.map.removeLayer(this.group); this.listeners.forEach(remove => remove()); }
  }
  return {METRES, MAX_LAT, MAX_SIZE, fields, normalize, point, local, geometry, validate, snapshot, bbox, body, query, draw, edit, Controller};
});
