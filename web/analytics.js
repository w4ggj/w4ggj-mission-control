/* ============================================================
   W4GGJ Mission Control — Visitor Analytics
   Fetches the token-gated /api/analytics and renders visitor
   counts, a world map of where they came from, and tables.
   Reuses WORLD_LAND (worldmap.js) + the shared theme.
   ============================================================ */
(function () {
  'use strict';

  var KEY_STORE = 'w4ggj_an_key';
  var W = 1000, H = 500;
  // Station home marker — W4GGJ, grid EL87, Tampa Bay FL.
  var HOME = [27.75, -82.4];

  var $ = function (id) { return document.getElementById(id); };
  var X = function (lon) { return (lon + 180) / 360 * W; };
  var Y = function (lat) { return (90 - lat) / 180 * H; };
  var NS = 'http://www.w3.org/2000/svg';

  function getKey() {
    var m = /[#&?]key=([^&]+)/.exec(location.hash + '&' + location.search);
    if (m) return decodeURIComponent(m[1]);
    try { return localStorage.getItem(KEY_STORE) || ''; } catch (e) { return ''; }
  }
  function saveKey(k) { try { localStorage.setItem(KEY_STORE, k); } catch (e) {} }

  function flag(cc) {
    if (!cc || cc.length !== 2 || /[^A-Za-z]/.test(cc)) return '🏳️';
    return String.fromCodePoint(0x1F1E6 + cc.toUpperCase().charCodeAt(0) - 65) +
           String.fromCodePoint(0x1F1E6 + cc.toUpperCase().charCodeAt(1) - 65);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.floor(s) + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function loc(e) {
    var p = [];
    if (e.city) p.push(e.city);
    else if (e.region) p.push(e.region);
    var out = p.join('');
    if (e.cc) out = (out ? out + ' ' : '') + flag(e.cc);
    return out || (e.bot ? '' : '·');
  }

  // ── world map (land outline + visitor dots) ──
  function el(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function drawMap(points) {
    var svg = $('map');
    svg.innerHTML = '';
    if (typeof WORLD_LAND !== 'undefined') {
      svg.appendChild(el('path', { d: WORLD_LAND, class: 'land' }));
    }
    // Home station.
    svg.appendChild(el('circle', { cx: X(HOME[1]), cy: Y(HOME[0]), r: 3.4, class: 'home' }));
    var max = 1;
    points.forEach(function (p) { if (p.count > max) max = p.count; });
    points.forEach(function (p) {
      if (p.lat == null || p.lon == null) return;
      var cx = X(p.lon), cy = Y(p.lat);
      var r = 2.5 + 6 * Math.sqrt(p.count / max);
      svg.appendChild(el('circle', { cx: cx, cy: cy, r: r * 2.4, class: 'dot-glow' }));
      var d = el('circle', { cx: cx, cy: cy, r: r, class: 'dot' });
      var t = el('title', {});
      t.textContent = p.label + ' — ' + p.count + (p.count === 1 ? ' visit' : ' visits');
      d.appendChild(t);
      svg.appendChild(d);
    });
    $('m-count').textContent = points.length ? '· ' + points.length + ' locations' : '';
  }

  function barRows(tbody, rows, total) {
    tbody.innerHTML = rows.map(function (r) {
      var pct = total ? Math.round(r.count / total * 100) : 0;
      var w = total ? Math.max(2, Math.round(r.count / total * 100)) : 0;
      return '<tr><td class="bar"><span class="fill" style="width:' + w + '%"></span>' +
             '<span class="txt">' + r.label + '</span></td>' +
             '<td class="num">' + r.count + '</td>' +
             '<td class="num muted">' + pct + '%</td></tr>';
    }).join('') || '<tr><td class="muted">no data yet</td></tr>';
  }

  function render(d) {
    var t = d.totals || {};
    $('t-views').textContent = (t.views || 0).toLocaleString();
    $('t-uniq').textContent = (t.uniques || 0).toLocaleString();
    $('t-today').textContent = (t.today || 0).toLocaleString();
    $('t-24h').textContent = (t.views_24h || 0).toLocaleString();
    $('t-cty').textContent = (t.countries || 0).toLocaleString();

    if (d.since) {
      var since = new Date(d.since * 1000);
      $('an-since').textContent = 'SINCE ' + since.toLocaleDateString(undefined,
        { year: 'numeric', month: 'short', day: 'numeric' }).toUpperCase();
    }

    drawMap(d.points || []);

    // visits-per-day sparkline
    var days = Object.keys(d.daily || {}).sort();
    var last = days.slice(-45);
    var maxD = 1;
    last.forEach(function (k) { if (d.daily[k] > maxD) maxD = d.daily[k]; });
    $('spark').innerHTML = last.map(function (k) {
      var h = Math.round(d.daily[k] / maxD * 100);
      return '<i style="height:' + h + '%" title="' + k + ': ' + d.daily[k] + '"></i>';
    }).join('') || '<span class="muted" style="font-size:11px">no data yet</span>';

    var totV = t.views || 0;
    barRows($('countries'), (d.countries || []).slice(0, 12).map(function (c) {
      return { label: '<span class="flag">' + flag(c.cc) + '</span>' +
               esc(c.name || c.cc), count: c.count };
    }), totV);
    barRows($('cities'), (d.cities || []).slice(0, 12).map(function (c) {
      return { label: esc([c.city, c.region].filter(Boolean).join(', ') || c.cc) +
               ' <span class="flag" style="font-size:13px">' + flag(c.cc) + '</span>', count: c.count };
    }), totV);
    barRows($('pages'), (d.pages || []).map(function (p) {
      return { label: esc(p.page), count: p.count };
    }), totV);
    var refs = d.referrers || [];
    barRows($('refs'), refs.length ? refs.map(function (r) {
      return { label: esc(r.host), count: r.count };
    }) : [], totV);
    if (!refs.length) $('refs').innerHTML = '<tr><td class="muted">direct / no referrers yet</td></tr>';

    $('recent').innerHTML = (d.recent || []).map(function (e) {
      return '<tr><td class="muted" style="font-family:var(--mono);font-size:11px">' + ago(e.ts) + '</td>' +
             '<td>' + loc(e) + (e.bot ? '<span class="tag-bot">BOT</span>' : '') + '</td>' +
             '<td>' + esc(e.page) + '</td>' +
             '<td class="muted">' + esc(e.ua) + '</td>' +
             '<td class="muted" style="font-family:var(--mono);font-size:10.5px">' + esc(e.ip) + '</td></tr>';
    }).join('') || '<tr><td class="muted">no visits yet</td></tr>';

    $('foot').textContent = 'W4GGJ MISSION CONTROL · VISITOR ANALYTICS · UPDATED ' +
      new Date().toLocaleTimeString();
  }

  var key = getKey();
  var timer = null;

  function load(showGateOnFail) {
    return fetch('/api/analytics?key=' + encodeURIComponent(key), { cache: 'no-store' })
      .then(function (r) {
        if (r.status === 401) { throw new Error('unauthorized'); }
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .then(function (d) {
        $('gate').classList.add('hidden');
        $('dash').classList.remove('hidden');
        render(d);
        if (!timer) timer = setInterval(function () { load(false).catch(function () {}); }, 30000);
      })
      .catch(function (err) {
        if (showGateOnFail && String(err.message) === 'unauthorized') showGate('Incorrect key.');
        else if (showGateOnFail) showGate('');
        throw err;
      });
  }

  function showGate(msg) {
    $('dash').classList.add('hidden');
    $('gate').classList.remove('hidden');
    $('gate-err').textContent = msg || '';
    $('gate-key').focus();
  }

  $('gate-go').addEventListener('click', function () {
    key = $('gate-key').value.trim();
    if (!key) { $('gate-err').textContent = 'Enter your key.'; return; }
    saveKey(key);
    load(true).catch(function () {});
  });
  $('gate-key').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') $('gate-go').click();
  });

  // First load: try the stored/URL key silently; fall back to the gate.
  if (key) load(true).catch(function () {});
  else showGate('');
})();
