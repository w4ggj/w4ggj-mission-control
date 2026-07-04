/* ============================================================
   W4GGJ Mission Control — client
   Polls /api/state ~1s and paints every panel. UTC clock ticks
   locally. New decodes/QSOs flash in for a "live" feel.
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

/* ── UTC clock (local tick, no server round-trip) ─────────── */
function tickClock() {
  const d = new Date();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  $('utc').textContent = `${hh}:${mm}:${ss}`;
}
setInterval(tickClock, 250);
tickClock();

/* ── live station audio (MP3 stream) ──────────────────────── */
(function initStream() {
  const btn = $('stream-btn'), audio = $('stream-audio');
  if (!btn || !audio) return;
  const txt = btn.querySelector('.stream-txt');
  function set(state, label) {
    // .playing drives the icon↔equalizer swap in CSS
    btn.classList.toggle('playing', state === 'playing');
    btn.classList.toggle('loading', state === 'loading');
    btn.classList.toggle('offline', state === 'offline');
    txt.textContent = label || ({
      playing: 'LIVE · ON AIR', loading: 'CONNECTING…',
      offline: 'STREAM OFFLINE', idle: 'LISTEN LIVE',
    }[state]);
  }
  btn.addEventListener('click', () => {
    if (audio.paused) {
      set('loading');
      audio.load();                 // jump to the live edge each time
      const p = audio.play();
      if (p) p.catch(() => set('offline'));
      // clear a stale "offline" if it starts within a few seconds
      setTimeout(() => { if (!audio.paused) set('playing'); }, 300);
    } else {
      audio.pause();
    }
  });
  audio.addEventListener('playing', () => set('playing'));
  audio.addEventListener('waiting', () => { if (!audio.paused) set('loading'); });
  audio.addEventListener('pause', () => set('idle'));
  audio.addEventListener('error', () => set('offline'));
  set('idle');
})();

/* ── helpers ──────────────────────────────────────────────── */
function fmtFreq(mhz) {
  // Ham-radio grouping MHz.kHz.10Hz (e.g. 7.208.98) so the exact dial shows
  // instead of a rounded 3-decimal value. Round frequencies read like 14.074.00.
  if (!mhz) return '--.---.--';
  const hz = Math.round(mhz * 1e6);
  const M = Math.floor(hz / 1e6);
  const k = Math.floor((hz % 1e6) / 1e3).toString().padStart(3, '0');
  const h = Math.floor((hz % 1e3) / 10).toString().padStart(2, '0');
  return `${M}.${k}.${h}`;
}
function bandClass(cond) {
  const c = (cond || '').toLowerCase();
  if (c.includes('good')) return 'band-good';
  if (c.includes('fair')) return 'band-fair';
  if (c.includes('poor') || c.includes('closed')) return 'band-poor';
  return '';
}
function snrColor(snr) {
  if (snr == null) return 'var(--muted)';
  if (snr >= 0) return 'var(--green)';
  if (snr >= -12) return 'var(--cyan)';
  return 'var(--muted)';
}
function fmtDate(d) {
  if (!d || d.length < 8) return '—';
  return `${d.slice(4, 6)}/${d.slice(6, 8)}`;
}
function fmtTime(t) {
  if (!t || t.length < 4) return '—';
  return `${t.slice(0, 2)}:${t.slice(2, 4)}`;
}
function renderMeters(el, meters) {
  if (!el) return;
  const keys = meters ? Object.keys(meters) : [];
  if (!keys.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = 'flex';
  el.innerHTML = keys.map(k => {
    let color = 'var(--cyan)';
    if (k === 'SWR') { const n = parseFloat(meters[k]); if (meters[k] === 'HIGH' || n > 2) color = 'var(--red)'; else if (n > 1.5) color = 'var(--amber)'; }
    return `<div class="chip">${k} <b style="color:${color}">${meters[k]}</b></div>`;
  }).join('');
}

/* ── live "new contact" celebration flash ─────────────────── */
let flashTimer = null;
function flashNewContact(q) {
  if (CFG.enable_flash === false) return;
  const el = $('qso-flash');
  if (!el || !q || !q.call) return;
  const meta = [q.country, q.band, (q.mode || '').toUpperCase()].filter(Boolean).join(' · ');
  const when = (q.date && q.time) ? `${fmtDate(q.date)} · ${fmtTime(q.time)} UTC` : '';
  el.innerHTML =
    '<div class="qf-card">' +
      '<button class="qf-x" type="button" aria-label="Dismiss">×</button>' +
      '<div class="qf-head"><span class="qf-dot"></span>NEW CONTACT LOGGED</div>' +
      `<div class="qf-call">${(q.call || '').replace(/</g, '&lt;')}</div>` +
      (meta ? `<div class="qf-meta">${meta}</div>` : '') +
      (when ? `<div class="qf-time">${when}</div>` : '') +
    '</div>';
  // restart the entrance animation even if one is already showing
  el.classList.remove('show'); void el.offsetWidth; el.classList.add('show');
  const x = el.querySelector('.qf-x');
  if (x) x.onclick = () => el.classList.remove('show');
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => el.classList.remove('show'), 9000);
}

