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
 ┌──────────────────────┐   WSJT-X UDP :2237    ┌────────────────────────┐
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
- **UDP Server port:** `2237`

This PC already binds `0.0.0.0:2237`, so once WSJT-X points here, decodes + frequency +
logged QSOs stream across. (JTDX is identical. If WSJT-X uses a *multicast* address instead,
put it in `wsjtx_multicast_group` in the config.)

### 2. Logbook — pick one
The full QSO history lives on the **station PC**, so there are two ways to feed the log panel:

- **(A) Full history** — share the station PC's WSJT-X folder, then set the UNC path in
  `station.config.json`:
  ```json
  "adif_log_path": "\\\\STATION-PC\\Users\\NAME\\AppData\\Local\\WSJT-X\\wsjtx_log.adi"
  ```
  Counters, Best-DX and the DX wall then reflect your real totals, updating within seconds
  of each new QSO.

- **(B) Live session only (zero setup)** — leave `adif_log_path` blank and
  `adif_autodetect: false` (the defaults). The dashboard builds the log **straight from the
  WSJT-X UDP "QSO Logged" packets** — every contact you log appears instantly. Totals cover
  the current session (no back-history).

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

## Notes / roadmap
- **Countries** is an *approximate* DXCC by callsign prefix (display only, not award-grade).
- The S-meter is derived from FT8 **decode SNR**, not a calibrated reading.
- Still to add (F4IOW parity): Story timeline, Contest calendar, QO-100 panel, My-Station gear
  cards, SOTA, PSKReporter embed, HamSphere.
- `/api/state` is polled ~1 Hz; can be upgraded to Server-Sent Events for instant push later.

73 de W4GGJ
