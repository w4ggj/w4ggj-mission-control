/* ============================================================
   W4GGJ Mission Control — client
   Polls /api/state ~1s and paints every panel. UTC clock ticks
   locally. New decodes/QSOs flash in for a "live" feel.
   ============================================================ */

const $ = (id) => document.getElementById(id);
let lastQsoTs = 0;
let seenDecodeTs = 0;
let firstLoad = true;

/* ── live S-meter: ease toward the reading and gently wander in between, so the
   needle always moves like a real meter instead of stepping every decode ──── */
let sMeterTarget = 0, sMeterShown = 0, sMeterNoise = 0, sMeterNoiseAt = 0;
function animateSMeter(ts) {
  if (ts - sMeterNoiseAt > 170) {
    sMeterNoiseAt = ts;
    sMeterNoise = sMeterTarget > 0 ? (Math.random() - 0.5) * 5 : 0;
  }
  const goal = Math.max(0, Math.min(100, sMeterTarget + sMeterNoise));
  sMeterShown += (goal - sMeterShown) * 0.12;
  const m = $('s-mask');
  if (m) m.style.left = sMeterShown.toFixed(2) + '%';
  requestAnimationFrame(animateSMeter);
}
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

/* ── helpers ──────────────────────────────────────────────── */
function fmtFreq(mhz) {
  if (!mhz) return '--.---';
  return mhz.toFixed(3);
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

  if (r.dx_call) {
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
  $('s-mode').textContent = r.mode || '—';

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

  firstLoad = false;
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