/* ── render ───────────────────────────────────────────────── */
/* ── live settings (from the shack Config page) ───────────── */
let CFG = {};
const SEC_TOGGLES = {
  show_records: 'records-bar', show_radio: 'sec-radio', show_decodes: 'sec-decodes',
  show_prop: 'sec-prop', show_log: 'sec-log', show_pota: 'sec-pota',
  show_space: 'sec-space', show_map: 'sec-map', show_awards: 'sec-awards',
  show_activity: 'sec-activity', show_psk: 'sec-psk',
};
function applySettings(c) {
  for (const key in SEC_TOGGLES) {
    const el = document.getElementById(SEC_TOGGLES[key]);
    if (el) el.style.display = (c[key] === false) ? 'none' : '';
  }
  const audio = document.getElementById('stream-wrap');
  if (audio) audio.style.display = (c.show_audio === false) ? 'none' : '';
}
function maskCall(call) { return (CFG.show_calls === false) ? '•••' : call; }

function render(s) {
  const id = s.identity || {};
  const r = s.radio || {};
  const sig = s.signal || {};
  const log = s.log || {};
  const sol = s.solar || {};

  // live dashboard settings — apply section visibility first
  CFG = s.settings || {};
  applySettings(CFG);
  if (CFG.tagline) {
    const parts = CFG.tagline.split('.');
    $('tag-a').textContent = (parts[0] || '').trim() + (parts.length > 1 ? '.' : '');
    $('tag-b').textContent = parts.slice(1).join('.').trim();
  }

  // ── portable / field op ──
  // While live telemetry is arriving from the field unit, flip the header to the
  // portable label, light the PORTABLE badge, and hide the (home-only) audio.
  const fld = s.field || {};
  const sub = fld.active ? (fld.label || 'POTA · PORTABLE')
                         : (CFG.subtitle || id.subtitle || '');
  if (sub) $('tb-sub').textContent = sub;
  const pp = $('portable-pill');
  if (pp) pp.style.display = fld.active ? '' : 'none';
  // Listen-Live is a home stream — hide it in the field (also honors show_audio).
  const audioWrap = $('stream-wrap');
  if (audioWrap) audioWrap.style.display = (CFG.show_audio === false || fld.active) ? 'none' : '';

  // identity (once)
  if (firstLoad) {
    $('tb-call').textContent = id.callsign || 'W4GGJ';
    $('hero-call').textContent = id.callsign || 'W4GGJ';
    $('tb-sub').textContent = id.subtitle || '';
    $('f-qth').textContent = id.qth || '—';
    $('f-grid').textContent = id.grid || '—';
    $('f-rig').textContent = id.rig || '—';
    $('f-pwr').textContent = (id.power_watts || '—') + ' W';
    if (id.tagline) {
      const parts = id.tagline.split('.');
      $('tag-a').textContent = (parts[0] || '').trim() + '.';
      $('tag-b').textContent = (parts.slice(1).join('.') || '').trim();
    }
    // PSKReporter live map — signals SENT by our callsign, last 12h.
    // Config can override with a full permalink via identity.pskreporter_url.
    const cs = (id.callsign || 'W4GGJ').toUpperCase();
    // Default: signals sent by our callsign (txrx=tx), last 15 min, with the
    // usual display toggles. A full PSKReporter Permalink in config
    // (identity.pskreporter_url) overrides this and carries every option exactly.
    const pskUrl = id.pskreporter_url ||
      `https://pskreporter.info/pskmap#preset&callsign=${encodeURIComponent(cs)}` +
      `&txrx=tx&timerange=900&hideunrec=1&blankifnone=1&hidepink=1&showlines=1&hidetime=1`;
    $('psk-call').textContent = cs;
    if ($('psk-map')) $('psk-map').src = pskUrl;
    if ($('psk-open')) $('psk-open').href = pskUrl;
  }

  // ── ON AIR / connection ──
  $('conn-dot').className = 'dot live';
  $('conn-txt').textContent = 'LINKED';
  $('conn').className = 'pill live';

  const air = $('onair'), airDot = $('air-dot'), airTxt = $('air-txt');
  if (r.tx) {
    air.className = 'pill tx'; airDot.className = 'dot tx'; airTxt.textContent = 'TRANSMITTING';
  } else if (r.online) {
    air.className = 'pill live'; airDot.className = 'dot live'; airTxt.textContent = 'ON AIR';
  } else {
    air.className = 'pill'; airDot.className = 'dot'; airTxt.textContent = 'STANDBY';
  }

  // Section 00 heading follows the rig: "Offline" when the radio is down or has
  // no frequency, otherwise the live "On The Air Now".
  const sec00 = $('sec00-title');
  if (sec00) sec00.textContent = (r.online && r.freq_mhz) ? 'On The Air Now' : 'Offline';

  // ── live radio ──
  $('freq').textContent = (CFG.show_freq === false) ? '••.•••' : fmtFreq(r.freq_mhz);
  $('chip-band').textContent = r.band || '—';
  $('chip-mode').textContent = r.mode || '—';
  const txrx = $('chip-txrx');
  if (r.tx) { txrx.textContent = 'TRANSMIT'; txrx.className = 'chip tx'; }
  else if (r.online) { txrx.textContent = 'RECEIVE'; txrx.className = 'chip rx'; }
  else { txrx.textContent = 'STANDBY'; txrx.className = 'chip'; }
  $('chip-src').textContent = r.online ? (r.source || '—') : 'NO SIGNAL';

  // "In digital" is decided by the RIG mode (from CAT), NOT by whether WSJT-X is
  // running — so you can leave WSJT-X open on SSB and it still reads as voice.
  // The FT-450 reports digital as a data/USER sub-mode (USER-U, DATA, PKT…);
  // with no CAT mode source the mode string is the WSJT-X submode itself.
  const modeU = (r.mode || '').toUpperCase();
  const inDigital = /FT8|FT4|JT|Q65|MSK|FST4|WSPR|JS8|USER|DATA|DIG|PKT|RTTY|FSK|-D\b/.test(modeU);

  // chip-mode shows the rig's real mode (LSB/USB/CW). The WSJT-X submode
  // (FT8/FT4) rides alongside in its own chip, only while actually in digital.
  const digi = $('chip-digi');
  if (digi) {
    if (inDigital && r.digital_mode) { digi.style.display = ''; digi.textContent = r.digital_mode; }
    else { digi.style.display = 'none'; }
  }

  // Voice vs digital: on voice/CW the decode + PSKReporter panels idle, so dim
  // them and flag it. The S-meter still runs off the rig meter either way.
  document.body.classList.toggle('voice-mode', !!r.online && !inDigital);

  if (inDigital && r.dx_call) {
    $('chip-dx-wrap').style.display = 'flex';
    $('chip-dx').textContent = 'WORKING ' + maskCall(r.dx_call) + (r.report ? ' · ' + r.report : '');
  } else {
    $('chip-dx-wrap').style.display = 'none';
  }

  // live power from the rig (Hamlib/HRD) when available, else the static rating
  const pwr = (r.power_w != null) ? r.power_w : id.power_watts;
  $('f-pwr').textContent = (pwr != null ? pwr : '—') + ' W';

  // live rig telemetry — power + whatever the radio reports (gains / SWR / …).
  // Only shown when a rig link is actually feeding data.
  const rig = {};
  if (r.power_w != null) rig.PWR = r.power_w + ' W';
  Object.assign(rig, r.meters || {});
  $('rig-wrap').style.display = Object.keys(rig).length ? 'block' : 'none';
  renderMeters($('chip-meters'), rig);

  // ── S-meter ──
  $('s-val').textContent = sig.s_meter || '—';
  sMeterTarget = sig.s_pct || 0;   // animator eases the bar toward this
  $('s-snr').textContent = sig.snr == null ? '—' : (sig.snr > 0 ? '+' : '') + sig.snr + ' dB';
  $('s-snr').style.color = snrColor(sig.snr);
  $('s-mode').textContent = (inDigital && r.digital_mode) ? r.digital_mode : (r.mode || '—');

  // ── decode feed ──
  const feed = $('feed');
  if (s.decodes && s.decodes.length) {
    feed.innerHTML = '';
    s.decodes.forEach(d => {
      const isCq = /^CQ/.test(d.msg || '');
      const isNew = d.ts > seenDecodeTs;
      const row = document.createElement('div');
      row.className = 'feed-row' + (isCq ? ' cq' : '') + (isNew && !firstLoad ? ' new' : '');
      row.innerHTML =
        `<div class="t">${d.time}</div>` +
        `<div class="snr" style="color:${snrColor(d.snr)}">${d.snr > 0 ? '+' : ''}${d.snr}</div>` +
        `<div style="color:var(--muted)">${d.df}</div>` +
        `<div class="msg">${(d.msg || '').replace(/</g, '&lt;')}</div>`;
      feed.appendChild(row);
    });
    seenDecodeTs = s.decodes[0].ts;
  }

  // ── propagation ──
  $('p-sfi').textContent = sol.sfi || '—';
  $('p-kp').textContent = sol.k_index || '—';
  $('p-ssn').textContent = sol.sunspots || '—';
  $('p-wind').textContent = sol.solar_wind ? sol.solar_wind + ' km/s' : '—';

  const bands = $('bands');
  if (sol.bands && sol.bands.length) {
    bands.innerHTML = '';
    sol.bands.forEach(b => {
      ['day', 'night'].forEach(period => {
        const el = document.createElement('div');
        el.className = 'band-chip ' + bandClass(b[period]);
        el.innerHTML = `<div class="bn">${b.band}</div><div class="bc">${period.toUpperCase()} · ${(b[period] || '—').toUpperCase()}</div>`;
        bands.appendChild(el);
      });
    });
  }

  // ── logbook ──
  $('f-qsos').textContent = log.total ? log.total.toLocaleString() : '—';
  $('l-total').textContent = log.total ? log.total.toLocaleString() : '—';
  $('l-calls').textContent = log.unique_calls || '—';
  $('l-ctry').textContent = log.countries || '—';
  $('l-bands').textContent = log.bands || '—';
  $('l-modes').textContent = log.modes || '—';
  $('l-path').textContent = log.adif_path || 'not found';
  if (log.best_dx) {
    $('l-dx').textContent = `${log.best_dx.call} · ${log.best_dx.km.toLocaleString()} km`;
  }

  const lb = $('log-body');
  const fresh = log.last_qso_ts && log.last_qso_ts > lastQsoTs && !firstLoad;
  if (log.recent && log.recent.length) {
    lb.innerHTML = '';
    log.recent.forEach((q, i) => {
      const tr = document.createElement('tr');
      if (fresh && i === 0) tr.className = 'fresh';
      tr.innerHTML =
        `<td>${fmtDate(q.date)}</td><td>${fmtTime(q.time)}</td>` +
        `<td class="call">${maskCall(q.call)}</td><td>${q.band}</td><td>${q.mode}</td>` +
        `<td>${q.rst_s || ''}${q.rst_r ? '/' + q.rst_r : ''}</td><td style="color:var(--muted)">${q.country || ''}</td>`;
      lb.appendChild(tr);
    });
  }
  // live "new contact" flash — the moment a QSO lands in the log
  if (fresh && log.recent && log.recent.length) flashNewContact(log.recent[0]);
  lastQsoTs = log.last_qso_ts || lastQsoTs;

  // ── POTA ──
  const pota = $('pota');
  if (s.pota && s.pota.length) {
    pota.innerHTML = '';
    s.pota.forEach(p => {
      const row = document.createElement('div');
      row.className = 'spot-row';
      row.innerHTML =
        `<div><span class="a">${p.call}</span> <span class="meta">${p.ref} · ${p.loc || ''}</span></div>` +
        `<div class="f">${p.freq}<div class="meta">${p.mode}</div></div>`;
      pota.appendChild(row);
    });
  } else {
    pota.innerHTML = '<div class="empty">No active POTA spots right now.</div>';
  }

  // ── ISS ──
  const iss = s.iss || {};
  if (iss.lat != null) {
    $('iss-pos').textContent = `${Math.abs(iss.lat)}°${iss.lat >= 0 ? 'N' : 'S'}  ${Math.abs(iss.lon)}°${iss.lon >= 0 ? 'E' : 'W'}`;
    $('iss-range').textContent = iss.range_km ? `RANGE: ${iss.range_km.toLocaleString()} km from ${id.grid || ''}` : '';
    $('iss-alt').textContent = iss.alt_km + ' km';
    $('iss-vel').textContent = iss.vel_kmh ? iss.vel_kmh.toLocaleString() + ' km/h' : '—';
  }

  // ── DX cluster ──
  const dx = $('dx');
  if (s.dx && s.dx.length) {
    dx.innerHTML = '';
    s.dx.forEach(d => {
      const row = document.createElement('div');
      row.className = 'spot-row';
      row.innerHTML =
        `<div><span class="a">${d.dx}</span> <span class="meta">${d.band} · de ${d.spotter}</span></div>` +
        `<div class="f">${d.freq}</div>`;
      dx.appendChild(row);
    });
  } else {
    dx.innerHTML = '<div class="empty">No DX spots yet.</div>';
  }

  // ── world reach map ──
  if (window.WorldMap && s.map) WorldMap.update(s.map);

  // ── awards (WAS / DXCC / grids) ──
  renderAwards(s.awards || {});

  // ── activity (calendar / hours / year / band / mode) ──
  renderActivity(s.activity || {}, log);

  // ── station records ribbon ──
  renderRecords(s.records || {}, log, s.awards || {});

  // ── live contest / run panel ──
  renderContest(s.contest || {});

  firstLoad = false;
}

