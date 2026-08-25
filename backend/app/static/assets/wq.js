/* =====================================================================
   Wastraq shared UI runtime.
   No build step, no framework, no CDN - the backend serves this file and
   the dashboards work whether or not the machine has internet.
   ===================================================================== */
(function (global) {
  'use strict';

  // ------------------------------------------------------------ storage --
  // localStorage can throw (private mode, file://). Fall back to memory so a
  // preference never breaks the page.
  const mem = {};
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? (k in mem ? mem[k] : d) : v; }
                catch (e) { return k in mem ? mem[k] : d; } },
    set(k, v) { mem[k] = v; try { localStorage.setItem(k, v); } catch (e) { /* memory only */ } }
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // --------------------------------------------------------------- api ---
  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    if (o.body && typeof o.body !== 'string' && !(o.body instanceof FormData)) {
      o.body = JSON.stringify(o.body);
      o.headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(path, o);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      const err = new Error(detailText(data) || (res.status + ' ' + res.statusText));
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }
  function detailText(d) {
    if (!d) return '';
    const x = d.detail !== undefined ? d.detail : d;
    if (typeof x === 'string') return x;
    if (Array.isArray(x)) return x.map(i => i.msg || i.error || JSON.stringify(i)).join('; ');
    if (x && typeof x === 'object') {
      if (x.error && x.blockers) return x.error + ': ' + x.blockers.join('; ');
      return x.error || x.msg || JSON.stringify(x);
    }
    return String(x);
  }

  // ---------------------------------------------------------- formatting --
  const nf = new Intl.NumberFormat();
  const num = (v) => (v === null || v === undefined || v === '') ? '—' : nf.format(v);
  const pct = (v) => (v === null || v === undefined) ? '—' : (Math.round(v * 10) / 10) + '%';
  function timeOf(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return isNaN(d) ? '—' : d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function dateOf(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return isNaN(d) ? '—' : d.toLocaleDateString([], { day: '2-digit', month: 'short', year: 'numeric' });
  }
  function ago(ts) {
    if (!ts) return '—';
    const s = (Date.now() - new Date(ts).getTime()) / 1000;
    if (isNaN(s)) return '—';
    if (s < 60) return Math.max(0, Math.floor(s)) + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  const esc = (s) => String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // -------------------------------------------------------------- state --
  // One place that decides what a state looks like, so the map, the tables
  // and the drawer can never disagree about a colour.
  const STATE = {
    // operations
    SEGREGATED:            { cls: 'good',    label: 'Segregated',      mark: '✓' },
    NOT_SEGREGATED:        { cls: 'crit',    label: 'Not segregated',  mark: '✕' },
    SERVICED:              { cls: 'good',    label: 'Serviced',        mark: '✓' },
    REVIEW:                { cls: 'warn',    label: 'Review required', mark: '!' },
    NEEDS_REVIEW:          { cls: 'warn',    label: 'Needs review',    mark: '!' },
    AUTO_CONFIRMED:        { cls: 'neutral', label: 'Auto confirmed',  mark: '•' },
    REVIEWED_OK:           { cls: 'good',    label: 'Reviewed OK',     mark: '✓' },
    REVIEWED_REJECTED:     { cls: 'crit',    label: 'Rejected',        mark: '✕' },
    AMBIGUOUS:             { cls: 'serious', label: 'Ambiguous',       mark: '?' },
    GIS_ISSUE:             { cls: 'violet',  label: 'GIS issue',       mark: '⚠' },
    MAPPED:                { cls: 'neutral', label: 'Mapped',          mark: '○' },
    AUTO_ASSOCIATED:       { cls: 'good',    label: 'Auto associated', mark: '✓' },
    NO_MATCH:              { cls: 'neutral', label: 'No match',        mark: '–' },
    // survey
    APPROVED:              { cls: 'good',    label: 'Verified',        mark: '✓' },
    SUBMITTED:             { cls: 'accent',  label: 'Pending review',  mark: '…' },
    CORRECTION_REQUIRED:   { cls: 'warn',    label: 'Correction',      mark: '↺' },
    REJECTED:              { cls: 'crit',    label: 'Rejected',        mark: '✕' },
    IN_PROGRESS:           { cls: 'violet',  label: 'In progress',     mark: '◐' },
    NOT_SURVEYED:          { cls: 'neutral', label: 'Not surveyed',    mark: '○' },
    PENDING:               { cls: 'accent',  label: 'Pending',         mark: '…' },
    // confidence / severity
    HIGH:                  { cls: 'good',    label: 'High',            mark: '' },
    MEDIUM:                { cls: 'warn',    label: 'Medium',          mark: '' },
    LOW:                   { cls: 'crit',    label: 'Low',             mark: '' },
    CRITICAL:              { cls: 'crit',    label: 'Critical',        mark: '' },
    OPEN:                  { cls: 'warn',    label: 'Open',            mark: '' },
    ACKNOWLEDGED:          { cls: 'accent',  label: 'Acknowledged',    mark: '' },
    RESOLVED:              { cls: 'good',    label: 'Resolved',        mark: '' },
    WONT_FIX:              { cls: 'neutral', label: 'Won’t fix',  mark: '' },
    // assignment
    NOT_STARTED:           { cls: 'neutral', label: 'Not started',     mark: '' },
    COMPLETED:             { cls: 'good',    label: 'Completed',       mark: '' },
    ON_HOLD:               { cls: 'warn',    label: 'On hold',         mark: '' },
    // property verification
    VERIFIED_FOR_OPERATION:{ cls: 'good',    label: 'Verified for operation', mark: '✓' },
    PENDING_SURVEY:        { cls: 'accent',  label: 'Pending survey',  mark: '○' },
    FIELD_SURVEYED:        { cls: 'accent',  label: 'Field surveyed',  mark: '' },
    FIELD_VERIFIED:        { cls: 'good',    label: 'Field verified',  mark: '' },
    UNVERIFIED:            { cls: 'neutral', label: 'Unverified',      mark: '' },
    DISPUTED:              { cls: 'crit',    label: 'Disputed',        mark: '' }
  };
  function state(key) {
    return STATE[key] || { cls: 'neutral', label: String(key || '—').replace(/_/g, ' '), mark: '' };
  }
  function chip(key, textOverride) {
    const s = state(key);
    return '<span class="chip ' + s.cls + '"><span class="dot"></span>' +
           esc(textOverride || s.label) + '</span>';
  }
  // Resolved CSS colour for a state, for canvas drawing.
  const colourCache = {};
  function stateColour(key) {
    const cls = state(key).cls;
    if (colourCache[cls]) return colourCache[cls];
    const varName = { good: '--good', warn: '--warn', crit: '--crit', serious: '--serious',
                      accent: '--accent', violet: '--violet', neutral: '--neutral' }[cls] || '--neutral';
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || '#6b737d';
    colourCache[cls] = v;
    return v;
  }
  function clearColourCache() { for (const k in colourCache) delete colourCache[k]; }

  // Severity is the one vocabulary that collides with confidence: HIGH confidence is
  // good news, HIGH severity is not. Keep it on its own scale so a critical QA issue
  // never renders green.
  const SEVERITY = {
    CRITICAL: { cls: 'crit',    label: 'Critical' },
    HIGH:     { cls: 'serious', label: 'High' },
    MEDIUM:   { cls: 'warn',    label: 'Medium' },
    LOW:      { cls: 'neutral', label: 'Low' }
  };
  function severity(key) {
    return SEVERITY[key] || { cls: 'neutral', label: String(key || '—').replace(/_/g, ' ') };
  }
  function sevChip(key) {
    const s = severity(key);
    return '<span class="chip ' + s.cls + '"><span class="dot"></span>' + esc(s.label) + '</span>';
  }
  function sevColour(key) {
    const cls = severity(key).cls;
    const varName = { crit: '--crit', serious: '--serious', warn: '--warn', neutral: '--neutral' }[cls];
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || '#6b737d';
  }

  // -------------------------------------------------------------- toast --
  function toast(msg, kind, ms) {
    let host = $('#wq-toasts');
    if (!host) { host = document.createElement('div'); host.id = 'wq-toasts'; document.body.appendChild(host); }
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 220); }, ms || 3600);
  }

  // ------------------------------------------------------------- theme ---
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    store.set('wq-theme', t);
    clearColourCache();
    document.dispatchEvent(new CustomEvent('wq:theme', { detail: t }));
  }
  function initTheme() { applyTheme(store.get('wq-theme', 'dark')); }
  function toggleTheme() {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
  }

  // ------------------------------------------------------- auto refresh --
  function autoRefresh(fn, seconds) {
    let timer = null, paused = false, last = null, busy = false;
    const listeners = [];
    async function tick(manual) {
      if (busy || (paused && !manual)) return;
      busy = true;
      try { await fn(); last = new Date(); }
      catch (e) { toast('Refresh failed: ' + e.message, 'crit'); }
      finally { busy = false; listeners.forEach(l => l(status())); }
    }
    function status() { return { paused: paused, last: last, seconds: seconds }; }
    function start() {
      stop();
      timer = setInterval(tick, seconds * 1000);
      // stop polling while the tab is hidden - no point loading the API
      document.addEventListener('visibilitychange', onVis);
    }
    function onVis() { if (document.visibilityState === 'visible' && !paused) tick(); }
    function stop() { if (timer) clearInterval(timer); timer = null; document.removeEventListener('visibilitychange', onVis); }
    return {
      start: function () { start(); tick(true); return this; },
      stop: stop,
      now: () => tick(true),
      pause: function () { paused = true; listeners.forEach(l => l(status())); },
      resume: function () { paused = false; listeners.forEach(l => l(status())); tick(true); },
      toggle: function () { paused ? this.resume() : this.pause(); return paused; },
      onChange: (l) => listeners.push(l),
      status: status,
      setInterval: function (s) { seconds = s; start(); }
    };
  }

  // ------------------------------------------------------------- drawer --
  function drawer(id) {
    const el = $('#' + id), scrim = $('#' + id + '-scrim');
    if (scrim) scrim.addEventListener('click', close);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    function open() { el.classList.add('open'); if (scrim) scrim.classList.add('open'); }
    function close() { el.classList.remove('open'); if (scrim) scrim.classList.remove('open'); }
    return { open: open, close: close, el: el, isOpen: () => el.classList.contains('open') };
  }

  function modal(id) {
    const el = $('#' + id);
    if (el) el.addEventListener('click', (e) => { if (e.target === el) close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    function open() { el.classList.add('open'); }
    function close() { el.classList.remove('open'); }
    return { open: open, close: close, el: el };
  }

  async function confirmDialog(message, okLabel, kind) {
    return new Promise((resolve) => {
      const scrim = document.createElement('div');
      scrim.className = 'modal-scrim open';
      scrim.innerHTML =
        '<div class="modal" style="width:min(440px,94vw)">' +
        '<header><h3>Confirm</h3></header>' +
        '<div class="body">' + esc(message) + '</div>' +
        '<footer><button data-x="no">Cancel</button>' +
        '<button class="' + (kind || 'primary') + '" data-x="yes">' + esc(okLabel || 'Confirm') + '</button></footer></div>';
      document.body.appendChild(scrim);
      scrim.addEventListener('click', (e) => {
        const x = e.target.getAttribute && e.target.getAttribute('data-x');
        if (x || e.target === scrim) { scrim.remove(); resolve(x === 'yes'); }
      });
    });
  }

  // -------------------------------------------------------------- shell --
  // The primary list is the workflow, in order:
  //   register the record -> survey the ground -> review it -> operate on it.
  // Everything else is real, working tooling that stays reachable but does
  // not belong in a four-step story: putting it up top makes a 16-property
  // pilot look more complicated than it is.
  const NAV = [
    { group: 'Register' },
    { href: '/property-registration', ic: '▤', label: 'Property master' },
    { group: 'Survey' },
    { href: '/survey/field', ic: '✒', label: 'Field survey' },
    { href: '/survey/review', ic: '✓', label: 'Review queue', badgeKey: 'review' },
    { group: 'Operations' },
    { href: '/dashboard', ic: '▣', label: 'Live operations' },
    { group: 'Perception' },
    // Camera-local picker tracking. Deliberately its own group: it observes
    // people, not properties, and it is not yet wired into association.
    { href: '/picker-tracking', ic: '◉', label: 'Picker tracking' },
    { group: 'GIS / admin tools' },
    { href: '/survey', ic: '◰', label: 'Survey overview' },
    { href: '/survey/map', ic: '◈', label: 'Survey map' },
    { href: '/survey/assignments', ic: '≡', label: 'Assignments' },
    { href: '/survey/qa', ic: '⚠', label: 'GIS QA', badgeKey: 'qa' },
    { href: '/docs', ic: '{ }', label: 'API docs' }
  ];

  function shell(opts) {
    const o = opts || {};
    const path = location.pathname.replace(/\/$/, '') || '/dashboard';
    const nav = NAV.map(item => {
      if (item.group) return '<div class="group">' + esc(item.group) + '</div>';
      const active = (path === item.href) ||
                     (item.href !== '/survey' && path.indexOf(item.href) === 0);
      return '<a href="' + item.href + '" class="' + (active ? 'active' : '') + '">' +
             '<span class="ic">' + item.ic + '</span>' +
             '<span class="label">' + esc(item.label) + '</span>' +
             (item.badgeKey ? '<span class="badge" data-badge="' + item.badgeKey + '" hidden>0</span>' : '') +
             '</a>';
    }).join('');

    document.body.insertAdjacentHTML('afterbegin',
      '<div class="app" id="wq-app">' +
        '<div class="brand"><div class="mark">W</div>' +
          '<div class="name">Wastraq<small>' + esc(o.suite || 'Evidence Engine') + '</small></div></div>' +
        '<div class="topbar" id="wq-topbar">' +
          '<div class="stack"><span class="page-title">' + esc(o.title || '') + '</span>' +
          '<span class="page-sub">' + esc(o.subtitle || '') + '</span></div>' +
          '<div class="spacer"></div>' +
          '<div class="row wrap" id="wq-topbar-slot"></div>' +
          '<span class="status-pill" id="wq-api"><span class="dot"></span><span>API</span></span>' +
          '<span class="status-pill" id="wq-db"><span class="dot"></span><span>DB</span></span>' +
          '<span class="status-pill" id="wq-clock" data-tip="Local time"><span>--:--:--</span></span>' +
          '<button class="icon ghost" id="wq-theme" data-tip="Light / dark">◑</button>' +
          '<span class="status-pill" data-tip="Signed in as (demo)"><span>●</span>' +
          '<span>' + esc(o.user || 'Operator') + '</span></span>' +
        '</div>' +
        '<nav class="side">' + nav + '</nav>' +
        '<main class="content" id="wq-main"></main>' +
      '</div>');

    $('#wq-theme').addEventListener('click', toggleTheme);
    const clock = $('#wq-clock span');
    const tickClock = () => { clock.textContent = new Date().toLocaleTimeString(); };
    tickClock(); setInterval(tickClock, 1000);
    health();
    setInterval(health, 20000);
    return $('#wq-main');
  }

  async function health() {
    const apiPill = $('#wq-api'), dbPill = $('#wq-db');
    if (!apiPill) return;
    try {
      const r = await api('/');
      apiPill.className = 'status-pill';
      apiPill.querySelector('span:last-child').textContent = 'API ' + (r.version || 'ok');
      apiPill.querySelector('.dot').classList.add('live');
    } catch (e) {
      apiPill.className = 'status-pill down';
      apiPill.querySelector('span:last-child').textContent = 'API down';
    }
    try {
      const d = await api('/health/db');
      dbPill.className = 'status-pill';
      const v = (d.postgis || '').split(' ')[0];
      dbPill.querySelector('span:last-child').textContent = 'PostGIS ' + v;
      dbPill.setAttribute('data-tip', 'database ' + (d.db || ''));
    } catch (e) {
      dbPill.className = 'status-pill down';
      dbPill.querySelector('span:last-child').textContent = 'DB down';
    }
  }

  function setNavBadge(key, n) {
    $$('[data-badge="' + key + '"]').forEach(el => {
      if (n > 0) { el.hidden = false; el.textContent = n > 999 ? '999+' : n;
                   el.className = 'badge ' + (key === 'qa' ? 'warn' : ''); }
      else el.hidden = true;
    });
  }

  // ------------------------------------------------------------ helpers --
  function kpi(o) {
    return '<div class="kpi ' + (o.tone || '') + '">' +
      '<div class="k-label">' + esc(o.label) +
        (o.tip ? '<span class="muted" data-tip="' + esc(o.tip) + '">ⓘ</span>' : '') + '</div>' +
      '<div class="k-value">' + (o.value === undefined ? '—' : o.value) + '</div>' +
      (o.sub ? '<div class="k-sub">' + o.sub + '</div>' : '') +
      (o.bar !== undefined ? '<div class="k-bar"><i style="width:' +
        Math.max(0, Math.min(100, o.bar)) + '%"></i></div>' : '') +
      '</div>';
  }

  function skeletonRows(cols, rows) {
    let h = '';
    for (let r = 0; r < (rows || 5); r++) {
      h += '<tr>';
      for (let c = 0; c < cols; c++) h += '<td><div class="skel" style="width:' + (40 + (c * 13) % 50) + '%"></div></td>';
      h += '</tr>';
    }
    return h;
  }

  function emptyState(msg, hint) {
    return '<div class="empty"><div class="big">∅</div><div>' + esc(msg) + '</div>' +
           (hint ? '<div class="small muted">' + hint + '</div>' : '') + '</div>';
  }

  function qs(obj) {
    const p = new URLSearchParams();
    Object.keys(obj || {}).forEach(k => {
      const v = obj[k];
      if (v !== undefined && v !== null && v !== '') p.set(k, v);
    });
    const s = p.toString();
    return s ? '?' + s : '';
  }

  function param(name, dflt) {
    return new URLSearchParams(location.search).get(name) || dflt;
  }

  global.WQ = {
    $: $, $$: $$, api: api, num: num, pct: pct, esc: esc,
    timeOf: timeOf, dateOf: dateOf, ago: ago,
    state: state, chip: chip, stateColour: stateColour,
    severity: severity, sevChip: sevChip, sevColour: sevColour,
    toast: toast, initTheme: initTheme, toggleTheme: toggleTheme,
    autoRefresh: autoRefresh, drawer: drawer, modal: modal, confirmDialog: confirmDialog,
    shell: shell, setNavBadge: setNavBadge, kpi: kpi,
    skeletonRows: skeletonRows, emptyState: emptyState, qs: qs, param: param,
    store: store, detailText: detailText
  };
  initTheme();
})(window);
