/* ============================================================
   W4GGJ Mission Control — SHACK WALL DISPLAY client
   Polls /api/state ~1s and paints a fixed, no-scroll operator
   view. List lengths are capped to what fits the screen so the
   layout never overflows. New decodes / QSOs flash in.
   ============================================================ */

const $ = (id) => document.getElementById(id);
let lastQsoTs = 0;
let seenDecodeTs = 0;
let firstLoad = true;

/* How many rows each panel can show without scrolling. Tuned for
   a 16:9 monitor; the CSS clips anything extra as a safety net. */
const MAX_DECODES = 18;
const MAX_QSOS = 6;
const MAX_POTA = 6;
const MAX_DX = 6;

/* ── UTC clock (ticks locally, no server round-trip) ─────── */
function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('utc').textContent = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}
setInterval(tickClock, 250);
tickClock();

/* ── helpers ─────────────────────────────────────────────── */
function fmtFreq(mhz) { return mhz ? mhz.toFixed(3) : '--.---'; }
function fmtTime(t) { return (!t || t.length < 4) ? '—' : `${t.slice(0, 2)}:${t.slice(2, 4)}`; }
function bandCls(cond) {
  const c = (cond || '').toLowerCase();
  if (c.includes('good')) return 'b-good';
  if (c.includes('fair')) return 'b-fair';
  if (c.includes('poor') || c.includes('closed')) return 'b-poor';
  return '';
}
function snrColor(snr) {
  if (snr == null) return 'var(--muted)';
  if (snr >= 0) return 'var(--green)';
  if (snr >= -12) return 'var(--cyan)';
  return 'var(--muted)';
}
function esc(s) { return (s || '').replace(/</g, '&lt;'); }

