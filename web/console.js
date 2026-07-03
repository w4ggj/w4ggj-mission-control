/* ============================================================
   W4GGJ Mission Control — SDR Console (/console)
   Rig header + geographic DX map (grey line / sun / great-circle
   arcs, reusing WORLD_LAND from worldmap.js) + a live SDR
   spectrum & waterfall that follows the radio via /api/spectrum.
   ============================================================ */
const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const D2R = Math.PI / 180, R2D = 180 / Math.PI;
const W = 1000, H = 500;
const X = (lon) => (lon + 180) / 360 * W;
const Y = (lat) => (90 - lat) / 180 * H;
const el = (t, a) => { const n = document.createElementNS(NS, t); for (const k in a) n.setAttribute(k, a[k]); return n; };

const BANDS = [
  ['160M', 1.8, 2.0], ['80M', 3.5, 4.0], ['60M', 5.3, 5.4], ['40M', 7.0, 7.3],
  ['30M', 10.1, 10.15], ['20M', 14.0, 14.35], ['17M', 18.06, 18.17], ['15M', 21.0, 21.45],
  ['12M', 24.89, 24.99], ['10M', 28.0, 29.7], ['6M', 50, 54],
];

/* approximate lat/lon per DXCC prefix — enough to scatter spots geographically.
   All keys quoted (some DXCC prefixes start with a digit). */
