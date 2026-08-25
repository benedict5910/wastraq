/* =====================================================================
   WQMap - a small slippy map on a 2-D canvas.

   Why not MapLibre GL JS: it would have to come from a CDN, and this
   dashboard has to work on a municipal laptop with restricted egress. This
   engine ships with the backend, has no dependencies, and degrades to a
   clean vector-only view when the OSM tile server is unreachable. The layer
   API below is deliberately GeoJSON-shaped, so swapping in MapLibre later
   is a renderer change, not a data change.

   Supports: pan, wheel/pinch zoom, raster basemap, polygon/line/point
   layers, hover tooltips, click hit-testing, fitTo(bounds), and drawing
   tools for point / line / polygon used by the field survey screen.
   ===================================================================== */
(function (global) {
  'use strict';

  const TILE = 256;
  const MAXZ = 19, MINZ = 2;      // OSM raster tops out at 19; we over-zoom past it
const MAXVIEW = 21;
  const RAD = Math.PI / 180;

  function lon2x(lon, s) { return (lon + 180) / 360 * s; }
  function lat2y(lat, s) {
    const t = Math.max(-85.05112878, Math.min(85.05112878, lat)) * RAD;
    return (1 - Math.log(Math.tan(t) + 1 / Math.cos(t)) / Math.PI) / 2 * s;
  }
  function x2lon(x, s) { return x / s * 360 - 180; }
  function y2lat(y, s) {
    const n = Math.PI - 2 * Math.PI * y / s;
    return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  }

  function create(container, opts) {
    const o = Object.assign({
      center: [12.29433, 76.64148], zoom: 17, tiles: true, minZoom: MINZ, maxZoom: MAXVIEW,
      attribution: '© OpenStreetMap contributors'
    }, opts || {});

    container.classList.add('map-shell');
    const cv = document.createElement('canvas');
    container.appendChild(cv);
    const ctx = cv.getContext('2d');

    const tip = document.createElement('div');
    tip.className = 'map-tip';
    container.appendChild(tip);

    const scaleEl = document.createElement('div');
    scaleEl.className = 'map-scale';
    container.appendChild(scaleEl);

    if (o.attribution) {
      const at = document.createElement('div');
      at.className = 'map-attr';
      at.textContent = o.attribution;
      container.appendChild(at);
    }

    const ctl = document.createElement('div');
    ctl.className = 'map-ctl';
    ctl.innerHTML = '<button data-z="1" data-tip="Zoom in">+</button>' +
                    '<button data-z="-1" data-tip="Zoom out">−</button>' +
                    '<button data-z="fit" data-tip="Fit to data">⤢</button>';
    container.appendChild(ctl);

    const st = {
      lat: o.center[0], lon: o.center[1], zoom: o.zoom,
      w: 0, h: 0, dpr: 1,
      layers: [], tileCache: new Map(), tilesOk: true, tilesTried: 0, tilesFailed: 0,
      hover: null, selected: null, draw: null, edit: null, listeners: {}, lastBounds: null,
      showTiles: o.tiles !== false
    };

    // ------------------------------------------------------------ sizing --
    function resize() {
      const r = container.getBoundingClientRect();
      st.dpr = Math.min(2, global.devicePixelRatio || 1);
      st.w = Math.max(1, Math.round(r.width));
      st.h = Math.max(1, Math.round(r.height));
      cv.width = Math.round(st.w * st.dpr);
      cv.height = Math.round(st.h * st.dpr);
      cv.style.width = st.w + 'px';
      cv.style.height = st.h + 'px';
      draw();
    }
    const ro = (global.ResizeObserver) ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(container); else global.addEventListener('resize', resize);

    // -------------------------------------------------------- projection --
    function scale() { return TILE * Math.pow(2, st.zoom); }
    function project(lat, lon) {
      const s = scale();
      return [lon2x(lon, s) - lon2x(st.lon, s) + st.w / 2,
              lat2y(lat, s) - lat2y(st.lat, s) + st.h / 2];
    }
    function unproject(px, py) {
      const s = scale();
      return [y2lat(lat2y(st.lat, s) + py - st.h / 2, s),
              x2lon(lon2x(st.lon, s) + px - st.w / 2, s)];
    }

    // ------------------------------------------------------------- tiles --
    function tileUrl(z, x, y) {
      const sub = ['a', 'b', 'c'][(x + y) % 3];
      return 'https://' + sub + '.tile.openstreetmap.org/' + z + '/' + x + '/' + y + '.png';
    }
    function getTile(z, x, y) {
      const k = z + '/' + x + '/' + y;
      if (st.tileCache.has(k)) return st.tileCache.get(k);
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.decoding = 'async';
      const rec = { img: img, ok: false };
      st.tilesTried++;
      img.onload = () => { rec.ok = true; draw(); };
      img.onerror = () => {
        st.tilesFailed++;
        // If nothing loads at all, stop asking - offline machines shouldn't
        // spend the session retrying a blocked host.
        if (st.tilesFailed >= 6 && st.tilesFailed === st.tilesTried) {
          st.tilesOk = false;
          draw();
        }
      };
      img.src = tileUrl(z, x, y);
      st.tileCache.set(k, rec);
      if (st.tileCache.size > 400) {
        const first = st.tileCache.keys().next().value;
        st.tileCache.delete(first);
      }
      return rec;
    }
    function drawTiles() {
      if (!st.showTiles || !st.tilesOk) return;
      const z = Math.round(st.zoom);
      const zc = Math.max(0, Math.min(MAXZ, z));
      const s = TILE * Math.pow(2, zc);
      const k = scale() / s;                       // fractional-zoom stretch
      const cx = lon2x(st.lon, s), cy = lat2y(st.lat, s);
      const halfW = st.w / 2 / k, halfH = st.h / 2 / k;
      const x0 = Math.floor((cx - halfW) / TILE), x1 = Math.floor((cx + halfW) / TILE);
      const y0 = Math.floor((cy - halfH) / TILE), y1 = Math.floor((cy + halfH) / TILE);
      const n = Math.pow(2, zc);
      ctx.imageSmoothingEnabled = true;
      for (let ty = y0; ty <= y1; ty++) {
        if (ty < 0 || ty >= n) continue;
        for (let tx = x0; tx <= x1; tx++) {
          const wx = ((tx % n) + n) % n;
          const rec = getTile(zc, wx, ty);
          if (!rec.ok) continue;
          const px = (tx * TILE - cx) * k + st.w / 2;
          const py = (ty * TILE - cy) * k + st.h / 2;
          ctx.drawImage(rec.img, px, py, TILE * k + 0.6, TILE * k + 0.6);
        }
      }
      // dark themes: knock the basemap back so data reads on top of it
      if (document.documentElement.getAttribute('data-theme') !== 'light') {
        ctx.save();
        ctx.globalCompositeOperation = 'multiply';
        ctx.fillStyle = 'rgba(30,36,44,0.62)';
        ctx.fillRect(0, 0, st.w, st.h);
        ctx.restore();
      }
    }

    // ------------------------------------------------------------ layers --
    function coordsOf(f) {
      const g = f.geometry || {};
      if (g.type === 'Point') return [g.coordinates];
      if (g.type === 'LineString') return g.coordinates;
      if (g.type === 'Polygon') return g.coordinates[0] || [];
      if (g.type === 'MultiPolygon') return (g.coordinates[0] || [])[0] || [];
      return [];
    }
    function styleOf(layer, f) {
      const base = { fill: null, stroke: null, width: 1.4, radius: 4, alpha: 1, dash: null };
      return Object.assign(base, typeof layer.style === 'function' ? layer.style(f) : (layer.style || {}));
    }

    // A layer's data may be a FeatureCollection, a bare Feature, or an array of
    // Features - callers build all three shapes and none of them is wrong.
    function featuresOf(data) {
      if (!data) return [];
      if (Array.isArray(data)) return data;
      if (data.features) return data.features;
      if (data.type === 'Feature') return [data];
      return [];
    }

    function drawLayers() {
      st.layers.forEach(layer => {
        if (layer.visible === false || !layer.data) return;
        const feats = featuresOf(layer.data);
        feats.forEach(f => {
          const g = f.geometry;
          if (!g) return;
          const s = styleOf(layer, f);
          const isSel = st.selected && st.selected.layer === layer.id && st.selected.id === f.id;
          const isHov = st.hover && st.hover.layer === layer.id && st.hover.id === f.id;
          ctx.save();
          ctx.globalAlpha = s.alpha;
          if (g.type === 'Point') {
            const p = project(g.coordinates[1], g.coordinates[0]);
            if (p[0] < -30 || p[0] > st.w + 30 || p[1] < -30 || p[1] > st.h + 30) { ctx.restore(); return; }
            const r = (isSel ? s.radius + 3 : isHov ? s.radius + 1.5 : s.radius);
            if (isSel) {
              ctx.beginPath(); ctx.arc(p[0], p[1], r + 5, 0, 7);
              ctx.fillStyle = s.fill || '#888'; ctx.globalAlpha = 0.22; ctx.fill();
              ctx.globalAlpha = s.alpha;
            }
            ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 7);
            ctx.fillStyle = s.fill || '#888'; ctx.fill();
            ctx.lineWidth = isSel ? 2.4 : 1.4;
            ctx.strokeStyle = s.stroke || 'rgba(255,255,255,0.85)'; ctx.stroke();
          } else {
            const ring = coordsOf(f);
            if (!ring.length) { ctx.restore(); return; }
            ctx.beginPath();
            for (let i = 0; i < ring.length; i++) {
              const p = project(ring[i][1], ring[i][0]);
              if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
            }
            if (g.type === 'Polygon' || g.type === 'MultiPolygon') {
              ctx.closePath();
              if (s.fill) {
                ctx.fillStyle = s.fill;
                ctx.globalAlpha = s.alpha * (isSel ? 0.85 : isHov ? 0.7 : 0.55);
                ctx.fill();
                ctx.globalAlpha = s.alpha;
              }
            }
            if (s.dash) ctx.setLineDash(s.dash);
            ctx.lineWidth = (isSel ? s.width + 1.6 : isHov ? s.width + 0.8 : s.width);
            ctx.strokeStyle = s.stroke || s.fill || '#888';
            ctx.stroke();
          }
          ctx.restore();
        });
      });
    }

    // ----------------------------------------------------------- drawing --
    function drawSketch() {
      if (!st.draw) return;
      const pts = st.draw.points;
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#3987e5';
      ctx.save();
      ctx.strokeStyle = accent; ctx.fillStyle = accent; ctx.lineWidth = 2;
      if (pts.length) {
        ctx.beginPath();
        pts.forEach((c, i) => {
          const p = project(c[1], c[0]);
          if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
        });
        if (st.draw.cursor && st.draw.kind !== 'point') {
          const p = project(st.draw.cursor[1], st.draw.cursor[0]);
          ctx.lineTo(p[0], p[1]);
        }
        if (st.draw.kind === 'polygon' && pts.length > 1) ctx.closePath();
        if (st.draw.kind === 'polygon') {
          ctx.globalAlpha = 0.2; ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.setLineDash([5, 4]); ctx.stroke(); ctx.setLineDash([]);
        pts.forEach((c, i) => {
          const p = project(c[1], c[0]);
          ctx.beginPath(); ctx.arc(p[0], p[1], 5, 0, 7);
          ctx.fillStyle = i === 0 ? '#fff' : accent; ctx.fill();
          ctx.lineWidth = 2; ctx.strokeStyle = accent; ctx.stroke();
        });
      }
      ctx.restore();
    }

    // ------------------------------------------------- edit (vertex drag) --
    // A finished shape stays adjustable until the surveyor submits: the
    // handles below are the same coordinates that will be sent to PostGIS, so
    // "editable after drawing" is a property of the data, not a UI illusion.
    function drawEditHandles() {
      if (!st.edit) return;
      const accent = cssVar('--accent', '#3987e5');
      const warn = cssVar('--warn', '#d9a441');
      const pts = st.edit.coords;
      ctx.save();
      // outline of the shape being edited
      if (pts.length > 1) {
        ctx.beginPath();
        pts.forEach((c, i) => {
          const p = project(c[1], c[0]);
          if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
        });
        if (st.edit.kind === 'polygon') ctx.closePath();
        ctx.strokeStyle = accent; ctx.lineWidth = 2;
        ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);
      }
      pts.forEach((c, i) => {
        const p = project(c[1], c[0]);
        const hot = st.edit.hoverIndex === i || st.edit.dragIndex === i;
        ctx.beginPath(); ctx.arc(p[0], p[1], hot ? 9 : 6.5, 0, 7);
        ctx.fillStyle = hot ? warn : '#fff';
        ctx.fill();
        ctx.lineWidth = 2.5; ctx.strokeStyle = accent; ctx.stroke();
      });
      ctx.restore();
    }

    function cssVar(name, dflt) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || dflt;
    }

    // index of the vertex handle under the cursor, or -1
    function handleAt(px, py) {
      if (!st.edit) return -1;
      for (let i = 0; i < st.edit.coords.length; i++) {
        const p = project(st.edit.coords[i][1], st.edit.coords[i][0]);
        if ((p[0] - px) ** 2 + (p[1] - py) ** 2 <= 144) return i;   // 12 px
      }
      return -1;
    }

    // ------------------------------------------------------------- scale --
    function drawScaleBar() {
      const s = scale();
      const mPerPx = 156543.03392 * Math.cos(st.lat * RAD) / Math.pow(2, st.zoom);
      const targets = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
      let best = targets[0];
      for (const t of targets) { if (t / mPerPx <= 140) best = t; }
      scaleEl.textContent = (best >= 1000 ? (best / 1000) + ' km' : best + ' m');
      scaleEl.style.borderBottom = '2px solid var(--ink-3)';
      scaleEl.style.minWidth = Math.round(best / mPerPx) + 'px';
      scaleEl.style.textAlign = 'center';
    }

    let raf = null;
    function draw() {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        if (!st.w) return;
        ctx.setTransform(st.dpr, 0, 0, st.dpr, 0, 0);
        const bg = getComputedStyle(document.documentElement).getPropertyValue('--surface-2').trim() || '#1b1f23';
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, st.w, st.h);
        drawTiles();
        drawLayers();
        drawSketch();
        drawEditHandles();
        drawScaleBar();
      });
    }

    // ------------------------------------------------------- interaction --
    function pick(px, py) {
      for (let i = st.layers.length - 1; i >= 0; i--) {
        const layer = st.layers[i];
        if (!layer.pickable || layer.visible === false || !layer.data) continue;
        const feats = featuresOf(layer.data);
        for (let j = feats.length - 1; j >= 0; j--) {
          const f = feats[j], g = f.geometry;
          if (!g) continue;
          if (g.type === 'Point') {
            const p = project(g.coordinates[1], g.coordinates[0]);
            const r = (styleOf(layer, f).radius || 4) + 4;
            if ((p[0] - px) ** 2 + (p[1] - py) ** 2 <= r * r) return { layer: layer.id, id: f.id, feature: f };
          } else if (g.type === 'Polygon' || g.type === 'MultiPolygon') {
            const ring = coordsOf(f).map(c => project(c[1], c[0]));
            if (pointInRing(px, py, ring)) return { layer: layer.id, id: f.id, feature: f };
          } else if (g.type === 'LineString') {
            const ring = coordsOf(f).map(c => project(c[1], c[0]));
            for (let k = 0; k < ring.length - 1; k++) {
              if (segDist(px, py, ring[k], ring[k + 1]) < 6) return { layer: layer.id, id: f.id, feature: f };
            }
          }
        }
      }
      return null;
    }
    function pointInRing(x, y, ring) {
      let inside = false;
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
        if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
      }
      return inside;
    }
    function segDist(px, py, a, b) {
      const dx = b[0] - a[0], dy = b[1] - a[1];
      const L = dx * dx + dy * dy;
      const t = L ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / L)) : 0;
      return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
    }

    let dragging = false, moved = 0, lastX = 0, lastY = 0;
    function localPos(e) {
      const r = cv.getBoundingClientRect();
      const t = e.touches ? e.touches[0] : e;
      return [t.clientX - r.left, t.clientY - r.top];
    }

    cv.addEventListener('mousedown', (e) => {
      if (st.draw) return;
      const p0 = localPos(e);
      // A vertex under the cursor wins over panning the map, otherwise the
      // shape would slide away the moment you tried to nudge a corner.
      const h = handleAt(p0[0], p0[1]);
      if (h >= 0) {
        st.edit.dragIndex = h;
        e.preventDefault();
        draw();
        return;
      }
      dragging = true; moved = 0;
      lastX = p0[0]; lastY = p0[1];
      cv.classList.add('dragging');
    });
    global.addEventListener('mouseup', () => {
      dragging = false; cv.classList.remove('dragging');
      if (st.edit && st.edit.dragIndex >= 0) {
        st.edit.dragIndex = -1;
        draw();
        if (st.edit.cb) st.edit.cb(editGeoJSON());
      }
    });
    cv.addEventListener('mousemove', (e) => {
      const p = localPos(e);
      if (st.edit && st.edit.dragIndex >= 0) {
        moveVertex(st.edit.dragIndex, p[0], p[1]);
        return;
      }
      if (st.edit && !dragging && !st.draw) {
        const h = handleAt(p[0], p[1]);
        if (h !== st.edit.hoverIndex) { st.edit.hoverIndex = h; draw(); }
        if (h >= 0) { cv.style.cursor = 'grab'; tip.classList.remove('on'); return; }
      }
      if (dragging) {
        const dx = p[0] - lastX, dy = p[1] - lastY;
        moved += Math.abs(dx) + Math.abs(dy);
        lastX = p[0]; lastY = p[1];
        const s = scale();
        st.lon = x2lon(lon2x(st.lon, s) - dx, s);
        st.lat = y2lat(lat2y(st.lat, s) - dy, s);
        draw();
        return;
      }
      if (st.draw) { st.draw.cursor = unproject(p[0], p[1]).slice().reverse(); draw(); return; }
      const hit = pick(p[0], p[1]);
      const changed = JSON.stringify(hit && [hit.layer, hit.id]) !== JSON.stringify(st.hover && [st.hover.layer, st.hover.id]);
      st.hover = hit;
      if (hit && st.tipFn) {
        const txt = st.tipFn(hit.feature, hit.layer);
        if (txt) {
          tip.innerHTML = txt;
          tip.style.left = p[0] + 'px'; tip.style.top = p[1] + 'px';
          tip.classList.add('on');
        } else tip.classList.remove('on');
      } else tip.classList.remove('on');
      cv.style.cursor = st.draw ? 'crosshair' : (hit ? 'pointer' : 'grab');
      if (changed) draw();
    });
    cv.addEventListener('mouseleave', () => { tip.classList.remove('on'); st.hover = null; draw(); });

    cv.addEventListener('click', (e) => {
      const p = localPos(e);
      if (st.draw) {
        const ll = unproject(p[0], p[1]);        // [lat, lon]
        st.draw.points.push([ll[1], ll[0]]);     // store as [lon, lat]
        if (st.draw.kind === 'point') { finishDraw(); return; }
        draw();
        // Tell the page a vertex landed. Without this the surrounding UI has
        // no way to know how many points exist mid-draw, and buttons like
        // "Undo last point" / "Finish line" stay stale until the shape ends.
        emit('drawprogress', st.draw.kind, st.draw.points.length);
        return;
      }
      if (moved > 4) return;
      const hit = pick(p[0], p[1]);
      emit('click', hit ? hit.feature : null, hit ? hit.layer : null, unproject(p[0], p[1]));
    });
    cv.addEventListener('dblclick', (e) => {
      e.preventDefault();
      if (st.draw && st.draw.kind !== 'point') finishDraw();
    });

    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = localPos(e);
      const before = unproject(p[0], p[1]);
      const dz = -Math.sign(e.deltaY) * (e.ctrlKey ? 0.6 : 0.42);
      st.zoom = Math.max(o.minZoom, Math.min(o.maxZoom, st.zoom + dz));
      const after = unproject(p[0], p[1]);
      st.lat += before[0] - after[0];
      st.lon += before[1] - after[1];
      draw();
    }, { passive: false });

    // touch: one finger pans, two fingers zoom
    let pinch = null;
    cv.addEventListener('touchstart', (e) => {
      if (e.touches.length === 2) {
        pinch = { d: touchDist(e), zoom: st.zoom };
      } else {
        const p = localPos(e);
        // same rule as the mouse: a finger on a vertex edits, it does not pan
        const h = handleAt(p[0], p[1]);
        if (h >= 0) { st.edit.dragIndex = h; draw(); return; }
        lastX = p[0]; lastY = p[1]; dragging = true; moved = 0;
      }
    }, { passive: true });
    cv.addEventListener('touchmove', (e) => {
      if (pinch && e.touches.length === 2) {
        const d = touchDist(e);
        st.zoom = Math.max(o.minZoom, Math.min(o.maxZoom, pinch.zoom + Math.log2(d / pinch.d)));
        draw(); e.preventDefault(); return;
      }
      if (st.edit && st.edit.dragIndex >= 0) {
        const p = localPos(e);
        moveVertex(st.edit.dragIndex, p[0], p[1]);
        e.preventDefault();
        return;
      }
      if (!dragging) return;
      const p = localPos(e);
      const dx = p[0] - lastX, dy = p[1] - lastY;
      moved += Math.abs(dx) + Math.abs(dy);
      lastX = p[0]; lastY = p[1];
      const s = scale();
      st.lon = x2lon(lon2x(st.lon, s) - dx, s);
      st.lat = y2lat(lat2y(st.lat, s) - dy, s);
      draw(); e.preventDefault();
    }, { passive: false });
    cv.addEventListener('touchend', (e) => {
      if (e.touches.length < 2) pinch = null;
      if (e.touches.length === 0) {
        dragging = false;
        if (st.edit && st.edit.dragIndex >= 0) {
          st.edit.dragIndex = -1;
          draw();
          if (st.edit.cb) st.edit.cb(editGeoJSON());
        }
      }
    });
    function touchDist(e) {
      return Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                        e.touches[0].clientY - e.touches[1].clientY);
    }

    ctl.addEventListener('click', (e) => {
      const z = e.target.getAttribute('data-z');
      if (!z) return;
      if (z === 'fit') { if (st.lastBounds) fitBounds(st.lastBounds); return; }
      st.zoom = Math.max(o.minZoom, Math.min(o.maxZoom, st.zoom + Number(z)));
      draw();
    });

    document.addEventListener('wq:theme', () => { st.tileCache.forEach(() => {}); draw(); });

    // ---------------------------------------------------------- emitters --
    function emit(name) {
      const args = Array.prototype.slice.call(arguments, 1);
      (st.listeners[name] || []).forEach(fn => fn.apply(null, args));
    }

    // ------------------------------------------------------- draw tools ---
    function startDraw(kind, cb) {
      st.draw = { kind: kind, points: [], cb: cb, cursor: null };
      cv.classList.add('drawing');
      cv.style.cursor = 'crosshair';
      draw();
    }
    function finishDraw() {
      if (!st.draw) return;
      const d = st.draw;
      let geo = null;
      if (d.kind === 'point' && d.points.length >= 1) {
        geo = { type: 'Point', coordinates: d.points[d.points.length - 1] };
      } else if (d.kind === 'line' && d.points.length >= 2) {
        geo = { type: 'LineString', coordinates: d.points };
      } else if (d.kind === 'polygon' && d.points.length >= 3) {
        const ring = d.points.slice();
        ring.push(ring[0]);
        geo = { type: 'Polygon', coordinates: [ring] };
      }
      st.draw = null;
      cv.classList.remove('drawing');
      cv.style.cursor = 'grab';
      draw();
      if (d.cb) d.cb(geo);
      return geo;
    }
    function cancelDraw() {
      st.draw = null; cv.classList.remove('drawing'); cv.style.cursor = 'grab'; draw();
    }
    function undoDrawPoint() {
      if (st.draw && st.draw.points.length) {
        st.draw.points.pop();
        draw();
        emit('drawprogress', st.draw.kind, st.draw.points.length);
      }
    }
    function drawPointCount() { return st.draw ? st.draw.points.length : 0; }

    // ------------------------------------------------------- edit tools ---
    // startEdit puts draggable handles on an EXISTING geometry. The polygon
    // ring's closing point is not given a handle of its own - dragging vertex
    // 0 moves it too, so the ring can never be left open by an edit.
    function startEdit(kind, geojson, cb) {
      let coords = [];
      if (geojson) {
        if (geojson.type === 'Point') coords = [geojson.coordinates.slice()];
        else if (geojson.type === 'LineString') coords = geojson.coordinates.map(c => c.slice());
        else if (geojson.type === 'Polygon') {
          const ring = (geojson.coordinates[0] || []).map(c => c.slice());
          if (ring.length > 1 &&
              ring[0][0] === ring[ring.length - 1][0] &&
              ring[0][1] === ring[ring.length - 1][1]) ring.pop();
          coords = ring;
        }
      }
      st.edit = { kind: kind, coords: coords, cb: cb, dragIndex: -1, hoverIndex: -1 };
      draw();
      return st.edit;
    }
    function stopEdit() {
      const g = st.edit ? editGeoJSON() : null;
      st.edit = null;
      draw();
      return g;
    }
    function editGeoJSON() {
      if (!st.edit) return null;
      const c = st.edit.coords;
      if (st.edit.kind === 'point') return c.length ? { type: 'Point', coordinates: c[0] } : null;
      if (st.edit.kind === 'line') return c.length >= 2 ? { type: 'LineString', coordinates: c.map(p => p.slice()) } : null;
      if (st.edit.kind === 'polygon') {
        if (c.length < 3) return null;
        const ring = c.map(p => p.slice());
        ring.push(ring[0].slice());
        return { type: 'Polygon', coordinates: [ring] };
      }
      return null;
    }
    function moveVertex(i, px, py) {
      if (!st.edit || !st.edit.coords[i]) return;
      const ll = unproject(px, py);              // [lat, lon]
      st.edit.coords[i] = [ll[1], ll[0]];        // store [lon, lat]
      draw();
    }
    function isEditing() { return !!st.edit; }
    function editVertexCount() { return st.edit ? st.edit.coords.length : 0; }

    // Whatever tool is live right now: 'draw:point' | 'draw:line' |
    // 'draw:polygon' | 'edit:point' | ... | null. The field page shows this
    // so the surveyor is never guessing what a tap will do.
    function mode() {
      if (st.draw) return 'draw:' + st.draw.kind;
      if (st.edit) return 'edit:' + st.edit.kind;
      return null;
    }

    // ----------------------------------------------------------- bounds ---
    function boundsOf(geojson) {
      let n = -Infinity, s = Infinity, e = -Infinity, w = Infinity, any = false;
      const eat = (c) => { any = true; w = Math.min(w, c[0]); e = Math.max(e, c[0]);
                           s = Math.min(s, c[1]); n = Math.max(n, c[1]); };
      const walk = (g) => {
        if (!g) return;
        if (g.type === 'Point') eat(g.coordinates);
        else if (g.type === 'LineString') g.coordinates.forEach(eat);
        else if (g.type === 'Polygon') (g.coordinates[0] || []).forEach(eat);
        else if (g.type === 'MultiPolygon') g.coordinates.forEach(p => (p[0] || []).forEach(eat));
      };
      (geojson.features || [geojson]).forEach(f => walk(f.geometry || f));
      return any ? { n: n, s: s, e: e, w: w } : null;
    }
    function fitBounds(b, padPx) {
      if (!b) return;
      st.lastBounds = b;
      const pad = padPx === undefined ? 40 : padPx;
      const W = Math.max(60, st.w - pad * 2), H = Math.max(60, st.h - pad * 2);
      let z = o.maxZoom;
      for (; z > o.minZoom; z -= 0.1) {
        const s = TILE * Math.pow(2, z);
        const dx = Math.abs(lon2x(b.e, s) - lon2x(b.w, s));
        const dy = Math.abs(lat2y(b.s, s) - lat2y(b.n, s));
        if (dx <= W && dy <= H) break;
      }
      st.zoom = Math.max(o.minZoom, Math.min(o.maxZoom, z));
      st.lat = (b.n + b.s) / 2;
      st.lon = (b.e + b.w) / 2;
      draw();
    }
    function fitTo(geojson, padPx) {
      const b = boundsOf(geojson);
      if (b) fitBounds(b, padPx);
      return b;
    }

    resize();

    return {
      el: container, canvas: cv,
      setLayers(layers) { st.layers = layers || []; draw(); return this; },
      layers() { return st.layers; },
      setLayerVisible(id, vis) {
        const l = st.layers.find(x => x.id === id);
        if (l) { l.visible = vis; draw(); }
        return this;
      },
      setTooltip(fn) { st.tipFn = fn; return this; },
      select(layerId, featureId) { st.selected = featureId ? { layer: layerId, id: featureId } : null; draw(); return this; },
      selected() { return st.selected; },
      on(name, fn) { (st.listeners[name] = st.listeners[name] || []).push(fn); return this; },
      setCenter(lat, lon, zoom) {
        st.lat = lat; st.lon = lon;
        if (zoom !== undefined) st.zoom = Math.max(o.minZoom, Math.min(o.maxZoom, zoom));
        draw(); return this;
      },
      center() { return { lat: st.lat, lon: st.lon, zoom: st.zoom }; },
      fitTo: fitTo, fitBounds: fitBounds, boundsOf: boundsOf,
      startDraw: startDraw, finishDraw: finishDraw, cancelDraw: cancelDraw,
      undoDrawPoint: undoDrawPoint, isDrawing: () => !!st.draw,
      drawPointCount: drawPointCount,
      startEdit: startEdit, stopEdit: stopEdit, editGeoJSON: editGeoJSON,
      isEditing: isEditing, editVertexCount: editVertexCount, mode: mode,
      setBasemap(on) { st.showTiles = on; draw(); return this; },
      basemapAvailable: () => st.tilesOk,
      redraw: draw, resize: resize,
      project: project, unproject: unproject,
      destroy() { if (ro) ro.disconnect(); container.innerHTML = ''; }
    };
  }

  global.WQMap = { create: create };
})(window);