/* ── live contest / run panel ─────────────────────────────── */
const BAND_COL = {
  '160m': '#8e7cc3', '80m': '#6d7fd6', '60m': '#4f8fe6', '40m': '#33b1e0',
  '30m': '#26c6b8', '20m': '#4fd08a', '17m': '#8fd45f', '15m': '#d0d84a',
  '12m': '#ffcf4d', '10m': '#ffab3d', '6m': '#ff7a45', '2m': '#ff5c6a', '70cm': '#ff6fae',
};
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function renderContest(c) {
  const sec = $('sec-contest');
  if (!sec) return;
  const now = Date.now() / 1000;
  const fresh = c.last_ts && (now - c.last_ts) < 21600;   // fade out ~6h after last QSO
  const minShow = (c.min_show == null) ? 5 : c.min_show;
  if (CFG.show_contest === false || !c.session || c.session < minShow || !fresh) {
    sec.style.display = 'none'; return;
  }
  sec.style.display = '';

  $('ct-rate').textContent = c.rate_10 || 0;
  $('ct-session').textContent = c.session || 0;
  $('ct-60').textContent = c.last_60 || 0;
  $('ct-best').textContent = c.best_hour || 0;
  const bands = c.bands || {};
  $('ct-nbands').textContent = Object.keys(bands).length;

  const live = $('ct-live');
  if (live) live.style.display = c.active ? '' : 'none';

  if (c.session_start) {
    const start = new Date(c.session_start * 1000);
    const hhmm = String(start.getUTCHours()).padStart(2, '0') + ':' +
                 String(start.getUTCMinutes()).padStart(2, '0');
    $('ct-elapsed').textContent = `RUN ${fmtDur(now - c.session_start)} · SINCE ${hhmm}Z`;
  } else {
    $('ct-elapsed').textContent = '—';
  }

  const entries = Object.entries(bands).sort((a, b) => b[1] - a[1]);
  const max = entries.length ? entries[0][1] : 1;
  $('ct-bars').innerHTML = entries.map(([b, n]) => {
    const w = Math.max(4, Math.round(n / max * 100));
    const col = BAND_COL[b] || 'var(--cyan)';
    return `<div class="ct-bar">
      <span class="ct-bl">${b}</span>
      <span class="ct-bt"><span class="ct-bf" style="width:${w}%;background:${col}"></span></span>
      <span class="ct-bn">${n}</span></div>`;
  }).join('') || '<span class="muted">—</span>';
}

