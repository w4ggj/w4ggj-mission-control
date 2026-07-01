# W4GGJ · Mission Control

A self-hosted, **live** amateur-radio station dashboard — the "one Python engine, live
everything" concept modeled on the F4IOW station page, rebuilt for **W4GGJ's home station
(Yaesu FT-450)**.

As you tune the FT-450, the frequency on the page moves. Every FT8 decode scrolls in. Every
logged QSO appears the instant it's saved. A viewer anywhere (LAN or Tailscale) sees your
bench in real time.

No third-party Python packages — **standard library only**, matching the TavaOne stack.

## The setup this is built for

```
 ┌──────────────────────┐   WSJT-X UDP :2242    ┌────────────────────────┐
 │  STATION PC          │  ───────────────────► │  DASHBOARD PC (this)   │
 │  Yaesu FT-450        │   (decodes, freq,     │  station_engine.py     │
 │  WSJT-X / JTDX       │    QSO-logged)        │  server.py  :8770      │
 └──────────────────────┘                       └────────────────────────┘
```

WSJT-X runs on the **station PC** and broadcasts UDP to **this** PC, which serves the dashboard.

---

## Quick start (on the dashboard PC)

```bat
cd "C:\TavaOne\Station Commnd Center"
python server.py
```

Then open **http://localhost:8770/** (or `http://100.100.41.109:8770/` over Tailscale).
Double-clicking `run_dashboard.bat` does the same.

> Even with the radio off you'll see propagation, POTA, ISS and DX populate from the internet.
> The radio/decode/log panels light up as soon as WSJT-X on the station PC is running.

---

## Make it live (3 things to confirm)

### 1. Point the station PC's WSJT-X at this dashboard PC
On the **station PC**, in **WSJT-X → File → Settings → Reporting**:

- ☑ **Accept UDP requests**
- **UDP Server:** the **dashboard PC's IP** (this machine — e.g. `192.168.x.x` or its Tailscale IP)
- **UDP Server port:** `2242`

This PC already binds `0.0.0.0:2242`, so once WSJT-X points here, decodes + frequency +
logged QSOs stream across. (JTDX is identical. If WSJT-X uses a *multicast* address instead,
put it in `wsjtx_multicast_group` in the config.)

### 2. Logbook — pick one
The log panel (totals, DXCC, Best-DX, recent contacts) can be fed three ways:

- **(A) QRZ Logbook API — recommended (full history + all new contacts).** If QRZ is your
  master logbook, the agent pulls your **entire** QRZ log on a timer — every band and mode,
  the complete back-history plus each new QSO your apps upload — no ADIF files, no
  re-exporting. It is **read-only**: the dashboard only ever *fetches*, never writes.
  Put your key (QRZ → your logbook → Settings → API) in `agent.config.json` (gitignored, so
  it stays on your PC):
  ```json
  "qrz_api_key": "XXXX-XXXX-XXXX-XXXX"
  ```
  (or set the `QRZ_API_KEY` env var). Refresh interval is `qrz_sync_sec` in
  `station.config.json` (default 300 s). When a key is set, QRZ overrides the ADIF options
  below. Live WSJT-X radio/decodes still stream in real time; new digital QSOs land in the
  log at the next QRZ refresh.

- **(B) Local ADIF file** — share the station PC's WSJT-X folder and set the UNC path in
  `station.config.json`:
  ```json
  "adif_log_path": "\\\\STATION-PC\\Users\\NAME\\AppData\\Local\\WSJT-X\\wsjtx_log.adi"
  ```
  Reflects that file's contents (WSJT-X = your digital contacts only), updating within
  seconds of each new QSO.

- **(C) Live session only (zero setup)** — leave `adif_log_path` blank and
  `adif_autodetect: false`. The dashboard builds the log **straight from the WSJT-X UDP
  "QSO Logged" packets** — every contact you log appears instantly, current session only.

### 3. Your grid
`station.config.json` ships with `"grid": "EL87"` (Tampa Bay). Fix it if your 6-char grid
differs — it drives ISS range and Best-DX distance.

---

## Optional: SSB / CW frequency via Hamlib

