"""
W4GGJ Mission Control — Home Agent
==================================
Runs on your LAN dashboard PC (the one that hears the station PC's WSJT-X UDP).
It reads the live radio / decodes / signal / logbook locally, then pushes that
telemetry up to the public Render app every couple of seconds.

Viewers hit Render, never your home network. Your home IP stays private.

Setup:
  1. Copy agent.config.example.json -> agent.config.json and fill in:
       "cloud_url":    "https://w4ggj-mission-control.onrender.com"
       "ingest_token": "<same secret you set as INGEST_TOKEN on Render>"
     (or set env vars CLOUD_URL and INGEST_TOKEN instead)
  2. Make sure WSJT-X on the station PC points its UDP Server at THIS PC:2242.
  3. Run:  python station_agent.py

Optional — UDP cloner + web logger bridge (folded in):
  If you also run the standalone udp_cloner_with_adif.py, set "cloner_enabled":
  true in station.config.json instead. WSJT-X can only send its UDP to ONE
  place, so point WSJT-X at the cloner's port (2235) and the cloner relays a
  copy to the dashboard engine (2242) plus your other apps. One process, one
  WSJT-X port. See station_cloner.py.

Standard library only.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import station_cloner
import station_engine as engine

HERE = Path(__file__).resolve().parent
PUSH_INTERVAL = 2.0     # seconds between pushes
MAP_RESEND_SEC = 60     # resend the (rarely-changing) world-map section at least this often


def load_agent_cfg():
    cfg = {}
    p = HERE / "agent.config.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[agent] could not read agent.config.json: {e}")
    # env overrides
    cfg["cloud_url"] = os.environ.get("CLOUD_URL", cfg.get("cloud_url", "")).rstrip("/")
    cfg["ingest_token"] = os.environ.get("INGEST_TOKEN", cfg.get("ingest_token", ""))
    cfg["qrz_api_key"] = os.environ.get("QRZ_API_KEY", cfg.get("qrz_api_key", "")).strip()
    return cfg


def push(url, token, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + "/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": token},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main():
    cfg = load_agent_cfg()
    url, token = cfg.get("cloud_url", ""), cfg.get("ingest_token", "")
    if not url or not token:
        print("[agent] ERROR: set cloud_url + ingest_token in agent.config.json "
              "(or CLOUD_URL / INGEST_TOKEN env vars). Exiting.")
        return

    # QRZ logbook API key (read-only pulls). When present, QRZ is the log source
    # of truth — full history + all new contacts, any mode — so the local WSJT-X
    # ADIF watcher is turned off to avoid it fighting QRZ for the log panel.
    qrz_key = cfg.get("qrz_api_key", "")
    if qrz_key:
        os.environ["QRZ_API_KEY"] = qrz_key

    # Home-only data: WSJT-X UDP (live radio/decodes) + log. Log comes from QRZ
    # when a key is set, else the local WSJT-X ADIF. The public pollers (solar /
    # POTA / ISS / DX cluster) normally run only on the cloud, but when we also
    # serve the dashboard locally those panels would be empty on the LAN view —
    # so run them here too when local_web_enabled. (Their data isn't in the
    # ingest push, so this doesn't affect the cloud.)
    serve_local = engine.cfg("local_web_enabled", True)
    engine.start_engine(enable_wsjtx=True, enable_adif=not qrz_key,
                        enable_pollers=serve_local, enable_qrz=bool(qrz_key))

    # Optional: fold in the UDP cloner + web logger bridge so a single WSJT-X
    # UDP port feeds both the fan-out apps and this dashboard. The cloner clones
    # a verbatim copy of every packet to the engine's WSJT-X port, so the engine
    # keeps binding it as usual — no engine change. See station_cloner.py.
    if engine.cfg("cloner_enabled", False):
        engine_port = int(engine.cfg("wsjtx_udp_port", 2242))
        station_cloner.start({
            "cloner_local_port":  engine.cfg("cloner_local_port", 2235),
            "cloner_remote_port": engine.cfg("cloner_remote_port", 2234),
            "cloner_bridge_port": engine.cfg("cloner_bridge_port", 12061),
            "cloner_targets":     engine.cfg("cloner_targets", []),
            "cloner_adif_path":   engine.cfg("cloner_adif_path", ""),
            "cloner_blog_folder": engine.cfg("cloner_blog_folder", ""),
            "cloner_scan_sec":    engine.cfg("cloner_scan_sec", 30),
            "engine_udp_port":    engine_port,
            "callsign":           engine.cfg("callsign", "W4GGJ"),
            "grid":               engine.cfg("grid", "EL87"),
        })
        print(f"[agent] cloner ON — point WSJT-X at UDP "
              f"{engine.cfg('cloner_local_port', 2235)} (it relays to the "
              f"dashboard on {engine_port} + your other apps)")

    # Local dashboard: serve the same UI + /api/state + /api/spectrum straight
    # off THIS process (which already owns the engine), so the shack can watch
    # the dashboard on the LAN without depending on the cloud — and the SDR
    # agent's full-res spectrum finally has a local server to land on. On by
    # default; set "local_web_enabled": false in station.config.json to disable.
    if serve_local:
        try:
            import server
            from http.server import ThreadingHTTPServer
            server.ROLE = "local"          # POST /api/spectrum needs no token on the LAN
            web_host = engine.cfg("bind_host", "0.0.0.0")
            web_port = int(engine.cfg("web_port", 8770))
            httpd = ThreadingHTTPServer((web_host, web_port), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            print(f"[agent] local dashboard: http://localhost:{web_port}/  ·  "
                  f"shack: http://localhost:{web_port}/shack.html  ·  "
                  f"console: http://localhost:{web_port}/console")
            print(f"[agent] on the LAN use this PC's IP, e.g. http://<this-pc-ip>:{web_port}/shack.html")
        except Exception as e:
            print(f"[agent] local web server not started ({e})")

    print(f"[agent] pushing telemetry to {url} every {PUSH_INTERVAL:.0f}s")
    print("[agent] logbook source: " + ("QRZ Logbook API (read-only)" if qrz_key
                                        else "WSJT-X ADIF / live UDP"))

    fails = 0
    last_beat = 0.0
    ever_online = False
    last_map_updated = None
    last_map_sent = 0.0
    while True:
        time.sleep(PUSH_INTERVAL)
        tel = engine.home_telemetry()
        radio_online = bool(tel.get("radio", {}).get("online"))
        ever_online = ever_online or radio_online
        # The world-map section (all-time reach, thousands of points) only
        # changes when the logbook does — once a minute at most. Push it when
        # its timestamp moved, or at least every MAP_RESEND_SEC so the cloud
        # rebuilds the map quickly after a restart; the cloud keeps the last
        # copy otherwise, so the 2s pushes stay tiny instead of re-sending the
        # whole reach list every time.
        mp = tel.get("map")
        now = time.time()
        map_updated = mp.get("updated") if isinstance(mp, dict) else None
        sending_map = isinstance(mp, dict) and (
            map_updated != last_map_updated or now - last_map_sent >= MAP_RESEND_SEC)
        if isinstance(mp, dict) and not sending_map:
            tel.pop("map", None)
        try:
            status = push(url, token, tel)
            if status == 200:
                if fails:
                    print("[agent] link restored")
                fails = 0
                if sending_map:
                    last_map_updated = map_updated   # only mark sent once it lands
                    last_map_sent = now
                # periodic heartbeat so "pushing OK but no data" is visible
                now = time.time()
                if now - last_beat >= 30:
                    last_beat = now
                    r = tel.get("radio", {})
                    print(f"[agent] push OK · radio={'ON' if radio_online else 'off'} "
                          f"{r.get('freq_mhz', 0)}MHz {r.get('mode', '—')} · "
                          f"decodes={len(tel.get('decodes', []))} · "
                          f"log={tel.get('log', {}).get('total', 0)} QSOs")
                    if not ever_online:
                        # With the cloner on, WSJT-X feeds the cloner's local
                        # port (which relays to the engine); without it, WSJT-X
                        # feeds the engine directly. Point the note at whichever
                        # port WSJT-X should actually target.
                        if engine.cfg("cloner_enabled", False):
                            port = engine.cfg("cloner_local_port", 2235)
                        else:
                            port = engine.cfg("wsjtx_udp_port", 2242)
                        print("[agent] NOTE: no WSJT-X data received yet — in WSJT-X "
                              "Settings>Reporting enable 'Accept UDP requests' and set "
                              f"UDP Server 127.0.0.1 port {port}.")
            else:
                print(f"[agent] ingest returned HTTP {status}")
        except urllib.error.HTTPError as e:
            fails += 1
            if e.code == 401:
                print("[agent] push REJECTED (HTTP 401) — ingest_token in "
                      "agent.config.json does not match INGEST_TOKEN on Render.")
            elif fails <= 3 or fails % 30 == 0:
                print(f"[agent] push failed (HTTP {e.code})")
            if fails > 3:
                time.sleep(min(20, fails))
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 30 == 0:
                print(f"[agent] push failed ({e})")
            # simple backoff on sustained failure
            if fails > 3:
                time.sleep(min(20, fails))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[agent] stopped")