/* ── station records ribbon (top of page) ─────────────────── */
function fmtDay(d) {                 // YYYYMMDD → "Mon D, YYYY"
  if (!d || d.length < 8) return '';
  return `${MONTHS[+d.slice(4, 6) - 1]} ${+d.slice(6, 8)}, ${d.slice(0, 4)}`;
}
function renderRecords(rec, log, aw) {
  $('rec-total').textContent = log.total ? log.total.toLocaleString() : '—';
  $('rec-years').textContent = rec.years_on_air || '—';
  if (rec.first_qso) $('rec-years-l').textContent = `SINCE ${fmtDay(rec.first_qso).toUpperCase()}`;
  $('rec-dxcc').textContent = (aw.dxcc || log.countries) || '—';
  const wasCount = (aw.was && aw.was.count) || 0;
  $('rec-states').textContent = wasCount ? `${wasCount}/50` : '—';
  $('rec-grids').textContent = (aw.grids || log.grids) ? (aw.grids || log.grids).toLocaleString() : '—';
  if (log.best_dx) {
    $('rec-dx').textContent = Math.round(log.best_dx.km).toLocaleString() + ' km';
    $('rec-dx-l').textContent = `FARTHEST · ${log.best_dx.call}`;
  }
  const mid = rec.most_in_day || {};
  $('rec-day').textContent = mid.count || '—';
  if (mid.date) $('rec-day-l').textContent = `BEST DAY · ${fmtDay(mid.date).replace(/, \d{4}$/, '')}`;
  const ls = rec.longest_streak || {};
  $('rec-streak').textContent = ls.days ? ls.days + (ls.days === 1 ? ' DAY' : ' DAYS') : '—';
}