/* ── render ──────────────────────────────────────────────── */
function render(s) {
  const id = s.identity || {};
  const r = s.radio || {};
  const sig = s.signal || {};
  const log = s.log || {};
  const sol = s.solar || {};

  // identity (once)
  if (firstLoad) {
    $('tb-call').textContent = id.callsign || 'W4GGJ';
    $('tb-sub').textContent = id.subtitle || '';
  }

  // connection + on-air state
  $('conn-dot').className = 'dot live';
  $('conn-txt').textContent = 'LINKED';
  $('conn').className = 'pill live';

  const air = $('onair'), airDot = $('air-dot'), airTxt = $('air-txt');
  const freqPanel = $('freq-panel');
  if (r.tx) {
    air.className = 'pill tx'; airDot.className = 'dot tx'; airTxt.textContent = 'TRANSMITTING';
    freqPanel.className = 'panel a-freq tx';
  } else if (r.online) {
    air.className = 'pill live'; airDot.className = 'dot live'; airTxt.textContent = 'ON AIR';
    freqPanel.className = 'panel a-freq rx';
  } else {
    air.className = 'pill'; airDot.className = 'dot'; airTxt.textContent = 'STANDBY';
    freqPanel.className = 'panel a-freq';
  }

  // frequency hero
  $('freq').textContent = fmtFreq(r.freq_mhz);
  $('chip-band').textContent = r.band || '—';
  $('chip-mode').textContent = r.mode || '—';
  const txrx = $('chip-txrx');
  if (r.tx) { txrx.textContent = 'TRANSMIT'; txrx.className = 'chip tx'; }
  else if (r.online) { txrx.textContent = 'RECEIVE'; txrx.className = 'chip rx'; }
  else { txrx.textContent = 'STANDBY'; txrx.className = 'chip'; }
  $('chip-src').textContent = r.online ? (r.source || '—') : 'NO SIGNAL';
  $('dxline').textContent = r.dx_call ? `▸ WORKING ${r.dx_call}${r.report ? ' · ' + r.report : ''}` : '';

  // S-meter
  $('s-val').textContent = sig.s_meter || '—';
  $('s-mask').style.left = (sig.s_pct || 0) + '%';
  $('s-snr').textContent = sig.snr == null ? '—' : (sig.snr > 0 ? '+' : '') + sig.snr + ' dB';
  $('s-snr').style.color = snrColor(sig.snr);
  $('s-mode').textContent = r.mode || '—';

  // decode feed
  const feed = $('feed');
  if (s.decodes && s.decodes.length) {
    feed.innerHTML = '';
    s.decodes.slice(0, MAX_DECODES).forEach(d => {
      const isCq = /^CQ/.test(d.msg || '');
      const isNew = d.ts > seenDecodeTs;
      const row = document.createElement('div');
      row.className = 'feed-row' + (isCq ? ' cq' : '') + (isNew && !firstLoad ? ' new' : '');
      row.innerHTML =
        `<div class="t">${d.time}</div>` +
        `<div class="snr" style="color:${snrColor(d.snr)}">${d.snr > 0 ? '+' : ''}${d.snr}</div>` +
        `<div style="color:var(--muted)">${d.df}</div>` +
        `<div class="msg">${esc(d.msg)}</div>`;
      feed.appendChild(row);
    });
    seenDecodeTs = s.decodes[0].ts;
  }

  // propagation
  $('h-sfi').textContent = sol.sfi || '—';
  $('h-kp').textContent = sol.k_index || '—';
  $('p-sfi').textContent = sol.sfi || '—';
  $('p-kp').textContent = sol.k_index || '—';
  $('p-ssn').textContent = sol.sunspots || '—';
  $('p-wind').textContent = sol.solar_wind ? sol.solar_wind + ' km/s' : '—';

  const bands = $('bands');
  if (sol.bands && sol.bands.length) {
    bands.innerHTML = '';
    sol.bands.forEach(b => {
      // one chip per band, coloured by the better of day/night
      const el = document.createElement('div');
      el.className = 'bchip ' + (bandCls(b.day) || bandCls(b.night));
      el.innerHTML = `<div class="bn">${b.band}</div>` +
        `<div class="bc">D:${(b.day || '—').slice(0, 4).toUpperCase()} N:${(b.night || '—').slice(0, 4).toUpperCase()}</div>`;
      bands.appendChild(el);
    });
  }

  // logbook stats
  $('h-qsos').textContent = log.total ? log.total.toLocaleString() : '—';
  $('l-total').textContent = log.total ? log.total.toLocaleString() : '—';
  $('l-calls').textContent = log.unique_calls || '—';
  $('l-ctry').textContent = log.countries || '—';
  $('l-bands').textContent = log.bands || '—';
  $('l-modes').textContent = log.modes || '—';
  $('l-dx').textContent = log.best_dx ? `${log.best_dx.call} · ${log.best_dx.km.toLocaleString()}km` : '—';

  // recent QSOs
  const lb = $('log-body');
  const fresh = log.last_qso_ts && log.last_qso_ts > lastQsoTs && !firstLoad;
  if (log.recent && log.recent.length) {
    lb.innerHTML = '';
    log.recent.slice(0, MAX_QSOS).forEach((q, i) => {
      const tr = document.createElement('tr');
      if (fresh && i === 0) tr.className = 'fresh';
      tr.innerHTML =
        `<td>${fmtTime(q.time)}</td><td class="call">${esc(q.call)}</td>` +
        `<td>${esc(q.band)}</td><td>${esc(q.mode)}</td>` +
        `<td style="color:var(--muted)">${esc(q.country) || ''}</td>`;
      lb.appendChild(tr);
    });
  }
  lastQsoTs = log.last_qso_ts || lastQsoTs;

  // POTA
  const pota = $('pota');
  if (s.pota && s.pota.length) {
    pota.innerHTML = '';
    s.pota.slice(0, MAX_POTA).forEach(p => {
      const row = document.createElement('div');
      row.className = 'srow';
      row.innerHTML =
        `<div><span class="a">${esc(p.call)}</span> <span class="meta">${esc(p.ref)} · ${esc(p.loc)}</span></div>` +
        `<div class="f">${esc(p.freq)}<span class="meta"> ${esc(p.mode)}</span></div>`;
      pota.appendChild(row);
    });
  } else {
    pota.innerHTML = '<div class="empty">No active POTA spots.</div>';
  }

  // ISS
  const iss = s.iss || {};
  if (iss.lat != null) {
    $('h-iss').textContent = iss.range_km ? iss.range_km.toLocaleString() + ' km' : '—';
    $('iss-pos').textContent =
      `${Math.abs(iss.lat)}°${iss.lat >= 0 ? 'N' : 'S'}  ${Math.abs(iss.lon)}°${iss.lon >= 0 ? 'E' : 'W'}`;
    $('iss-range').textContent = iss.range_km ? `RANGE ${iss.range_km.toLocaleString()} km from ${id.grid || ''}` : '';
    $('iss-alt').textContent = (iss.alt_km || '—') + ' km';
    $('iss-vel').textContent = iss.vel_kmh ? iss.vel_kmh.toLocaleString() + ' km/h' : '—';
  }

  // DX cluster
  const dx = $('dx');
  if (s.dx && s.dx.length) {
    dx.innerHTML = '';
    s.dx.slice(0, MAX_DX).forEach(d => {
      const row = document.createElement('div');
      row.className = 'srow';
      row.innerHTML =
        `<div><span class="a">${esc(d.dx)}</span> <span class="meta">${esc(d.band)} · de ${esc(d.spotter)}</span></div>` +
        `<div class="f">${esc(String(d.freq))}</div>`;
      dx.appendChild(row);
    });
  } else {
    dx.innerHTML = '<div class="empty">No DX spots yet.</div>';
  }

  firstLoad = false;
}

/* ── poll loop ───────────────────────────────────────────── */
async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    render(await res.json());
  } catch (e) {
    $('conn-dot').className = 'dot';
    $('conn-txt').textContent = 'NO LINK';
    $('conn').className = 'pill';
  }
}
poll();
setInterval(poll, 1000);