For non-FT8 operating (WSJT-X isn't broadcasting then), frequency can come from **Hamlib
`rigctld`** running on the **station PC**:

```
rigctld -m 1023 -r COM? -s 4800        # 1023 = Yaesu FT-450 in Hamlib
```
```json
"rigctld_enabled": true,
"rigctld_host": "192.168.x.x",         // the STATION PC's IP
"rigctld_port": 4532
```
(Confirm your FT-450's COM port and baud; `rigctl --list | findstr FT-450`.)

---

## Files

| File | Role |
|---|---|
| `station_engine.py` | Live engine — WSJT-X UDP (freq/decodes/**QSO-logged**), ADIF watcher, rigctld, solar/POTA/ISS/DX pollers. Thread-safe `snapshot()`. |
| `server.py` | Stdlib `http.server` serving the UI + `GET /api/state` (JSON). HTTPS if TavaOne certs exist. |
| `station.config.json` | Station identity + all data-source settings. |
| `web/` | `index.html`, `style.css`, `app.js` — mission-control UI, polls `/api/state` every second. |

**Test the engine alone** (prints a live state line every few seconds):
```bat
python station_engine.py
```

---

## Going public (Render, viewable from anywhere)

The dashboard is a **live app**, not a static site — the radio data comes off your home LAN.
So the public version uses a **relay**: your home PC pushes telemetry up to a cloud app that
the whole world can view. Viewers never touch your home network; your home IP stays private.

```
 STATION PC ──WSJT-X UDP──► HOME AGENT ──HTTPS POST──► RENDER (public) ──► anyone
 (FT-450)                   station_agent.py           server.py (ROLE=cloud)
```

### 1. Deploy the cloud app on Render
- Push this folder to a **public GitHub repo** (no secrets are in it).
- Render → **New + → Blueprint** → pick the repo. It reads `render.yaml` and creates a free
  web service running `server.py` with `ROLE=cloud`.
- When prompted, set **`INGEST_TOKEN`** to a long random string (this is the shared secret).
- You get a public URL like `https://w4ggj-mission-control.onrender.com`.

At this point the public data (propagation / POTA / ISS / DX) is already live. The radio,
decode and log panels show "offline" until the home agent connects.

### 2. Run the home agent (on your LAN dashboard PC)
```bat
copy agent.config.example.json agent.config.json
```
Edit `agent.config.json`:
```json
"cloud_url":    "https://w4ggj-mission-control.onrender.com",
"ingest_token": "<the SAME string you set as INGEST_TOKEN on Render>"
```
Then:
```bat
python station_agent.py
```
It listens to WSJT-X locally and pushes radio/decodes/log to Render every 2 s — which also
keeps the free Render instance from sleeping. Leave it running (Task Scheduler can auto-start it).

> `agent.config.json` holds your token and is **gitignored** — it never leaves your PC.
> Local LAN viewing (`python server.py`) still works exactly as before, independently.

### Agent running but the public site isn't updating?
Work top-down — the agent console and the cloud health probe tell you which link is broken:

1. **Is telemetry reaching Render?** Open `https://<your-app>.onrender.com/api/health`.
   The `ingest` block reports `age_sec` (seconds since the last push) and `radio_online`.
   - `age_sec: null` or large → the agent's pushes aren't arriving. Check the agent console:
     `push REJECTED (HTTP 401)` means `ingest_token` ≠ Render's `INGEST_TOKEN`; other
     `push failed` lines mean a network/URL problem.
   - `age_sec` small but the panels look empty → data *is* flowing; the next check applies.
2. **Is WSJT-X reaching the agent?** The agent prints a heartbeat every ~30 s. If it says
   `radio=off` and warns `no WSJT-X data received yet`, WSJT-X isn't delivering UDP: in
   **Settings > Reporting** enable *Accept UDP requests* and set **UDP Server `127.0.0.1`,
   port `2242`** (the agent must run on the same PC as WSJT-X).

## Shack wall display (`/shack.html`)
A second, purpose-built view for a dedicated monitor over the station: **everything on
one fixed screen, no scrolling**, glanceable from across the room. Same live `/api/state`
feed as the public dashboard — big frequency readout (glows red on transmit), the decode
waterfall, S-meter, propagation, logbook + recent contacts, POTA, DX cluster and the ISS.

- Open it at `/shack.html` (locally `http://localhost:8770/shack.html`, or your public URL).
- Tuned for a 2560×1440 landscape monitor; the layout is fluid, so 1080p/4K fit too, and it
  reflows to a stacked layout on portrait screens.
- It's an operator view, unlisted (not linked from the public page). It shows the same data
  the public site already serves — for true access control, put it behind your own reverse
  proxy / basic-auth.
- Kiosk tip: open it fullscreen (`F11`) in the browser and enable auto-start on the shack PC.

## Notes / roadmap
- **Countries** is an *approximate* DXCC by callsign prefix (display only, not award-grade).
- The S-meter is derived from FT8 **decode SNR**, not a calibrated reading.
- Still to add (F4IOW parity): Story timeline, Contest calendar, QO-100 panel, My-Station gear
  cards, SOTA, PSKReporter embed, HamSphere.
- `/api/state` is polled ~1 Hz; can be upgraded to Server-Sent Events for instant push later.

73 de W4GGJ