/* ── activity: contribution calendar + hour/year/band/mode charts ── */
const SVGNS = 'http://www.w3.org/2000/svg';
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function ymd(d) {
  return '' + d.getUTCFullYear() + String(d.getUTCMonth() + 1).padStart(2, '0') +
    String(d.getUTCDate()).padStart(2, '0');
}
function svgEl(tag, attrs, txt) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (txt != null) n.textContent = txt;
  return n;
}
function calLevel(c, t) {           // 0 empty, then quartile buckets 1..4
  if (!c) return 0;
  if (c <= t[0]) return 1;
  if (c <= t[1]) return 2;
  if (c <= t[2]) return 3;
  return 4;
}
function renderActivity(act, log) {
  buildCalendar(act.daily || {});
  buildHours(act.hours || []);
  buildYears(act.by_year || {});
  buildBars('band-bars', log.band_breakdown || {}, true);
  buildBars('mode-bars', log.mode_breakdown || {}, false);
}
function buildCalendar(daily) {
  const svg = $('cal'); if (!svg) return;
  const CELL = 11, GAP = 3, STEP = CELL + GAP, WEEKS = 53;
  const padL = 30, padT = 16;
  const today = new Date(); today.setUTCHours(0, 0, 0, 0);
  const start = new Date(today);
  start.setUTCDate(start.getUTCDate() - ((WEEKS - 1) * 7 + today.getUTCDay()));
  // gather counts in-window for quartile thresholds
  const vals = [];
  for (let i = 0; i < WEEKS * 7; i++) {
    const d = new Date(start); d.setUTCDate(d.getUTCDate() + i);
    if (d > today) break;
    const c = daily[ymd(d)] || 0; if (c > 0) vals.push(c);
  }
  vals.sort((a, b) => a - b);
  const q = (p) => vals.length ? vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] : 0;
  const th = [q(0.25) || 1, q(0.5) || 2, q(0.75) || 3];
  const W = padL + WEEKS * STEP, H = padT + 7 * STEP;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = '';
  let total = 0, peak = { c: 0, d: null }, lastMonth = -1;
  for (let w = 0; w < WEEKS; w++) {
    for (let dow = 0; dow < 7; dow++) {
      const d = new Date(start); d.setUTCDate(d.getUTCDate() + w * 7 + dow);
      if (d > today) continue;
      const c = daily[ymd(d)] || 0; total += c;
      if (c > peak.c) peak = { c, d: new Date(d) };
      const rect = svgEl('rect', {
        x: padL + w * STEP, y: padT + dow * STEP, width: CELL, height: CELL,
        rx: 2, class: 'cal-cell l' + calLevel(c, th),
      });
      rect.appendChild(svgEl('title', {}, `${c} QSO${c === 1 ? '' : 's'} · ${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`));
      svg.appendChild(rect);
      // month label once per month, on the column holding its first week —
      // evenly ~4-5 columns apart, so no leading partial-month collision
      if (dow === 0 && d.getUTCDate() <= 7 && d.getUTCMonth() !== lastMonth) {
        lastMonth = d.getUTCMonth();
        svg.appendChild(svgEl('text', { x: padL + w * STEP, y: 11, class: 'cal-mon' }, MONTHS[lastMonth]));
      }
    }
  }
  ['Mon', 'Wed', 'Fri'].forEach((lab, i) => {
    svg.appendChild(svgEl('text', { x: 0, y: padT + (i * 2 + 1) * STEP + CELL - 2, class: 'cal-dow' }, lab));
  });
  $('cal-total').textContent = total.toLocaleString() + ' QSOs';
  $('cal-peak').textContent = peak.d
    ? `Best day: ${peak.c} on ${MONTHS[peak.d.getUTCMonth()]} ${peak.d.getUTCDate()}` : '';
}
function buildHours(hours) {
  const svg = $('hrs'); if (!svg) return;
  const W = 480, H = 170, padL = 8, padR = 8, padB = 20, padT = 16;
  const max = Math.max(1, ...hours);
  const peakH = hours.indexOf(max);
  const bw = (W - padL - padR - 23 * 3) / 24;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = '';
  const base = H - padB;
  for (let h = 0; h < 24; h++) {
    const v = hours[h] || 0, bh = v / max * (base - padT);
    const x = padL + h * (bw + 3);
    const r = svgEl('rect', {
      x, y: base - bh, width: bw, height: Math.max(bh, 0.5), rx: 3,
      class: 'act-bar' + (h === peakH ? ' peak' : ''),
    });
    r.appendChild(svgEl('title', {}, `${String(h).padStart(2, '0')}:00 UTC · ${v} QSOs`));
    svg.appendChild(r);
  }
  [0, 6, 12, 18].forEach(h => {
    svg.appendChild(svgEl('text', { x: padL + h * (bw + 3) + bw / 2, y: H - 6, class: 'act-ax' }, String(h).padStart(2, '0')));
  });
  $('hr-peak').textContent = max > 1 ? `· peak ${String(peakH).padStart(2, '0')}:00Z` : '';
}
function buildYears(byYear) {
  const svg = $('yrs'); if (!svg) return;
  const years = Object.keys(byYear).sort();
  const W = 480, H = 170, padL = 8, padR = 8, padB = 20, padT = 18;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = '';
  if (!years.length) return;
  const max = Math.max(1, ...years.map(y => byYear[y]));
  const base = H - padB, gap = 6;
  const bw = (W - padL - padR - (years.length - 1) * gap) / years.length;
  years.forEach((y, i) => {
    const v = byYear[y], bh = v / max * (base - padT), x = padL + i * (bw + gap);
    const r = svgEl('rect', { x, y: base - bh, width: bw, height: Math.max(bh, 0.5), rx: 3, class: 'act-bar' });
    r.appendChild(svgEl('title', {}, `${y} · ${v} QSOs`));
    svg.appendChild(r);
    if (bw >= 22) svg.appendChild(svgEl('text', { x: x + bw / 2, y: H - 6, class: 'act-ax' }, "'" + y.slice(2)));
    if (bw >= 22) svg.appendChild(svgEl('text', { x: x + bw / 2, y: base - bh - 4, class: 'act-val' }, v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v));
  });
}
function buildBars(id, breakdown, bandOrder) {
  const el = $(id); if (!el) return;
  let entries = Object.entries(breakdown).filter(([, v]) => v > 0);
  entries.sort((a, b) => b[1] - a[1]);
  entries = entries.slice(0, 10);
  const max = Math.max(1, ...entries.map(e => e[1]));
  el.innerHTML = entries.map(([k, v]) =>
    `<div class="bar-row"><span class="bar-lab">${k.toUpperCase()}</span>` +
    `<span class="bar-track"><span class="bar-fill" style="width:${(v / max * 100).toFixed(1)}%"></span></span>` +
    `<span class="bar-val">${v.toLocaleString()}</span></div>`
  ).join('') || '<div class="empty">No data yet…</div>';
}

