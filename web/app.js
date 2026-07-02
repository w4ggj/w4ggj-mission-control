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
  $('q-utc').textContent = `${hh}:${mm}`;
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

/* ── render ───────────────────────────────────────────────── */
function render(s) {
  const id = s.identity || {};
  const r = s.radio || {};
  const sig = s.signal || {};
  const log = s.log || {};
  const sol = s.solar || {};

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

  // ── live radio ──
  $('freq').textContent = fmtFreq(r.freq_mhz);
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
    $('chip-dx').textContent = 'WORKING ' + r.dx_call + (r.report ? ' · ' + r.report : '');
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

  // ── quick stats + propagation ──
  $('q-sfi').textContent = sol.sfi || '—';
  $('q-kp').textContent = sol.k_index || '—';
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
        `<td class="call">${q.call}</td><td>${q.band}</td><td>${q.mode}</td>` +
        `<td>${q.rst_s || ''}${q.rst_r ? '/' + q.rst_r : ''}</td><td style="color:var(--muted)">${q.country || ''}</td>`;
      lb.appendChild(tr);
    });
  }
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
    $('q-iss').textContent = iss.range_km ? iss.range_km.toLocaleString() + ' km' : '—';
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

  firstLoad = false;
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