const PFX = {
  'K':[39,-98],'W':[39,-98],'N':[39,-98],'A':[39,-98],'VE':[56,-106],'VA':[56,-106],'VO':[48,-56],'VY':[64,-110],
  'XE':[23,-102],'CO':[22,-80],'KP4':[18,-66],'HI':[19,-70],'HH':[19,-72],'TI':[10,-84],'HP':[9,-80],'YN':[13,-85],
  'HR':[15,-87],'YS':[14,-89],'TG':[15,-90],'PJ':[12,-69],'FM':[15,-61],'J3':[12,-62],'J6':[14,-61],'8P':[13,-59],
  '9Y':[10,-61],'ZF':[19,-81],'C6':[24,-76],'VP2':[18,-63],'PY':[-10,-52],'LU':[-38,-63],'CE':[-33,-70],'CX':[-33,-56],
  'CP':[-17,-64],'OA':[-10,-76],'HK':[4,-73],'HC':[-1,-78],'YV':[7,-66],'PZ':[4,-56],'8R':[5,-58],'ZP':[-23,-58],
  'G':[52,-1],'M':[52,-1],'GM':[57,-4],'GW':[52,-3],'GI':[54,-6],'GD':[54,-4],'EI':[53,-8],'F':[47,2],'ON':[50,4],
  'PA':[52,5],'DL':[51,10],'DK':[51,10],'DJ':[51,10],'DB':[51,10],'OE':[47,14],'HB9':[47,8],'OK':[50,15],'OM':[48,19],
  'SP':[52,20],'S5':[46,15],'9A':[45,16],'YU':[44,21],'YT':[44,21],'LZ':[43,25],'YO':[46,25],'EA':[40,-4],'CT':[39,-8],
  'EA6':[39,3],'IT9':[37,14],'I':[42,12],'IK':[42,12],'IZ':[42,12],'SM':[62,15],'SA':[62,15],'LA':[62,10],'OZ':[56,10],
  'OH':[62,26],'ES':[59,26],'YL':[57,25],'LY':[55,24],'SV':[39,22],'SV9':[35,25],'EA8':[28,-16],'CU':[38,-28],'CT3':[33,-17],
  'UA':[56,38],'R':[56,38],'UA9':[55,80],'RA':[56,38],'UT':[49,32],'UR':[49,32],'EW':[53,28],'ER':[47,29],'4L':[42,44],
  'EK':[40,45],'4J':[40,48],'UN':[48,67],'EX':[41,74],'EY':[39,71],'UK':[41,64],'TA':[39,35],'4X':[31,35],'5B':[35,33],
  'OD':[34,36],'YK':[35,38],'YI':[33,44],'9K':[29,48],'A4':[21,57],'A6':[24,54],'A7':[25,51],'A9':[26,50],'HZ':[24,45],
  'YA':[34,66],'AP':[30,70],'4S':[7,81],'8Q':[3,73],'VU':[22,79],'S2':[24,90],'9N':[28,84],'XZ':[21,96],'HS':[15,101],
  'XV':[16,106],'XU':[13,105],'3W':[16,106],'9M':[3,102],'9V':[1,104],'DU':[13,122],'BV':[24,121],'BY':[35,105],
  'B':[35,105],'JA':[36,138],'JH':[36,138],'JR':[36,138],'HL':[37,128],'DS':[37,128],'P5':[40,127],'VK':[-25,134],
  'ZL':[-41,174],'KH6':[20,-157],'FK':[-21,165],'YB':[-6,107],'9G':[8,-1],'5N':[9,8],'TU':[7,-5],'TY':[9,2],
  '5V':[8,1],'XT':[12,-1],'TZ':[17,-4],'6W':[14,-14],'C5':[13,-15],'J5':[12,-15],'9L':[8,-11],'EL':[6,-9],
  'TL':[7,20],'TJ':[6,12],'TR':[-1,11],'TN':[-4,15],'9Q':[-4,15],'D2':[-12,17],'9J':[-15,28],'Z2':[-19,29],
  '7Q':[-13,34],'C9':[-18,35],'V5':[-22,17],'A2':[-22,24],'ZS':[-29,24],'3DA':[-26,31],'5R':[-19,46],'5Z':[1,38],
  '5H':[-6,35],'5X':[1,32],'9X':[-2,30],'ET':[9,40],'T5':[5,46],'ST':[15,30],'SU':[26,30],'5A':[27,17],'3V':[34,9],
  '7X':[28,3],'CN':[32,-6],'EA9':[35,-3],'FR':[-21,55],'3B8':[-20,57],'VP8':[-51,-59],
};
const PKEYS = Object.keys(PFX).sort((a, b) => b.length - a.length);
function locate(call) {
  call = (call || '').toUpperCase().replace(/^[A-Z0-9]+\//, '');   // drop portable prefix like PA/
  for (const k of PKEYS) if (call.startsWith(k)) return PFX[k];
  return null;
}

/* ── map geometry (grey line + arcs), same math as worldmap.js ── */
function subsolar(now) {
  const start = Date.UTC(now.getUTCFullYear(), 0, 0);
  const N = Math.floor((now - start) / 86400000);
  const decl = -23.44 * Math.cos(D2R * (360 / 365) * (N + 10));
  const hrs = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
  let lon = -15 * (hrs - 12);
  lon = ((lon + 180) % 360 + 360) % 360 - 180;
  return { lat: decl, lon };
}
function terminatorD(now) {
  const s = subsolar(now);
  const decl = (Math.abs(s.lat) < 0.5 ? (s.lat >= 0 ? 0.5 : -0.5) : s.lat) * D2R;
  let d = '';
  for (let lon = -180; lon <= 180; lon += 2) {
    const Hn = (lon - s.lon) * D2R;
    const lat = Math.atan(-Math.cos(Hn) / Math.tan(decl)) * R2D;
    d += (lon === -180 ? 'M' : 'L') + X(lon).toFixed(1) + ',' + Y(lat).toFixed(1) + ' ';
  }
  const poleY = s.lat >= 0 ? Y(-90) : Y(90);
  d += 'L' + X(180).toFixed(1) + ',' + poleY + ' L' + X(-180).toFixed(1) + ',' + poleY + ' Z';
  return { d, sun: s };
}
function arcD(a, b, n) {
  const la1 = a[0] * D2R, lo1 = a[1] * D2R, la2 = b[0] * D2R, lo2 = b[1] * D2R;
  const dd = 2 * Math.asin(Math.sqrt(Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
  if (!dd) return '';
  let d = '', prev = null, started = false;
  for (let i = 0; i <= n; i++) {
    const f = i / n, A = Math.sin((1 - f) * dd) / Math.sin(dd), B = Math.sin(f * dd) / Math.sin(dd);
    const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
    const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
    const z = A * Math.sin(la1) + B * Math.sin(la2);
    const lat = Math.atan2(z, Math.hypot(x, y)) * R2D, lon = Math.atan2(y, x) * R2D;
    if (prev !== null && Math.abs(lon - prev) > 180) started = false;
    d += (started ? 'L' : 'M') + X(lon).toFixed(1) + ',' + Y(lat).toFixed(1) + ' ';
    started = true; prev = lon;
  }
  return d;
}
const BAND_COL = { '160m':'#8e7cc3','80m':'#6d7fd6','60m':'#4f8fe6','40m':'#33b1e0','30m':'#26c6b8','20m':'#4fd08a','17m':'#8fd45f','15m':'#d0d84a','12m':'#ffcf4d','10m':'#ffab3d','6m':'#ff7a45' };
const bandCol = (b) => BAND_COL[(b || '').toLowerCase()] || '#3fe0cf';

let mapL = {}, station = null;
function buildMap() {
  const svg = $('c-map'); svg.innerHTML = '';
  svg.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: 'var(--ocean)' }));
  const grat = el('g', { class: 'wmc-grat' });
  let gd = '';
  for (let lon = -150; lon <= 150; lon += 30) gd += `M${X(lon).toFixed(1)},0 L${X(lon).toFixed(1)},${H} `;
  for (let lat = -60; lat <= 60; lat += 30) gd += `M0,${Y(lat).toFixed(1)} L${W},${Y(lat).toFixed(1)} `;
  grat.appendChild(el('path', { d: gd })); svg.appendChild(grat);
  // WORLD_LAND is a top-level const in worldmap.js (loaded first) — reachable by
  // bare name across scripts, but NOT as a window property, so guard with typeof.
  if (typeof WORLD_LAND !== 'undefined') svg.appendChild(el('path', { class: 'wmc-land', d: WORLD_LAND }));
  mapL.term = el('path', { class: 'wmc-term' }); svg.appendChild(mapL.term);
  mapL.arcs = el('g'); svg.appendChild(mapL.arcs);
  mapL.spots = el('g'); svg.appendChild(mapL.spots);
  mapL.sun = el('circle', { class: 'wmc-sun', r: 5, cx: -9, cy: -9 }); svg.appendChild(mapL.sun);
  mapL.home = el('g'); svg.appendChild(mapL.home);
}
function drawTerminator() {
  const { d, sun } = terminatorD(new Date());
  mapL.term.setAttribute('d', d);
  mapL.sun.setAttribute('cx', X(sun.lon).toFixed(1));
  mapL.sun.setAttribute('cy', Y(sun.lat).toFixed(1));
}
function drawMapData(s) {
  const map = s.map || {}, dx = s.dx || [];
  if (map.station) { station = map.station; mapL.home.innerHTML = '';
    mapL.home.appendChild(el('circle', { class: 'wmc-home', r: 3.5, cx: X(station[1]).toFixed(1), cy: Y(station[0]).toFixed(1) })); }
  // great-circle arcs to recent contacts
  mapL.arcs.innerHTML = '';
  if (station) (map.recent || []).slice(0, 40).forEach(q => {
    const d = arcD(station, [q.lat, q.lon], 40);
    if (d) mapL.arcs.appendChild(el('path', { class: 'wmc-arc', d, stroke: bandCol(q.band) }));
  });
  // callsign spots — my recent QSOs (cyan) + live DX-cluster spots (amber)
  mapL.spots.innerHTML = '';
  const put = (ll, call, dxSpot) => {
    if (!ll) return;
    const x = X(ll[1]), y = Y(ll[0]);
    mapL.spots.appendChild(el('circle', { class: dxSpot ? 'wmc-dot-dx' : 'wmc-dot', r: dxSpot ? 2 : 1.6, cx: x.toFixed(1), cy: y.toFixed(1) }));
    const t = el('text', { class: 'wmc-spot' + (dxSpot ? ' wmc-spot-dx' : ''), x: (x + 3).toFixed(1), y: (y + 2.5).toFixed(1) });
    t.textContent = call; mapL.spots.appendChild(t);
  };
  (map.recent || []).slice(0, 28).forEach(q => put([q.lat, q.lon], q.call, false));
  dx.slice(0, 24).forEach(d => put(locate(d.dx), d.dx, true));
}