/* ── awards: Worked-All-States map + DXCC / grid tallies ──── */
const WAS_5BANDS = ['80m', '40m', '20m', '15m', '10m'];   // classic 5-Band WAS
let usMapBuilt = false;
function buildUsMap() {
  const svg = $('usmap');
  if (!svg || !window.US_STATE_PATHS) return;
  let h = '';
  for (const code in US_STATE_PATHS) {
    h += `<path class="us-st" data-st="${code}" d="${US_STATE_PATHS[code]}"><title>${code}</title></path>`;
  }
  svg.innerHTML = h;
  usMapBuilt = true;
}
function renderAwards(a) {
  if (!usMapBuilt) buildUsMap();
  const was = a.was || { worked: [], count: 0 };
  const worked = new Set(was.worked || []);
  // light up worked states
  const svg = $('usmap');
  if (svg) svg.querySelectorAll('.us-st').forEach(p => {
    p.classList.toggle('worked', worked.has(p.getAttribute('data-st')));
  });
  const cnt = was.count || 0;
  $('aw-was').textContent = cnt || '—';
  $('aw-was').classList.toggle('green', cnt >= 50);
  $('aw-was').classList.toggle('cyan', cnt < 50);
  $('aw-was-sub').textContent = cnt >= 50 ? 'WAS COMPLETE ✦' : `of 50 · ${50 - cnt} to go`;
  $('aw-was-inline').textContent = `${cnt} / 50`;
  $('aw-dxcc').textContent = a.dxcc || '—';
  $('aw-grids').textContent = a.grids ? a.grids.toLocaleString() : '—';
  // 5-Band WAS: how many of the classic 5 bands have all 50 states
  const wb = a.was_bands || {};
  const full = WAS_5BANDS.filter(b => (wb[b] || 0) >= 50).length;
  $('aw-5bwas').textContent = `${full}/5`;
  // still-needed list
  const miss = $('us-missing'), lbl = $('us-missing-lbl');
  if (cnt >= 50) {
    if (lbl) lbl.style.display = 'none';
    if (miss) { miss.innerHTML = '<b class="green">All 50 states worked — WAS complete.</b>'; }
  } else if (miss) {
    if (lbl) lbl.style.display = '';
    const need = [...WAS_ALL].filter(st => !worked.has(st));
    miss.innerHTML = need.map(st => `<span class="us-chip">${st}</span>`).join('');
  }
}
const WAS_ALL = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];

