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

/* ── analog swinging-needle S-meter ──────────────────────── */
/* SVG rectangular meter face (S1…+40) built once; a light spring swings the
   needle toward each reading with a gentle overshoot + wander, like a real
   mechanical meter instead of a bar that steps every decode. */
let sMeterTarget = 0, sMeterShown = 0, sMeterVel = 0, sMeterNoise = 0, sMeterNoiseAt = 0;
function buildSMeter(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const cx = 120, cy = 108, R = 90, rMaj = 74, rMin = 82, lr = 96;
  const labels = ['S1', 'S3', 'S6', 'S9', '+20', '+40'];
  const ang = (pct) => (pct / 100 * 108 - 54) * Math.PI / 180;   // -54°…+54°
  let s = '<svg viewBox="0 0 240 120" preserveAspectRatio="xMidYMid meet">';
  s += '<rect class="s-face" x="2" y="2" width="236" height="116" rx="9"/>';
  for (let i = 0; i <= 20; i++) {
    const a = ang(i * 5), sn = Math.sin(a), cs = Math.cos(a);
    const maj = i % 4 === 0, red = i * 5 >= 80, ri = maj ? rMaj : rMin;
    s += `<line x1="${(cx+ri*sn).toFixed(1)}" y1="${(cy-ri*cs).toFixed(1)}" x2="${(cx+R*sn).toFixed(1)}" y2="${(cy-R*cs).toFixed(1)}" class="s-tick${maj?' maj':''}${red?' red':''}"/>`;
  }
  for (let j = 0; j < labels.length; j++) {
    const a = ang(j * 20);
    s += `<text x="${(cx+lr*Math.sin(a)).toFixed(1)}" y="${(cy-lr*Math.cos(a)+3.5).toFixed(1)}" class="s-lab${j*20>=80?' red':''}">${labels[j]}</text>`;
  }
  s += '<line id="s-needle" class="s-needle" x1="120" y1="108" x2="120" y2="22"/>';
  s += '<circle class="s-hub" cx="120" cy="108" r="5"/></svg>';
  el.innerHTML = s;
}
function animateSMeter(ts) {
  if (ts - sMeterNoiseAt > 200) {
    sMeterNoiseAt = ts;
    sMeterNoise = sMeterTarget > 0 ? (Math.random() - 0.5) * 4 : 0;
  }
  const goal = Math.max(0, Math.min(100, sMeterTarget + sMeterNoise));
  sMeterVel += (goal - sMeterShown) * 0.09;   // spring toward the reading
  sMeterVel *= 0.78;                            // damping → gentle overshoot
  sMeterShown = Math.max(-4, Math.min(104, sMeterShown + sMeterVel));
  const n = document.getElementById('s-needle');
  if (n) n.setAttribute('transform', 'rotate(' + (sMeterShown / 100 * 108 - 54).toFixed(2) + ' 120 108)');
  requestAnimationFrame(animateSMeter);
}
buildSMeter('s-gauge');
requestAnimationFrame(animateSMeter);

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
function fmtFreq(mhz) {
  // Ham-radio grouping MHz.kHz.10Hz (e.g. 7.208.98) — exact dial, not rounded.
  if (!mhz) return '--.---.--';
  const hz = Math.round(mhz * 1e6);
  const M = Math.floor(hz / 1e6);
  const k = Math.floor((hz % 1e6) / 1e3).toString().padStart(3, '0');
  const h = Math.floor((hz % 1e3) / 10).toString().padStart(2, '0');
  return `${M}.${k}.${h}`;
}
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
function renderMeters(el, meters) {
  if (!el) return;
  const keys = Object.keys(meters || {});
  if (!keys.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = 'flex';
  el.innerHTML = keys.map(k => {
    let color = 'var(--cyan)';
    if (k === 'SWR') { const n = parseFloat(meters[k]); if (meters[k] === 'HIGH' || n > 2) color = 'var(--red)'; else if (n > 1.5) color = 'var(--amber)'; }
    return `<div class="chip">${esc(k)} <b style="color:${color}">${esc(String(meters[k]))}</b></div>`;
  }).join('');
}

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
  // toggle tx/rx via classList so the scope's has-scope class (added elsewhere)
  // isn't wiped every poll
  freqPanel.classList.remove('tx', 'rx');
  if (r.tx) {
    air.className = 'pill tx'; airDot.className = 'dot tx'; airTxt.textContent = 'TRANSMITTING';
    freqPanel.classList.add('tx');
  } else if (r.online) {
    air.className = 'pill live'; airDot.className = 'dot live'; airTxt.textContent = 'ON AIR';
    freqPanel.classList.add('rx');
  } else {
    air.className = 'pill'; airDot.className = 'dot'; airTxt.textContent = 'STANDBY';
  }

  // frequency hero
  $('freq').textContent = fmtFreq(r.freq_mhz);
  $('chip-band').textContent = r.band || '—';
  $('chip-mode').textContent = r.mode || '—';   // rig mode (LSB/USB/CW) from CAT
  // Digital is decided by the rig mode, not whether WSJT-X is open (so WSJT-X
  // left running on SSB reads as voice). FT-450 digital = a data/USER sub-mode.
  const modeU = (r.mode || '').toUpperCase();
  const inDigital = /FT8|FT4|JT|Q65|MSK|FST4|WSPR|JS8|USER|DATA|DIG|PKT|RTTY|FSK|-D\b/.test(modeU);
  const digi = $('chip-digi');
  if (digi) {
    if (inDigital && r.digital_mode) { digi.style.display = ''; digi.textContent = r.digital_mode; }
    else { digi.style.display = 'none'; }
  }
  const txrx = $('chip-txrx');
  if (r.tx) { txrx.textContent = 'TRANSMIT'; txrx.className = 'chip tx'; }
  else if (r.online) { txrx.textContent = 'RECEIVE'; txrx.className = 'chip rx'; }
  else { txrx.textContent = 'STANDBY'; txrx.className = 'chip'; }
  $('chip-src').textContent = r.online ? (r.source || '—') : 'NO SIGNAL';
  $('dxline').textContent = (inDigital && r.dx_call) ? `▸ WORKING ${r.dx_call}${r.report ? ' · ' + r.report : ''}` : '';

  // live rig meters (power + SWR / ALC / S / … whatever Hamlib reports)
  const meters = {};
  if (r.power_w != null) meters.PWR = r.power_w + ' W';
  Object.assign(meters, r.meters || {});
  renderMeters($('sk-meters'), meters);

  // S-meter
  $('s-val').textContent = sig.s_meter || '—';
  sMeterTarget = sig.s_pct || 0;   // animator eases the bar toward this
  $('s-snr').textContent = sig.snr == null ? '—' : (sig.snr > 0 ? '+' : '') + sig.snr + ' dB';
  $('s-snr').style.color = snrColor(sig.snr);
  $('s-mode').textContent = (inDigital && r.digital_mode) ? r.digital_mode : (r.mode || '—');

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

/* ── SDR band scope (spectrum + waterfall) at the bottom of the freq card ── */
const scope = { sx: null, fx: null, floor: -110, peak: -40, water: false, n: 0 };
function scopeColor(t) {
  t = Math.max(0, Math.min(1, t));
  const s = [[4,10,30],[12,40,110],[20,120,190],[40,210,180],[230,220,90],[255,120,40],[255,255,255]];
  const f = t * (s.length - 1), i = Math.floor(f), g = f - i;
  const a = s[i], b = s[Math.min(s.length - 1, i + 1)];
  return [a[0]+(b[0]-a[0])*g, a[1]+(b[1]-a[1])*g, a[2]+(b[2]-a[2])*g];
}
function drawScope(frame) {
  const bins = frame.bins || [], n = bins.length;
  if (!n) return;
  const spec = $('sk-spec'), fall = $('sk-fall');
  if (!scope.sx) { scope.sx = spec.getContext('2d'); scope.fx = fall.getContext('2d'); }
  if (scope.n !== n) { spec.width = n; fall.width = n; fall.height = 120; scope.n = n; scope.water = false; }
  const sh = 60; spec.height = sh;
  let mn = Infinity, mx = -Infinity;
  for (const v of bins) { if (v < mn) mn = v; if (v > mx) mx = v; }
  scope.floor += (mn - scope.floor) * 0.1; scope.peak += (mx - scope.peak) * 0.1;
  const lo = scope.floor - 3, hi = Math.max(scope.peak + 3, lo + 12), rng = hi - lo, sx = scope.sx;
  sx.clearRect(0, 0, n, sh); sx.beginPath(); sx.moveTo(0, sh);
  for (let i = 0; i < n; i++) sx.lineTo(i, sh - Math.max(0, Math.min(1, (bins[i]-lo)/rng)) * (sh-2));
  sx.lineTo(n, sh); sx.closePath(); sx.fillStyle = 'rgba(63,224,207,0.16)'; sx.fill();
  sx.beginPath();
  for (let i = 0; i < n; i++) { const y = sh - Math.max(0, Math.min(1, (bins[i]-lo)/rng)) * (sh-2); i ? sx.lineTo(i, y) : sx.moveTo(i, y); }
  sx.strokeStyle = '#7ff0e2'; sx.lineWidth = 1; sx.stroke();
  sx.strokeStyle = 'rgba(246,168,33,0.85)'; sx.beginPath(); sx.moveTo(n/2, 0); sx.lineTo(n/2, sh); sx.stroke();
  const fx = scope.fx, H2 = fall.height;
  if (!scope.water) { fx.fillStyle = '#030a12'; fx.fillRect(0, 0, n, H2); scope.water = true; }
  fx.drawImage(fall, 0, 0, n, H2, 0, -1, n, H2);
  const row = fx.createImageData(n, 1);
  for (let i = 0; i < n; i++) { const c = scopeColor((bins[i]-lo)/rng); row.data[i*4]=c[0]; row.data[i*4+1]=c[1]; row.data[i*4+2]=c[2]; row.data[i*4+3]=255; }
  fx.putImageData(row, 0, H2 - 1);
  const dial = frame.dial_hz || frame.center_hz || 0;
  if (dial) $('sk-scope-freq').textContent = (dial/1e6).toFixed(3) + ' MHz';
}
async function pollScope() {
  try {
    const r = await fetch('/api/spectrum', { cache: 'no-store' });
    const frame = await r.json();
    const fresh = frame && frame.bins && frame.bins.length &&
      (Date.now()/1000 - (frame.recv_ts || frame.ts || 0)) < 12;
    const card = $('sk-scope'), fp = $('freq-panel');
    if (fresh) {
      if (card.style.display === 'none') card.style.display = '';
      if (fp) fp.classList.add('has-scope');
      drawScope(frame);
    } else if (card.style.display !== 'none') {
      card.style.display = 'none';
      if (fp) fp.classList.remove('has-scope');
    }
  } catch (e) { /* ignore */ }
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
pollScope();
setInterval(pollScope, 350);