/* ── rig header ─────────────────────────────────────────── */
function fmtFreq(hz) {
  if (!hz) return '--.---.---';
  const M = Math.floor(hz / 1e6);
  const k = Math.floor((hz % 1e6) / 1e3).toString().padStart(3, '0');
  const h = Math.floor(hz % 1e3).toString().padStart(3, '0');
  return `${M}.${k}.${h}`;
}
let bandsBuilt = false;
function buildBands() {
  const box = $('c-bands');
  box.innerHTML = BANDS.map(b => `<span class="band-btn" data-b="${b[0]}">${b[0]}</span>`).join('');
  bandsBuilt = true;
}
function sLabelDbm(sig) {
  // map s_pct (0..100, where S9≈60%) to an S label + dBm estimate
  const label = sig.s_meter && sig.s_meter !== '—' ? sig.s_meter : null;
  let dbm = null;
  if (label) {
    const m = /S(\d+)\s*\+?\s*(\d+)?/.exec(label);
    if (m) { const s = +m[1], plus = +(m[2] || 0); dbm = -127 + s * 6 + plus; }
  } else if (sig.s_pct) { dbm = -121 + (sig.s_pct / 100) * 73; }
  return { label: label || (sig.s_pct ? 'S' + Math.round(sig.s_pct / 11) : 'S—'), dbm };
}
function renderRig(s) {
  if (!bandsBuilt) buildBands();
  const r = s.radio || {}, sig = s.signal || {};
  const hz = r.rx_hz || r.dial_hz || Math.round((r.freq_mhz || 0) * 1e6);
  $('c-freq').textContent = fmtFreq(hz);
  const mhz = hz / 1e6;
  document.querySelectorAll('.band-btn').forEach(b => {
    const def = BANDS.find(x => x[0] === b.dataset.b);
    b.classList.toggle('on', def && mhz >= def[1] && mhz <= def[2]);
  });
  $('c-mode').textContent = r.mode || '—';
  const modeU = (r.mode || '').toUpperCase();
  const digi = /FT8|FT4|JT|Q65|MSK|FST4|WSPR|JS8|USER|DATA|DIG|PKT|RTTY|FSK/.test(modeU);
  const dg = $('c-digi');
  if (digi && r.digital_mode) { dg.style.display = ''; dg.textContent = r.digital_mode; } else dg.style.display = 'none';
  $('c-src').textContent = r.online ? (r.source || '—').toUpperCase() : 'NO RIG';
  const tx = $('c-txrx');
  if (r.tx) { tx.textContent = 'TRANSMIT'; tx.className = 'txrx tx'; }
  else if (r.online) { tx.textContent = 'RECEIVE'; tx.className = 'txrx'; }
  else { tx.textContent = 'STANDBY'; tx.className = 'txrx off'; }
  $('c-sm-fill').style.width = Math.max(0, Math.min(100, sig.s_pct || 0)) + '%';
  const sd = sLabelDbm(sig);
  $('c-dbm').textContent = sd.label + (sd.dbm != null ? ` · ${sd.dbm} dBm` : '');
  $('c-ovl').innerHTML = `A: <b>${hz ? hz.toLocaleString() : '—'}</b> Hz &nbsp; ${sd.label}` +
    (r.band ? ` &nbsp; ${r.band}` : '');
}