/* ── SDR band scope (spectrum + waterfall) ────────────────── */
/* Renders /api/spectrum (fed by sdr_agent.py, relayed to the cloud) into the
   Live Radio section. The card only appears when a fresh SDR frame exists. */
const scope = { specX: null, fallX: null, floor: -110, peak: -40, water: false, n: 0 };
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
  const spec = $('scope-spec'), fall = $('scope-fall');
  if (!scope.specX) { scope.specX = spec.getContext('2d'); scope.fallX = fall.getContext('2d'); }
  if (scope.n !== n) { spec.width = n; fall.width = n; fall.height = 150; scope.n = n; scope.water = false; }
  const sh = 90; spec.height = sh;
  // colour scale: black point at a robust noise-floor estimate (25th percentile,
  // not the single lowest bin), wide min dynamic range so the noise floor stays
  // dark and only real signals climb the colours (a narrow range paints noise
  // solid yellow).
  const srt = bins.slice().sort((a, b) => a - b);
  const mn = srt[Math.floor(srt.length * 0.25)], mx = srt[srt.length - 1];
  scope.floor += (mn - scope.floor) * 0.1; scope.peak += (mx - scope.peak) * 0.2;
  const lo = scope.floor - 1, hi = Math.max(scope.peak, lo + 34), rng = hi - lo;
  const sx = scope.specX;
  sx.clearRect(0, 0, n, sh);
  sx.beginPath(); sx.moveTo(0, sh);
  for (let i = 0; i < n; i++) sx.lineTo(i, sh - Math.max(0, Math.min(1, (bins[i]-lo)/rng)) * (sh-2));
  sx.lineTo(n, sh); sx.closePath(); sx.fillStyle = 'rgba(63,224,207,0.16)'; sx.fill();
  sx.beginPath();
  for (let i = 0; i < n; i++) { const y = sh - Math.max(0, Math.min(1, (bins[i]-lo)/rng)) * (sh-2); i ? sx.lineTo(i, y) : sx.moveTo(i, y); }
  sx.strokeStyle = '#7ff0e2'; sx.lineWidth = 1; sx.stroke();
  sx.strokeStyle = 'rgba(246,168,33,0.85)'; sx.beginPath(); sx.moveTo(n/2, 0); sx.lineTo(n/2, sh); sx.stroke();
  const fx = scope.fallX, H2 = fall.height;
  if (!scope.water) { fx.fillStyle = '#030a12'; fx.fillRect(0, 0, n, H2); scope.water = true; }
  fx.drawImage(fall, 0, 0, n, H2, 0, 1, n, H2);   // flow downward: shift down, new row on top
  const row = fx.createImageData(n, 1);
  for (let i = 0; i < n; i++) { const c = scopeColor((bins[i]-lo)/rng); row.data[i*4]=c[0]; row.data[i*4+1]=c[1]; row.data[i*4+2]=c[2]; row.data[i*4+3]=255; }
  fx.putImageData(row, 0, 0);
  const dial = frame.dial_hz || frame.center_hz || 0, span = frame.span_hz || 0;
  if (dial) $('scope-freq').textContent = (dial/1e6).toFixed(3) + ' MHz';
  if (dial && span) {
    const f = (x) => (x/1e6).toFixed(3);
    $('scope-axis').innerHTML = `<span>${f(dial-span/2)}</span><span>${f(dial-span/4)}</span>` +
      `<span class="amber">${f(dial)}</span><span>${f(dial+span/4)}</span><span>${f(dial+span/2)} MHz</span>`;
  }
}
async function pollScope() {
  try {
    const r = await fetch('/api/spectrum', { cache: 'no-store' });
    const frame = await r.json();
    const fresh = frame && frame.bins && frame.bins.length &&
      (Date.now()/1000 - (frame.recv_ts || frame.ts || 0)) < 12 &&
      CFG.show_scope !== false;
    const card = $('scope-card');
    if (fresh) { if (card.style.display === 'none') card.style.display = ''; drawScope(frame); }
    else if (card.style.display !== 'none') card.style.display = 'none';
  } catch (e) { /* ignore — card stays hidden */ }
}

/* ── poll loop ────────────────────────────────────────────── */
async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    const s = await res.json();
    render(s);
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
