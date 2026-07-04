/* ============================================================
   W4GGJ Mission Control — Dashboard Config
   Reads/writes the live settings that shape the PUBLIC page.
   Writes go to the home agent (POST /api/settings), which
   relays them to the cloud — so a toggle here shows on the
   public site within a second. Read-only when opened from the
   cloud copy (the agent is authoritative).
   ============================================================ */
(function () {
  'use strict';

  // key, label, type ('toggle' default | 'num' | 'text'), optional hint
  const GROUPS = [
    { title: 'Public-page sections', items: [
      ['show_records', 'Records ribbon'],
      ['show_radio', 'Live Radio'],
      ['show_decodes', 'Live Decodes'],
      ['show_prop', 'Propagation'],
      ['show_log', 'Logbook'],
      ['show_pota', 'Field Ops (POTA)'],
      ['show_space', 'Space trackers'],
      ['show_map', 'World Reach map'],
      ['show_awards', 'Awards'],
      ['show_activity', 'Activity charts'],
      ['show_psk', 'PSKReporter'],
      ['show_contest', 'Contest / run panel'],
      ['show_audio', 'Listen-Live audio button'],
      ['show_scope', 'SDR band scope'],
    ]},
    { title: 'Privacy', items: [
      ['show_freq', 'Show exact frequency', 'toggle', 'Off masks the dial readout on the public page'],
      ['show_calls', 'Show worked callsigns', 'toggle', 'Off masks recent callsigns in the public logbook'],
    ]},
    { title: 'Features', items: [
      ['enable_flash', 'New-contact flash', 'toggle', 'The celebration popup when a QSO lands'],
    ]},
    { title: 'Contest panel tuning', items: [
      ['contest_min_qsos', 'Min QSOs to show the panel', 'num', '0 = show any run'],
      ['contest_gap_min', 'Run-break gap (minutes)', 'num', 'A longer pause ends the run'],
    ]},
    { title: 'Live text (blank = use station config)', items: [
      ['subtitle', 'Top-bar subtitle', 'text'],
      ['tagline', 'Hero tagline', 'text'],
    ]},
  ];

  const $ = (id) => document.getElementById(id);
  let readOnly = false;
  let saveTimer = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function build(settings) {
    const root = $('groups');
    root.innerHTML = '';
    GROUPS.forEach((g) => {
      const box = document.createElement('div');
      box.className = 'group';
      box.innerHTML = '<h2>' + esc(g.title) + '</h2>';
      g.items.forEach(([key, label, type, hint]) => {
        type = type || 'toggle';
        const row = document.createElement('div');
        row.className = 'row';
        const lab = '<div class="lab">' + esc(label) +
          (hint ? '<small>' + esc(hint) + '</small>' : '') + '</div>';
        let ctrl;
        const val = settings[key];
        if (type === 'toggle') {
          ctrl = '<label class="sw"><input type="checkbox" data-key="' + key + '"' +
            (val !== false ? ' checked' : '') + (readOnly ? ' disabled' : '') +
            '><span class="track"></span><span class="knob"></span></label>';
        } else if (type === 'num') {
          ctrl = '<input class="num" type="number" min="0" data-key="' + key +
            '" value="' + esc(val) + '"' + (readOnly ? ' disabled' : '') + '>';
        } else {
          ctrl = '<input class="txt" type="text" data-key="' + key +
            '" value="' + esc(val) + '" placeholder="(station default)"' +
            (readOnly ? ' disabled' : '') + '>';
        }
        row.innerHTML = lab + ctrl;
        box.appendChild(row);
      });
      root.appendChild(box);
    });
    // wire change handlers
    root.querySelectorAll('input[data-key]').forEach((inp) => {
      const ev = inp.type === 'text' ? 'change' : 'change';
      inp.addEventListener(ev, () => {
        const key = inp.dataset.key;
        let v;
        if (inp.type === 'checkbox') v = inp.checked;
        else if (inp.type === 'number') v = parseInt(inp.value || '0', 10) || 0;
        else v = inp.value;
        save({ [key]: v });
      });
    });
  }

  function save(patch) {
    if (readOnly) return;
    fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => {
      if (r.status === 403) { readOnly = true; showRO(); load(); return null; }
      return r.json();
    }).then((d) => {
      if (!d) return;
      const s = $('saved');
      s.classList.remove('hidden');
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => s.classList.add('hidden'), 1800);
    }).catch(() => {});
  }

  function showRO() { $('ro').classList.remove('hidden'); }

  function load() {
    // role check → read-only when served from the cloud copy
    fetch('/api/health', { cache: 'no-store' })
      .then((r) => r.json()).then((h) => { if (h && h.role === 'cloud') { readOnly = true; showRO(); } })
      .catch(() => {})
      .finally(() => {
        fetch('/api/settings', { cache: 'no-store' })
          .then((r) => r.json())
          .then((settings) => build(settings || {}))
          .catch(() => { $('groups').innerHTML =
            '<div class="banner ro">Could not load settings — is the station agent running?</div>'; });
      });
  }

  load();
})();