/* ── SDR spectrum + waterfall ───────────────────────────── */
let spec, specX, fall, fallX, waterInit = false, floorEMA = -110, peakEMA = -40;
function initPan() {
  spec = $('c-spec'); fall = $('c-fall');
  specX = spec.getContext('2d'); fallX = fall.getContext('2d');
}
function colormap(t) {                       // 0..1 -> waterfall color
  t = Math.max(0, Math.min(1, t));
  const stops = [[4,10,30],[12,40,110],[20,120,190],[40,210,180],[230,220,90],[255,120,40],[255,255,255]];
  const f = t * (stops.length - 1), i = Math.floor(f), g = f - i;
  const a = stops[i], b = stops[Math.min(stops.length - 1, i + 1)];
  return [a[0] + (b[0] - a[0]) * g, a[1] + (b[1] - a[1]) * g, a[2] + (b[2] - a[2]) * g];
}
function drawSpectrum(frame) {
  const bins = frame.bins || [];
  const n = bins.length; if (!n) return;
  $('c-wait').style.display = 'none';
  // size canvases to bin count (device px); CSS scales to width
  if (spec.width !== n) { spec.width = n; fall.width = n; fall.height = 118; waterInit = false; }
  const sh = 70; spec.height = sh;
  // auto-scale with smoothed floor/peak
  let mn = Infinity, mx = -Infinity;
  for (const v of bins) { if (v < mn) mn = v; if (v > mx) mx = v; }
  // black point at the (slow-tracked) noise floor + wide min range so flat noise
  // stays dark instead of painting the whole waterfall yellow
  floorEMA += (mn - floorEMA) * 0.05; peakEMA += (mx - peakEMA) * 0.2;
  const lo = floorEMA, hi = Math.max(peakEMA, lo + 36), rng = hi - lo;
  // spectrum trace
  specX.clearRect(0, 0, n, sh);
  specX.beginPath(); specX.moveTo(0, sh);
  for (let i = 0; i < n; i++) {
    const y = sh - Math.max(0, Math.min(1, (bins[i] - lo) / rng)) * (sh - 2);
    specX.lineTo(i, y);
  }
  specX.lineTo(n, sh); specX.closePath();
  specX.fillStyle = 'rgba(63,224,207,0.18)'; specX.fill();
  specX.beginPath();
  for (let i = 0; i < n; i++) {
    const y = sh - Math.max(0, Math.min(1, (bins[i] - lo) / rng)) * (sh - 2);
    if (i === 0) specX.moveTo(i, y); else specX.lineTo(i, y);
  }
  specX.strokeStyle = '#7ff0e2'; specX.lineWidth = 1; specX.stroke();
  // center (dial) marker
  specX.strokeStyle = 'rgba(246,168,33,0.8)'; specX.beginPath();
  specX.moveTo(n / 2, 0); specX.lineTo(n / 2, sh); specX.stroke();
  // waterfall: scroll up 1px, draw new row at bottom
  const H2 = fall.height;
  if (!waterInit) { fallX.fillStyle = '#030a12'; fallX.fillRect(0, 0, n, H2); waterInit = true; }
  fallX.drawImage(fall, 0, 0, n, H2, 0, -1, n, H2);
  const row = fallX.createImageData(n, 1);
  for (let i = 0; i < n; i++) {
    const c = colormap((bins[i] - lo) / rng);
    row.data[i * 4] = c[0]; row.data[i * 4 + 1] = c[1]; row.data[i * 4 + 2] = c[2]; row.data[i * 4 + 3] = 255;
  }
  fallX.putImageData(row, 0, H2 - 1);
  // axis labels: dial ± span/2
  const dial = frame.dial_hz || frame.center_hz || 0, span = frame.span_hz || 0;
  if (dial && span) {
    const f = (x) => (x / 1e6).toFixed(3);
    $('c-axis').innerHTML = `<span>${f(dial - span / 2)}</span><span>${f(dial - span / 4)}</span>` +
      `<span style="color:var(--amber)">${f(dial)}</span><span>${f(dial + span / 4)}</span><span>${f(dial + span / 2)} MHz</span>`;
  }
}
async function pollSpectrum() {
  try {
    const r = await fetch('/api/spectrum', { cache: 'no-store' });
    const frame = await r.json();
    if (frame && frame.bins && frame.bins.length) {
      const age = Date.now() / 1000 - (frame.recv_ts || frame.ts || 0);
      if (age < 5) drawSpectrum(frame);
    }
  } catch (e) { /* ignore */ }
}

/* ── state poll ─────────────────────────────────────────── */
let firstMap = true;
async function pollState() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const s = await r.json();
    renderRig(s);
    drawMapData(s);
    $('c-conn').className = 'conn live'; $('c-conn-t').textContent = 'LINKED';
  } catch (e) {
    $('c-conn').className = 'conn'; $('c-conn-t').textContent = 'NO LINK';
  }
}

/* ── boot ───────────────────────────────────────────────── */
buildMap();
initPan();
drawTerminator();
setInterval(drawTerminator, 60000);
pollState(); setInterval(pollState, 1000);
pollSpectrum(); setInterval(pollSpectrum, 80);
