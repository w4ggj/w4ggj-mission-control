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

Standard library only.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import station_engine as engine

HERE = Path(__file__).resolve().parent
PUSH_INTERVAL = 2.0     # seconds between pushes


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

    # Home-only data: WSJT-X UDP (live radio/decodes) + log. No public pollers
    # (the cloud runs those). Log comes from QRZ when a key is set, else the
    # local WSJT-X ADIF.
    engine.start_engine(enable_wsjtx=True, enable_adif=not qrz_key,
                        enable_pollers=False, enable_qrz=bool(qrz_key))
    print(f"[agent] pushing telemetry to {url} every {PUSH_INTERVAL:.0f}s")
    print("[agent] logbook source: " + ("QRZ Logbook API (read-only)" if qrz_key
                                        else "WSJT-X ADIF / live UDP"))

    fails = 0
    last_beat = 0.0
    ever_online = False
    while True:
        time.sleep(PUSH_INTERVAL)
        tel = engine.home_telemetry()
        radio_online = bool(tel.get("radio", {}).get("online"))
        ever_online = ever_online or radio_online
        try:
            status = push(url, token, tel)
            if status == 200:
                if fails:
                    print("[agent] link restored")
                fails = 0
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
