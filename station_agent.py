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

    # Home-only data: WSJT-X UDP + ADIF. No public pollers (the cloud runs those).
    engine.start_engine(enable_wsjtx=True, enable_adif=True, enable_pollers=False)
    print(f"[agent] pushing telemetry to {url} every {PUSH_INTERVAL:.0f}s")

    fails = 0
    while True:
        time.sleep(PUSH_INTERVAL)
        try:
            status = push(url, token, engine.home_telemetry())
            if status == 200:
                if fails:
                    print("[agent] link restored")
                fails = 0
            else:
                print(f"[agent] ingest returned HTTP {status}")
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
