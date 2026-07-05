"""
W4GGJ Mission Control — Station Engine
=====================================
The "one Python engine" that reads the live station and exposes a single
thread-safe STATE snapshot for the web dashboard.

Live sources (all optional, all fail-soft):
  * WSJT-X / JTDX UDP (port 2242)  -> live dial freq, mode, TX/RX, decodes
  * wsjtx_log.adi (file watcher)    -> logged QSOs + cumulative stats
  * Hamlib rigctld (optional)       -> freq/mode for SSB/CW when not on FT8
  * HamQSL solar XML                -> SFI, A, K, sunspots, solar wind, bands
  * POTA API                        -> live Parks-On-The-Air spots
  * wheretheiss.at                  -> live ISS position + range from your grid
  * DX Summit                       -> recent DX cluster spots

No third-party packages required — Python standard library only.
Author: built for W4GGJ / Joe
"""

import hashlib
import html
import json
import math
import os
import queue
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Browser-like User-Agent. Some public feeds (notably the Cloudflare-fronted
# dxsummit.fi DX-cluster API) 403 non-browser agents like "Python-urllib" or a
# bare product token, which left the DX panel permanently empty on the cloud.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"}


# ── TLS ───────────────────────────────────────────────────────────────────────
def _build_ssl_context():
    """A verifying TLS context that also works on Windows. Python on Windows
    frequently ships without a usable CA bundle, so plain urllib can't verify
    public HTTPS feeds (hamqsl solar, POTA, wheretheiss) → 'CERTIFICATE_VERIFY_
    FAILED: unable to get local issuer certificate'. Prefer certifi's Mozilla
    bundle when it's installed ('pip install certifi'); otherwise fall back to the
    system default. Verification is never disabled — the QRZ call carries your API
    key, so its TLS must stay trusted."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


_SSL_CTX = _build_ssl_context()


# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    # station.config.json is your LIVE config and is gitignored (so pulls never
    # fight your local edits). A fresh clone only ships station.config.example.json
    # as a template — fall back to it so the app still runs before you copy it.
    for name in ("station.config.json", "station.config.example.json"):
        path = HERE / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if name.endswith(".example.json"):
                print("[engine] station.config.json not found — using "
                      "station.config.example.json defaults (copy it to "
                      "station.config.json and edit for your station)")
            return {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception as e:
            print(f"[engine] config load failed for {name} ({e})")
    print("[engine] no config file found — using built-in defaults")
    return {}


CONFIG = load_config()


def cfg(key, default=None):
    return CONFIG.get(key, default)


# ── Live dashboard settings (the shack Config page) ───────────────────────────
# Operator-tunable switches that shape the PUBLIC page. Authored on the home
# agent (via the shack's local Config page → POST /api/settings), persisted to
# settings.json, and relayed to the cloud with the telemetry push (settings is an
# INGEST section), so a toggle on the shack LAN reflects on the public site within
# a second. Everything defaults ON (full display). The cloud is read-only for
# these — the agent is authoritative and overwrites them on each ingest.
_SETTINGS_FILE = HERE / "settings.json"
DEFAULT_SETTINGS = {
    # public-page section visibility
    "show_records": True, "show_radio": True, "show_decodes": True,
    "show_prop": True, "show_log": True, "show_pota": True, "show_space": True,
    "show_map": True, "show_awards": True, "show_activity": True, "show_psk": True,
    "show_contest": True, "show_audio": True, "show_scope": True,
    # privacy
    "show_freq": True,        # off → mask the exact frequency readout
    "show_calls": True,       # off → mask recent worked callsigns
    # features
    "enable_flash": True,     # new-contact flash celebration
    "portable_audio": False,  # keep Listen-Live visible in portable mode (SDR feed)
    # contest panel tuning (live)
    "contest_min_qsos": 5, "contest_gap_min": 60,
    # live text (blank → use the station identity from config)
    "tagline": "", "subtitle": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[engine] settings load failed ({e})")
    return s


SETTINGS = load_settings()


def get_settings():
    with _lock:
        return dict(STATE["settings"])


def update_settings(patch):
    """Validate + apply a settings patch, persist it, return what changed."""
    changed = {}
    with _lock:
        s = STATE["settings"]
        for k, v in (patch or {}).items():
            if k not in DEFAULT_SETTINGS:
                continue
            d = DEFAULT_SETTINGS[k]
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int):
                try:
                    v = max(0, int(v))
                except (TypeError, ValueError):
                    continue
            elif isinstance(d, str):
                v = str(v)[:120]
            s[k] = v
            changed[k] = v
    if changed:
        _save_settings()
    return changed


def _save_settings():
    with _lock:
        data = dict(STATE["settings"])
    try:
        tmp = _SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_SETTINGS_FILE)
    except Exception as e:
        print(f"[engine] settings save failed ({e})")


# ── Shared state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
STATE = {
    "identity": {
        "callsign": cfg("callsign", "W4GGJ"),
        "operator": cfg("operator", ""),
        "grid": cfg("grid", ""),
        "qth": cfg("qth", ""),
        "rig": cfg("rig", ""),
        "power_watts": cfg("power_watts", 5),
        "modes": cfg("modes", ""),
        "digital": cfg("digital", ""),
        "tagline": cfg("tagline", ""),
        "subtitle": cfg("subtitle", ""),
        "pskreporter_url": cfg("pskreporter_url", ""),
    },
    # live radio (WSJT-X / rigctld)
    "radio": {
        "online": False,
        "source": "—",          # "WSJT-X" | "rigctld" | "—"
        "dial_hz": 0,
        "rx_hz": 0,             # dial + rx audio offset
        "freq_mhz": 0.0,
        "band": "—",
        "mode": "—",            # the rig's actual mode (LSB/USB/CW/…) from CAT
        "digital_mode": "",     # WSJT-X submode (FT8/FT4/…) when it's running
        "tx": False,
        "decoding": False,
        "dx_call": "",
        "report": "",
        "last_seen": 0,
        # live rig telemetry from Hamlib rigctld (optional)
        "cat_online": False,
        "power_w": None,        # actual/set TX power in watts (None = not reported)
        "meters": {},           # {label: "value"} — SWR, ALC, S, Vd, … whatever the rig reports
    },
    "decodes": [],              # recent WSJT-X decodes (newest first)
    "signal": {"snr": None, "s_meter": "—", "s_pct": 0},
    # logbook
    "log": {
        "total": 0, "unique_calls": 0, "bands": 0, "modes": 0,
        "countries": 0, "grids": 0,
        "best_dx": None,        # {call, km, country, grid}
        "recent": [],           # newest first
        "last_qso_ts": 0,       # bumps when a new QSO is logged
        "adif_path": "",
        "mode_breakdown": {}, "band_breakdown": {},
    },
    # world map — station + all-time reach + recent contact arcs (from the log)
    "map": {
        "station": None,   # [lat, lon] of this station (from grid)
        "reach": [],       # [[lat, lon], …] unique worked grid-fields, all-time
        "recent": [],      # [{lat, lon, call, band, mode, date, time}, …] newest first
        "updated": 0,
    },
    # award progress (from the log): Worked All States, DXCC, grids
    "awards": {
        "was": {"worked": [], "count": 0},   # US states worked (of 50)
        "was_bands": {},                     # {band: worked-state count} for 5BWAS
        "dxcc": 0,                           # unique DXCC entities (≈ countries)
        "grids": 0,                          # unique Maidenhead fields
        "updated": 0,
    },
    # QSO activity (from the log): calendar heatmap + hour/year histograms
    "activity": {
        "daily": {},        # {"YYYYMMDD": count} for ~the last 53 weeks
        "hours": [0] * 24,  # all-time QSOs by UTC hour-of-day
        "by_year": {},      # {"YYYY": count} all-time
        "updated": 0,
    },
    # station records / milestones (from the log) — the top-of-page ribbon
    "records": {
        "first_qso": "",                                   # YYYYMMDD
        "years_on_air": 0,
        "most_in_day": {"count": 0, "date": ""},
        "longest_streak": {"days": 0, "start": "", "end": ""},
        "newest_dxcc": {"country": "", "date": ""},
        "busiest_band": {"band": "", "count": 0},
        "busiest_hour": {"hour": None, "count": 0},
        "updated": 0,
    },
    # live contest / run panel (from the log) — rate + session tally + per-band
    "contest": {
        "active": False,        # a QSO within the active window (operating now)
        "session": 0,           # QSOs in the current run (since a >gap break)
        "session_start": 0,     # epoch of the first QSO in the run
        "bands": {},            # band -> count for this run
        "modes": {},            # mode -> count for this run
        "rate_10": 0,           # projected Q/hr from the last 10 minutes
        "last_60": 0,           # QSOs in the trailing 60 minutes
        "best_hour": 0,         # best QSOs in any rolling 60 min of the run
        "last_ts": 0,           # epoch of the most recent QSO
        "updated": 0,
    },
    # space weather / propagation
    "solar": {
        "sfi": "—", "a_index": "—", "k_index": "—", "sunspots": "—",
        "solar_wind": "—", "xray": "—", "aurora": "—",
        "bands": [],            # [{band, day, night}]
        "updated": 0,
    },
    # field / space
    "pota": [],                 # live POTA spots
    "iss": {"lat": None, "lon": None, "alt_km": None, "vel_kmh": None, "range_km": None},
    "dx": [],                   # recent DX cluster spots
    # live dashboard settings (shack Config page → shapes the public page)
    "settings": SETTINGS,
    # portable / field op — flips on when live telemetry arrives from the field
    # unit (cloner's remote port) instead of the home rig. Shapes the header.
    "field": {"active": False, "label": "", "since": 0, "last_ts": 0},
    "server_time": 0,
    "engine_started": 0,
}


def _set(section, patch):
    with _lock:
        if section in STATE and isinstance(STATE[section], dict):
            STATE[section].update(patch)
        else:
            STATE[section] = patch


# ── Portable / field-op detection ─────────────────────────────────────────────
# The cloner calls mark_field_telemetry() for every packet that arrives on its
# remote/field port (2234), i.e. from the portable unit rather than the home rig.
# _refresh_field() then flips STATE["field"].active on while packets are recent
# and off after field_timeout_sec of silence — so the dashboard can switch the
# header to a "POTA · PORTABLE" label and hide the (home-only) audio stream.
def mark_field_telemetry():
    with _lock:
        STATE["field"]["last_ts"] = time.time()


def _refresh_field():
    """Recompute portable state from the last field packet. Caller holds _lock."""
    f = STATE["field"]
    now = time.time()
    timeout = float(cfg("field_timeout_sec", 180))
    active = bool(f["last_ts"]) and (now - f["last_ts"] <= timeout)
    if active and not f["active"]:
        f["since"] = now
    elif not active:
        f["since"] = 0
    f["active"] = active
    f["label"] = cfg("field_label", "POTA · PORTABLE") if active else ""


def snapshot():
    with _lock:
        _refresh_field()
        STATE["server_time"] = time.time()
        return json.loads(json.dumps(STATE, default=str))


# ── Live spectrum frame (SDR panadapter → /console) ────────────────────────────
# The SDR agent (sdr_agent.py) POSTs FFT frames here at ~10 Hz over the LAN. Kept
# OUT of STATE/snapshot on purpose: it's high-rate and local-only (the shack
# console polls /api/spectrum directly), so it never bloats the 1 Hz /api/state
# snapshot nor the cloud ingest.
_spectrum = None


def set_spectrum(frame):
    """Store the latest spectrum frame from the SDR agent (dict with center_hz,
    span_hz, dial_hz, bins[], ref_dbm, ts…)."""
    global _spectrum
    if isinstance(frame, dict):
        frame["recv_ts"] = time.time()
        _spectrum = frame


def get_spectrum():
    return _spectrum or {}


# ══════════════════════════════════════════════════════════════════════════════
#  Visitor analytics  (who's been on the PUBLIC page, and where from)
# ══════════════════════════════════════════════════════════════════════════════
# The public site is served by the cloud role, so visit events accumulate here.
# Kept OUT of STATE/snapshot: it's private operator data, exposed only via the
# token-gated /api/analytics endpoint (never the public /api/state). Each HTML
# page GET calls record_visit(); a single background worker geolocates new IPs
# through ip-api.com (free, no key) and caches the result so repeat visitors and
# the fast /api/state pollers never trigger a lookup. Counters are lifetime and
# persisted best-effort to analytics_data.json (survives restarts; the cloud
# host's disk is wiped on redeploy, which just resets the stats — no secrets).
_AN_FILE = HERE / "analytics_data.json"
_AN_MAX_EVENTS = 400          # recent-visit rows kept for the table
_AN_MAX_DAILY = 120           # days of the visits-per-day sparkline
_an_lock = threading.Lock()
_an_started = False
_geo_q = queue.Queue()
_analytics = {
    "totals": {"views": 0},
    "uniques": set(),          # distinct visitor hashes (lifetime)
    "by_country": {},          # "US" -> count
    "cc_names": {},            # "US" -> "United States" (for display)
    "by_city": {},             # "City, Region, CC" -> {count, lat, lon, cc, city, region}
    "by_page": {},             # "/" -> count
    "by_ref": {},              # "google.com" -> count
    "daily": {},               # "YYYY-MM-DD" (UTC) -> count
    "events": [],              # recent visits (newest first)
    "geo": {},                 # ip -> {country, cc, region, city, lat, lon, isp}
    "first_ts": 0,
}

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|bing|yandex|duckduck|baidu|semrush|ahrefs|"
    r"facebookexternalhit|python-|curl|wget|headless|monitor|uptime|pingdom",
    re.I)
_PAGE_LABELS = {
    "/": "Home", "": "Home", "/index.html": "Home",
    "/shack.html": "Shack", "/console": "Console", "/console.html": "Console",
    "/analytics": "Analytics", "/analytics.html": "Analytics",
}


def _is_public_ip(ip):
    """True for a routable IP we can geolocate (skip LAN / loopback / unknown)."""
    if not ip or ":" in ip and ip.count(":") < 2:   # malformed
        return False
    if ip.startswith(("10.", "127.", "192.168.", "169.254.", "::1", "fc", "fd", "fe80")):
        return False
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:
                return False
        except (ValueError, IndexError):
            return False
    return "." in ip or ":" in ip


def _mask_ip(ip):
    """Redact the host part for display (203.0.113.0 / v6 /48)."""
    if not ip:
        return "—"
    if "." in ip:
        p = ip.split(".")
        return ".".join(p[:3] + ["0"]) if len(p) == 4 else ip
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::"
    return ip


def _ua_summary(ua):
    """Coarse browser · OS label from a User-Agent string (no libraries)."""
    if not ua:
        return "Unknown"
    u = ua.lower()
    if _BOT_RE.search(ua):
        m = re.search(r"([a-z0-9]+bot|[a-z]+preview|facebookexternalhit|"
                      r"semrush|ahrefs|uptimerobot|pingdom)", u)
        return "Bot · " + (m.group(1) if m else "crawler")
    if "edg/" in u:
        br = "Edge"
    elif "opr/" in u or "opera" in u:
        br = "Opera"
    elif "chrome/" in u and "chromium" not in u:
        br = "Chrome"
    elif "firefox/" in u:
        br = "Firefox"
    elif "safari/" in u:
        br = "Safari"
    else:
        br = "Browser"
    if "android" in u:
        osn = "Android"
    elif "iphone" in u or "ipad" in u or "ios" in u:
        osn = "iOS"
    elif "windows" in u:
        osn = "Windows"
    elif "mac os" in u or "macintosh" in u:
        osn = "macOS"
    elif "linux" in u:
        osn = "Linux"
    else:
        osn = ""
    return br + (" · " + osn if osn else "")


def _ref_domain(referer, own_host):
    """Bare hostname of an external referrer ('' for direct / same-site)."""
    if not referer:
        return ""
    try:
        host = urllib.parse.urlparse(referer).hostname or ""
    except Exception:
        return ""
    host = host.lower().lstrip("www.")
    if not host or (own_host and own_host.lower().endswith(host)):
        return ""
    return host


def _ensure_analytics():
    global _an_started
    with _an_lock:
        if _an_started:
            return
        _an_started = True
        _analytics_load()
        threading.Thread(target=_geo_worker, daemon=True).start()
        threading.Thread(target=_analytics_saver, daemon=True).start()


def record_visit(ip, path, referer, user_agent, host=""):
    """Log one page view. Fast + fail-soft — called inline from the web handler."""
    try:
        _ensure_analytics()
        now = time.time()
        page = _PAGE_LABELS.get(path.split("?")[0], path.split("?")[0][:40] or "Home")
        is_bot = bool(_BOT_RE.search(user_agent or ""))
        vhash = hashlib.sha256((ip or "").encode("utf-8", "replace")).hexdigest()[:16]
        ref = _ref_domain(referer, host)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _an_lock:
            geo = _analytics["geo"].get(ip)
            ev = {
                "ts": now, "page": page, "ip": _mask_ip(ip), "vhash": vhash,
                "ua": _ua_summary(user_agent), "bot": is_bot, "ref": ref,
                "country": (geo or {}).get("country", ""),
                "cc": (geo or {}).get("cc", ""),
                "city": (geo or {}).get("city", ""),
                "region": (geo or {}).get("region", ""),
            }
            _analytics["events"].insert(0, ev)
            del _analytics["events"][_AN_MAX_EVENTS:]
            if not _analytics["first_ts"]:
                _analytics["first_ts"] = now
            # Bots are logged (visible in the table) but excluded from the tallies
            # so the human numbers stay honest.
            if not is_bot:
                _analytics["totals"]["views"] += 1
                _analytics["uniques"].add(vhash)
                _analytics["by_page"][page] = _analytics["by_page"].get(page, 0) + 1
                _analytics["daily"][day] = _analytics["daily"].get(day, 0) + 1
                if len(_analytics["daily"]) > _AN_MAX_DAILY:
                    for k in sorted(_analytics["daily"])[:-_AN_MAX_DAILY]:
                        _analytics["daily"].pop(k, None)
                if ref:
                    _analytics["by_ref"][ref] = _analytics["by_ref"].get(ref, 0) + 1
                if geo:
                    _tally_geo(geo)
            need_geo = geo is None and _is_public_ip(ip)
        if need_geo:
            _geo_q.put(ip)
    except Exception:
        pass   # analytics must never break a page load


def _tally_geo(geo, n=1):
    """Add n located visits to the country/city rollups (holds _an_lock)."""
    cc = geo.get("cc") or "??"
    _analytics["by_country"][cc] = _analytics["by_country"].get(cc, 0) + n
    if geo.get("country"):
        _analytics["cc_names"][cc] = geo["country"]
    if geo.get("lat") is not None:
        key = ", ".join(x for x in (geo.get("city"), geo.get("region"), cc) if x)
        c = _analytics["by_city"].setdefault(
            key, {"count": 0, "lat": geo.get("lat"), "lon": geo.get("lon"),
                  "cc": cc, "city": geo.get("city", ""), "region": geo.get("region", "")})
        c["count"] += n


def _geo_worker():
    """Serialize IP → location lookups (ip-api.com free tier: 45 req/min)."""
    while True:
        ip = _geo_q.get()
        try:
            with _an_lock:
                if ip in _analytics["geo"]:
                    continue
            url = ("http://ip-api.com/json/" + urllib.parse.quote(ip) +
                   "?fields=status,country,countryCode,regionName,city,lat,lon,isp")
            raw = _get(url, timeout=8)
            d = json.loads(raw.decode("utf-8", "replace"))
            if d.get("status") != "success":
                geo = {"country": "", "cc": "", "region": "", "city": "",
                       "lat": None, "lon": None, "isp": ""}
            else:
                geo = {"country": d.get("country", ""), "cc": d.get("countryCode", ""),
                       "region": d.get("regionName", ""), "city": d.get("city", ""),
                       "lat": d.get("lat"), "lon": d.get("lon"), "isp": d.get("isp", "")}
            with _an_lock:
                _analytics["geo"][ip] = geo
                # Back-fill the pending events for this IP + roll it into the
                # located tallies once (the earlier record_visit couldn't).
                vh = hashlib.sha256(ip.encode("utf-8", "replace")).hexdigest()[:16]
                filled = 0
                for ev in _analytics["events"]:
                    if ev["vhash"] == vh and not ev["cc"] and not ev["country"]:
                        ev.update({"country": geo["country"], "cc": geo["cc"],
                                   "city": geo["city"], "region": geo["region"]})
                        if not ev["bot"]:
                            filled += 1
                if filled and geo.get("cc"):
                    _tally_geo(geo, filled)
        except Exception:
            with _an_lock:
                _analytics["geo"].setdefault(ip, {"country": "", "cc": "", "region": "",
                                                  "city": "", "lat": None, "lon": None})
        finally:
            time.sleep(1.4)   # stay well under the 45/min public-tier limit


def analytics_summary():
    """Aggregated dashboard payload for the token-gated /api/analytics."""
    with _an_lock:
        now = time.time()
        cutoff = now - 86400
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        recent = [dict(e) for e in _analytics["events"]]
        views_24h = sum(1 for e in _analytics["events"] if not e["bot"] and e["ts"] >= cutoff)
        countries = sorted(_analytics["by_country"].items(), key=lambda kv: -kv[1])
        cc_names = _analytics["cc_names"]
        cities = sorted(_analytics["by_city"].values(), key=lambda c: -c["count"])
        pages = sorted(_analytics["by_page"].items(), key=lambda kv: -kv[1])
        refs = sorted(_analytics["by_ref"].items(), key=lambda kv: -kv[1])
        points = [{"lat": c["lat"], "lon": c["lon"], "count": c["count"],
                   "label": ", ".join(x for x in (c["city"], c["region"], c["cc"]) if x)}
                  for c in cities if c.get("lat") is not None]
        daily = dict(sorted(_analytics["daily"].items()))
        return {
            "totals": {
                "views": _analytics["totals"]["views"],
                "uniques": len(_analytics["uniques"]),
                "today": _analytics["daily"].get(today, 0),
                "views_24h": views_24h,
                "countries": len(_analytics["by_country"]),
            },
            "since": _analytics["first_ts"] or None,
            "server_time": now,
            "countries": [{"cc": cc, "count": n, "name": cc_names.get(cc, "")}
                          for cc, n in countries[:60]],
            "cities": cities[:40],
            "pages": [{"page": p, "count": n} for p, n in pages],
            "referrers": [{"host": h, "count": n} for h, n in refs[:20]],
            "points": points,
            "daily": daily,
            "recent": recent[:120],
        }


def _analytics_load():
    try:
        d = json.loads(_AN_FILE.read_text(encoding="utf-8"))
        _analytics["totals"] = d.get("totals", {"views": 0})
        _analytics["uniques"] = set(d.get("uniques", []))
        _analytics["by_country"] = d.get("by_country", {})
        _analytics["cc_names"] = d.get("cc_names", {})
        _analytics["by_city"] = d.get("by_city", {})
        _analytics["by_page"] = d.get("by_page", {})
        _analytics["by_ref"] = d.get("by_ref", {})
        _analytics["daily"] = d.get("daily", {})
        _analytics["events"] = d.get("events", [])[:_AN_MAX_EVENTS]
        _analytics["geo"] = d.get("geo", {})
        _analytics["first_ts"] = d.get("first_ts", 0)
        print(f"[engine] analytics restored ({_analytics['totals'].get('views', 0)} views)")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[engine] analytics load failed ({e})")


def _analytics_save():
    with _an_lock:
        d = {
            "totals": _analytics["totals"],
            "uniques": list(_analytics["uniques"]),
            "by_country": _analytics["by_country"],
            "cc_names": _analytics["cc_names"],
            "by_city": _analytics["by_city"],
            "by_page": _analytics["by_page"],
            "by_ref": _analytics["by_ref"],
            "daily": _analytics["daily"],
            "events": _analytics["events"][:_AN_MAX_EVENTS],
            "geo": _analytics["geo"],
            "first_ts": _analytics["first_ts"],
        }
    try:
        tmp = _AN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(_AN_FILE)
    except Exception:
        pass


def _analytics_saver():
    while True:
        time.sleep(30)
        _analytics_save()


# ── Helpers ───────────────────────────────────────────────────────────────────
BANDS = [
    (0.1357, 0.1378, "2200m"), (0.472, 0.479, "630m"), (1.8, 2.0, "160m"),
    (3.5, 4.0, "80m"), (5.3, 5.4, "60m"), (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"), (14.0, 14.35, "20m"), (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"), (24.89, 24.99, "12m"), (28.0, 29.7, "10m"),
    (50.0, 54.0, "6m"), (144.0, 148.0, "2m"), (222.0, 225.0, "1.25m"),
    (420.0, 450.0, "70cm"),
]


# The 50 US states (for Worked All States). DC is worked-able but not one of
# the 50, so it counts on the map if worked but never in the /50 denominator.
US_STATES = frozenset((
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
))


def _longest_streak(day_keys):
    """Longest run of consecutive calendar days that each have ≥1 QSO.
    day_keys = iterable of 'YYYYMMDD' strings. Returns {days, start, end}."""
    ords = []
    for s in day_keys:
        try:
            ords.append((date(int(s[:4]), int(s[4:6]), int(s[6:8])).toordinal(), s))
        except Exception:
            pass
    ords.sort()
    if not ords:
        return {"days": 0, "start": "", "end": ""}
    best = {"days": 1, "start": ords[0][1], "end": ords[0][1]}
    cur_len, cur_start, prev = 1, ords[0][1], ords[0][0]
    for o, s in ords[1:]:
        if o == prev + 1:
            cur_len += 1
        elif o != prev:            # o == prev would be a duplicate day; ignore
            cur_len, cur_start = 1, s
        if cur_len > best["days"]:
            best = {"days": cur_len, "start": cur_start, "end": s}
        prev = o
    return best


def freq_to_band(hz):
    mhz = hz / 1e6
    for lo, hi, b in BANDS:
        if lo <= mhz <= hi:
            return b
    return "—"


def _qso_epoch(d, t):
    """UTC epoch seconds from an ADIF qso_date (YYYYMMDD) + time (HHMM[SS])."""
    if not (d and len(d) == 8 and d.isdigit()):
        return None
    t = (t or "").strip()
    hh = int(t[0:2]) if len(t) >= 2 and t[0:2].isdigit() else 0
    mm = int(t[2:4]) if len(t) >= 4 and t[2:4].isdigit() else 0
    ss = int(t[4:6]) if len(t) >= 6 and t[4:6].isdigit() else 0
    try:
        return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), hh, mm, ss,
                        tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _compute_contest(qsos):
    """Build the live contest/rate panel from QSO dicts carrying date/time/band/
    mode. A 'session' is the current run of QSOs with no gap longer than
    contest_gap_min (default 60m) — so it spans a UTC-midnight contest but resets
    once you stop operating for a while. Caller holds _lock."""
    st = STATE["settings"]
    gap_sec = float(st.get("contest_gap_min") or cfg("contest_gap_min", 60)) * 60
    active_sec = float(cfg("contest_active_min", 45)) * 60
    now = time.time()
    ev = []
    for q in qsos:
        e = _qso_epoch(q.get("date"), q.get("time"))
        if e is not None:
            ev.append((e, (q.get("band") or "").lower(), (q.get("mode") or "").upper()))
    ev.sort()
    epochs = [e for e, _, _ in ev]
    last10 = sum(1 for e in epochs if e >= now - 600)
    last60 = sum(1 for e in epochs if e >= now - 3600)

    # current run: walk newest→oldest while each gap stays under gap_sec
    run, prev = [], None
    for e, band, mode in reversed(ev):
        if prev is None or prev - e <= gap_sec:
            run.append((e, band, mode))
            prev = e
        else:
            break
    run.reverse()
    session_bands, session_modes = {}, {}
    for e, band, mode in run:
        if band:
            session_bands[band] = session_bands.get(band, 0) + 1
        if mode:
            session_modes[mode] = session_modes.get(mode, 0) + 1

    # best QSOs in any rolling 60 min of the run (sliding window)
    run_ep = [e for e, _, _ in run]
    best_hour, j = 0, 0
    for i in range(len(run_ep)):
        while run_ep[i] - run_ep[j] > 3600:
            j += 1
        best_hour = max(best_hour, i - j + 1)

    last_ts = epochs[-1] if epochs else 0
    STATE["contest"].update({
        "active": bool(epochs) and (now - last_ts <= active_sec),
        "session": len(run),
        "session_start": run_ep[0] if run_ep else 0,
        "bands": session_bands,
        "modes": session_modes,
        "rate_10": last10 * 6,
        "last_60": last60,
        "best_hour": best_hour,
        "last_ts": last_ts,
        # The panel shows once a run reaches this many QSOs, so a couple of casual
        # ragchews don't trip it. Live-tunable from the Config page (0 = any run).
        "min_show": int(st.get("contest_min_qsos", cfg("contest_min_qsos", 5))),
        "updated": now,
    })


def grid_to_latlon(g):
    try:
        g = g.strip().upper()
        if len(g) < 4:
            return None
        lon = (ord(g[0]) - 65) * 20 - 180
        lat = (ord(g[1]) - 65) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6:
            lon += (ord(g[4]) - 65) * (2 / 24) + (1 / 24)
            lat += (ord(g[5]) - 65) * (1 / 24) + (0.5 / 24)
        else:
            lon += 1
            lat += 0.5
        return lat, lon
    except Exception:
        return None


def haversine_km(a, b):
    if not a or not b:
        return None
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# Compact DXCC-ish prefix map (approximate — for display, not award-grade)
PREFIX_COUNTRY = {
    "K": "USA", "W": "USA", "N": "USA", "AA": "USA", "AK": "Alaska", "KH6": "Hawaii",
    "KP4": "Puerto Rico", "VE": "Canada", "VA": "Canada", "XE": "Mexico",
    "G": "England", "M": "England", "2E": "England", "GW": "Wales", "GM": "Scotland",
    "GI": "N. Ireland", "EI": "Ireland", "F": "France", "DL": "Germany", "DK": "Germany",
    "DJ": "Germany", "DB": "Germany", "EA": "Spain", "I": "Italy", "IK": "Italy",
    "IZ": "Italy", "PA": "Netherlands", "ON": "Belgium", "LX": "Luxembourg",
    "HB9": "Switzerland", "OE": "Austria", "OK": "Czech", "OM": "Slovakia",
    "SP": "Poland", "S5": "Slovenia", "9A": "Croatia", "YU": "Serbia", "LZ": "Bulgaria",
    "YO": "Romania", "SV": "Greece", "CT": "Portugal", "EA8": "Canary Is.",
    "SM": "Sweden", "SA": "Sweden", "LA": "Norway", "OZ": "Denmark", "OH": "Finland",
    "ES": "Estonia", "YL": "Latvia", "LY": "Lithuania", "UR": "Ukraine", "UT": "Ukraine",
    "R": "Russia", "UA": "Russia", "RA": "Russia", "EU": "Belarus", "4X": "Israel",
    "JA": "Japan", "JH": "Japan", "JR": "Japan", "JE": "Japan", "JF": "Japan",
    "BY": "China", "BG": "China", "BH": "China", "BA": "China", "HL": "S. Korea",
    "DS": "S. Korea", "VK": "Australia", "ZL": "New Zealand", "YB": "Indonesia",
    "DU": "Philippines", "HS": "Thailand", "9V": "Singapore", "9M": "Malaysia",
    "VU": "India", "AP": "Pakistan", "A6": "UAE", "A7": "Qatar", "HZ": "Saudi Arabia",
    "ZS": "South Africa", "CN": "Morocco", "7X": "Algeria", "SU": "Egypt",
    "5Z": "Kenya", "EA9": "Ceuta", "PY": "Brazil", "PP": "Brazil", "LU": "Argentina",
    "CE": "Chile", "CX": "Uruguay", "HK": "Colombia", "YV": "Venezuela", "OA": "Peru",
    "CP": "Bolivia", "HC": "Ecuador", "ZP": "Paraguay", "PJ": "Curacao", "PZ": "Suriname",
    "V3": "Belize", "TI": "Costa Rica", "HP": "Panama", "YS": "El Salvador",
    "TG": "Guatemala", "HR": "Honduras", "YN": "Nicaragua", "CO": "Cuba", "HI": "Dominican Rep.",
    "6Y": "Jamaica", "8P": "Barbados", "9Y": "Trinidad", "FG": "Guadeloupe",
    "FO": "French Polynesia", "3D2": "Fiji", "KH2": "Guam", "3B8": "Mauritius",
    "TF": "Iceland", "OY": "Faroe Is.", "3A": "Monaco", "T7": "San Marino",
    "9H": "Malta", "TK": "Corsica", "IS0": "Sardinia", "ZB": "Gibraltar",
}


def callsign_country(call):
    if not call:
        return None
    c = call.strip().upper()
    c = re.sub(r"^\d+", "", c)  # drop stray leading
    for length in (3, 2, 1):
        pref = c[:length]
        if pref in PREFIX_COUNTRY:
            return PREFIX_COUNTRY[pref]
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  WSJT-X / JTDX  UDP protocol reader
# ══════════════════════════════════════════════════════════════════════════════
WSJTX_MAGIC = 0xADBCCBDA

# QSOs captured live from WSJT-X "QSO Logged" UDP packets (this run only).
# Used to drive the log panel when no ADIF file is reachable (cross-PC case).
_session_qsos = []
_file_log_active = False
# True while rigctld / HRD / Flrig is feeding the rig's real mode/freq. When any
# is set, WSJT-X's "mode" (its FT8/FT4 submode) is published as the separate
# digital_mode instead of overwriting the radio mode chip.
_rigctld_online = False
_hrd_online = False
_flrig_online = False


def _qso_key(call, date, hhmm, band, mode):
    """Identity of a single QSO, for de-duplication. The same contact can now
    reach us from more than one place — the live WSJT-X UDP stream, the cloner's
    web-logger bridge / remote (2234) feed, and QRZ once an app has uploaded it —
    so we collapse any that share call + date + minute + band + mode."""
    return ((call or "").upper().strip(), (date or "")[:8], (hhmm or "")[:4],
            (band or "").lower().strip(), (mode or "").upper().strip())


def _hhmm_to_min(t):
    t = (t or "")[:4]
    try:
        return int(t[:2]) * 60 + int(t[2:4])
    except (ValueError, IndexError):
        return -999


def _is_dup_session_qso(q):
    """True if a just-logged QSO already matches a recent session entry (same
    call/band/mode/date within a couple of minutes) — a duplicate injection."""
    for e in _session_qsos[-12:]:
        if (e["call"] == q["call"] and e["band"] == q["band"]
                and e["mode"] == q["mode"] and e["date"] == q["date"]
                and abs(_hhmm_to_min(e["time"]) - _hhmm_to_min(q["time"])) <= 2):
            return True
    return False


class _QReader:
    """Minimal big-endian QDataStream reader for WSJT-X datagrams."""

    def __init__(self, data):
        self.d = data
        self.i = 0

    def u8(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def boolean(self):
        return self.u8() != 0

    def u32(self):
        v = struct.unpack_from(">I", self.d, self.i)[0]
        self.i += 4
        return v

    def i32(self):
        v = struct.unpack_from(">i", self.d, self.i)[0]
        self.i += 4
        return v

    def i64(self):
        v = struct.unpack_from(">q", self.d, self.i)[0]
        self.i += 8
        return v

    def u64(self):
        v = struct.unpack_from(">Q", self.d, self.i)[0]
        self.i += 8
        return v

    def f64(self):
        v = struct.unpack_from(">d", self.d, self.i)[0]
        self.i += 8
        return v

    def string(self):
        n = self.u32()
        if n == 0xFFFFFFFF:
            return ""
        s = self.d[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return s


def _snr_to_smeter(snr):
    """Map an FT8 decode SNR (roughly -24..+20 dB) to an S-meter-style label/%."""
    if snr is None:
        return "—", 0
    pct = max(0, min(100, (snr + 24) / 44 * 100))
    # rough S-scale: S1 ~ bottom, S9 at ~ -6 dB SNR for FT8, +over above
    if snr >= 10:
        label = f"S9+{min(40, int((snr - 10) * 2)):02d}"
    else:
        s = max(1, min(9, int((snr + 24) / 4)))
        label = f"S{s}"
    return label, round(pct)


def _strength_to_smeter(db):
    """Map a rig RX S-meter reading (dB relative to S9, Hamlib STRENGTH) onto the
    same S-label / gauge-% the digital path uses — so the analog needle swings on
    voice and CW too, not just on WSJT-X decodes. 6 dB per S-unit below S9."""
    if db is None:
        return "—", 0
    if db >= 10:                       # well over S9
        pct = 60 + min(40, db) / 40 * 40
        label = f"S9+{min(40, int(db)):02d}"
    elif db >= 0:                      # right around S9
        pct = 60 + db / 40 * 40
        label = "S9"
    else:                              # below S9
        s_units = max(0.0, 9 + db / 6.0)
        pct = max(0.0, (s_units - 1) / 8 * 60)
        label = f"S{max(1, min(9, int(round(s_units))))}"
    return label, round(max(0, min(100, pct)))


def _skip_qdatetime(r):
    """Advance past a serialized Qt QDateTime (date + time + timespec)."""
    r.i64()          # QDate: Julian day number
    r.u32()          # QTime: ms since midnight
    spec = r.u8()    # 0=local 1=UTC 2=offset 3=timezone
    if spec == 2:
        r.i32()      # offset from UTC (seconds)


def _wsjtx_loop():
    host = cfg("wsjtx_udp_host", "0.0.0.0")
    port = int(cfg("wsjtx_udp_port", 2242))
    group = cfg("wsjtx_multicast_group", "") or ""
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            if group:
                mreq = struct.pack("=4sl", socket.inet_aton(group), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                print(f"[wsjtx] joined multicast group {group}")
            sock.settimeout(5.0)
            print(f"[wsjtx] listening on UDP {host}:{port}")
            field_src = None
            while True:
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    # mark radio offline if we've heard nothing for a while
                    with _lock:
                        if STATE["radio"]["source"] == "WSJT-X" and \
                           time.time() - STATE["radio"]["last_seen"] > 30:
                            STATE["radio"]["online"] = False
                    continue
                # WSJT-X telemetry from any NON-loopback source is the field
                # (portable) station: home WSJT-X always reaches the engine as
                # 127.0.0.1 (same PC, or re-emitted by the cloner), so anything
                # arriving from a real remote address — public IP, VPN, Tailscale,
                # or straight at the engine port — is the field. Flip portable.
                src = _addr[0] if _addr else ""
                if src and not src.startswith("127.") and src not in ("::1", "localhost"):
                    if src != field_src:
                        field_src = src
                        print(f"[wsjtx] portable telemetry from {src} — field mode ON")
                    mark_field_telemetry()
                _handle_wsjtx(data)
        except Exception as e:
            print(f"[wsjtx] socket error: {e} — retry in 5s")
            time.sleep(5)


def _handle_wsjtx(data):
    try:
        r = _QReader(data)
        if r.u32() != WSJTX_MAGIC:
            return
        r.u32()                 # schema
        mtype = r.u32()
        r.string()              # client id

        if mtype == 1:          # Status
            dial = r.u64()
            mode = r.string()
            dx_call = r.string()
            report = r.string()
            r.string()          # tx mode
            r.boolean()         # tx enabled
            transmitting = r.boolean()
            decoding = r.boolean()
            rx_df = r.u32()
            upd = {
                "online": True, "source": "WSJT-X", "dial_hz": dial,
                "rx_hz": dial + rx_df, "freq_mhz": round(dial / 1e6, 6),
                "band": freq_to_band(dial),
                "digital_mode": mode or "",     # WSJT-X submode (FT8/FT4/…)
                "tx": transmitting, "decoding": decoding,
                "dx_call": dx_call, "report": report,
                "last_seen": time.time(),
            }
            # The rig's real mode comes from rigctld/HRD/Flrig (LSB/USB/CW). Only
            # fall back to the WSJT-X submode for the mode chip when none feed it.
            if not (_rigctld_online or _hrd_online or _flrig_online):
                upd["mode"] = mode or "—"
            _set("radio", upd)

        elif mtype == 2:        # Decode
            r.boolean()         # new
            t_ms = r.u32()
            snr = r.i32()
            r.f64()             # delta time
            df = r.u32()
            r.string()          # mode
            message = r.string()
            hh = (t_ms // 3600000) % 24
            mm = (t_ms // 60000) % 60
            ss = (t_ms // 1000) % 60
            entry = {
                "time": f"{hh:02d}:{mm:02d}:{ss:02d}", "snr": snr,
                "df": df, "msg": message.strip(), "ts": time.time(),
            }
            with _lock:
                STATE["decodes"].insert(0, entry)
                del STATE["decodes"][40:]
                # signal panel = strongest of the last few decodes
                recent = STATE["decodes"][:8]
                best = max((d["snr"] for d in recent), default=None)
                STATE["signal"]["snr"] = best
                # Flrig's real hardware S-meter owns the needle when present;
                # otherwise derive it from the decode SNR.
                if not _flrig_online:
                    label, pct = _snr_to_smeter(best)
                    STATE["signal"]["s_meter"] = label
                    STATE["signal"]["s_pct"] = pct

        elif mtype == 5:        # QSO Logged
            _skip_qdatetime(r)          # Date/Time OFF
            dx_call = (r.string() or "").upper()
            dx_grid = (r.string() or "").upper()
            tx_freq = r.u64()
            mode = (r.string() or "").upper()
            rst_sent = r.string() or ""
            rst_rcvd = r.string() or ""
            now = datetime.now(timezone.utc)
            qso = {
                "date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M"),
                "call": dx_call, "band": freq_to_band(tx_freq),
                "freq": round(tx_freq / 1e6, 3), "mode": mode,
                "rst_s": rst_sent, "rst_r": rst_rcvd, "grid": dx_grid,
                "country": callsign_country(dx_call) or "",
            }
            with _lock:
                # The same QSO can arrive more than once now that the cloner
                # fans WSJT-X / web-logger / remote packets into one engine —
                # drop duplicate injections so the log panel doesn't double up.
                if _is_dup_session_qso(qso):
                    print(f"[wsjtx] QSO logged (dup ignored): {dx_call} {qso['band']} {mode}")
                else:
                    _session_qsos.append(qso)
                    # QRZ or a local ADIF file is the authority when present; only
                    # build a live session log when neither is driving the panel.
                    # last_qso_ts must move together with the 'recent' list it
                    # points at — so only bump it here when the session log owns
                    # 'recent'. When QRZ/ADIF owns it, that source bumps the
                    # timestamp as it ingests the new QSO (a few seconds later),
                    # keeping the "new contact" flash on the RIGHT contact instead
                    # of flashing the previous one against a stale recent list.
                    if not _file_log_active and not _qrz_log_active:
                        STATE["log"]["last_qso_ts"] = time.time()
                        _rebuild_log_from_session()
                    print(f"[wsjtx] QSO logged: {dx_call} {qso['band']} {mode}")
    except Exception:
        pass  # malformed / partial datagram — ignore


# ══════════════════════════════════════════════════════════════════════════════
#  Hamlib rigctld poller — live rig telemetry (freq/mode/PTT + meters)
# ══════════════════════════════════════════════════════════════════════════════
# Reads whatever the rig exposes over CAT via rigctld: real power, SWR, ALC,
# S-meter, drain voltage/current, temp, etc. Each rig/backend supports a different
# subset, so we probe once on connect and then stream only what actually reports.
#
# (label, Hamlib level name, unit suffix, decimals, tx_only)
_RIG_METERS = [
    ("SWR",  "SWR",        "",   2, True),
    ("ALC",  "ALC",        "",   2, True),
    ("COMP", "COMP_METER", "dB", 0, True),
    ("S",    "STRENGTH",   "dB", 0, False),
    ("Vd",   "VD_METER",   "V",  1, False),
    ("Id",   "ID_METER",   "A",  1, False),
    ("TEMP", "TEMP_METER", "",   0, False),
]


def _rigctld_cmd(sock, cmd):
    sock.sendall((cmd + "\n").encode())
    return sock.recv(1024).decode("utf-8", "replace").strip()


def _rigctld_level(sock, name):
    """Read one Hamlib level via rigctld. Returns a float, or None if the rig /
    backend doesn't support it (rigctld answers 'RPRT -n')."""
    try:
        sock.sendall((f"l {name}\n").encode())
        resp = sock.recv(256).decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not resp or resp.startswith("RPRT"):
        return None
    try:
        return float(resp.split()[0])
    except (ValueError, IndexError):
        return None


def _rigctld_loop():
    global _rigctld_online
    host = cfg("rigctld_host", "127.0.0.1")
    port = int(cfg("rigctld_port", 4532))
    max_w = float(cfg("rig_max_power_watts", 100))
    interval = float(cfg("rigctld_poll_sec", 1.0))
    while True:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.settimeout(3)
                print(f"[rigctld] connected {host}:{port}")
                # Probe once: keep only the meters/power sources this rig reports.
                supported = [m for m in _RIG_METERS
                             if _rigctld_level(sock, m[1]) is not None]
                has_po_w = _rigctld_level(sock, "RFPOWER_METER_WATTS") is not None
                has_po_frac = (not has_po_w
                               and _rigctld_level(sock, "RFPOWER_METER") is not None)
                has_set = _rigctld_level(sock, "RFPOWER") is not None
                print(f"[rigctld] rig reports meters: "
                      f"{[m[0] for m in supported] or 'none'} | power: "
                      f"{'measured W' if has_po_w else 'measured %' if has_po_frac else 'set level' if has_set else 'n/a'}")

                while True:
                    freq = _rigctld_cmd(sock, "f")
                    mode = (_rigctld_cmd(sock, "m").splitlines() or [""])[0]
                    tx = _rigctld_cmd(sock, "t").strip() == "1"
                    hz = int(freq) if freq.isdigit() else 0

                    # measured TX power out (watts), only meaningful while keyed
                    po = None
                    if has_po_w:
                        v = _rigctld_level(sock, "RFPOWER_METER_WATTS")
                        po = round(v) if v is not None else None
                    elif has_po_frac:
                        v = _rigctld_level(sock, "RFPOWER_METER")
                        po = round(v * max_w) if v is not None else None
                    # the power-knob setting (steady, shown as POWER)
                    set_w = None
                    if has_set:
                        v = _rigctld_level(sock, "RFPOWER")
                        set_w = round(v * max_w) if v is not None else None

                    meters = {}
                    strength_db = None
                    if po is not None and tx:
                        meters["PO"] = f"{po} W"
                    for label, lvl, unit, dec, tx_only in supported:
                        if tx_only and not tx:
                            continue
                        v = _rigctld_level(sock, lvl)
                        if v is not None:
                            meters[label] = f"{v:.{dec}f}{(' ' + unit) if unit else ''}"
                            if lvl == "STRENGTH":
                                strength_db = v

                    _rigctld_online = True
                    with _lock:
                        rd = STATE["radio"]
                        rd["cat_online"] = True
                        rd["power_w"] = set_w if set_w is not None else po
                        rd["meters"] = meters
                        # The rig's real mode always comes from CAT, even while
                        # WSJT-X owns freq/tx — so the mode chip shows LSB/USB/CW
                        # and WSJT-X's FT8/FT4 rides alongside as digital_mode.
                        if mode:
                            rd["mode"] = mode
                        # WSJT-X drives freq/tx when it's actively feeding us;
                        # otherwise rigctld does. Meters flow either way.
                        if rd["source"] != "WSJT-X" or time.time() - rd["last_seen"] > 10:
                            rd.update({
                                "online": True, "source": "rigctld",
                                "dial_hz": hz, "rx_hz": hz,
                                "freq_mhz": round(hz / 1e6, 6),
                                "band": freq_to_band(hz),
                                "tx": tx, "last_seen": time.time(),
                            })
                            # Drive the analog S-meter from the rig's RX signal
                            # strength so the needle lives on voice/CW too (WSJT-X
                            # owns the gauge whenever it's the active source).
                            if not tx and strength_db is not None:
                                lbl, spct = _strength_to_smeter(strength_db)
                                STATE["signal"] = {"snr": None, "s_meter": lbl, "s_pct": spct}
                    time.sleep(interval)
        except Exception:
            _rigctld_online = False
            with _lock:
                STATE["radio"]["cat_online"] = False
            time.sleep(8)


# ══════════════════════════════════════════════════════════════════════════════
#  ADIF logbook watcher + stats
# ══════════════════════════════════════════════════════════════════════════════
def _find_adif():
    p = cfg("adif_log_path", "") or ""
    if p and Path(p).exists():
        return Path(p)
    for extra in cfg("extra_adif_paths", []) or []:
        try:
            if Path(extra).exists():
                return Path(extra)
        except Exception:
            pass
    # Only fall back to this PC's local WSJT-X log if autodetect is on.
    # In a cross-PC setup that local file is stale, so autodetect defaults off.
    if not cfg("adif_autodetect", False):
        return None
    la = os.environ.get("LOCALAPPDATA", "")
    for c in (Path(la) / "WSJT-X" / "wsjtx_log.adi",
              Path(la) / "JTDX" / "wsjtx_log.adi",
              Path(la) / "WSJT-X" / "wsjtx_log.adif"):
        try:
            if c.exists():
                return c
        except Exception:
            pass
    return None


_ADIF_TAG = re.compile(r"<(\w+)(?::(\d+))?(?::[^>]*)?>", re.I)


def parse_adif(text):
    # skip header if present
    low = text.lower()
    h = low.find("<eoh>")
    if h != -1:
        text = text[h + 5:]
    records, cur, pos = [], {}, 0
    for m in _ADIF_TAG.finditer(text):
        tag = m.group(1).lower()
        if tag == "eor":
            if cur:
                records.append(cur)
            cur = {}
            continue
        ln = m.group(2)
        if ln is None:
            continue
        start = m.end()
        cur[tag] = text[start:start + int(ln)]
    return records


def _rebuild_log_from_session():
    """Populate STATE['log'] from live UDP-captured QSOs (caller holds _lock)."""
    my_ll = grid_to_latlon(cfg("grid", "") or "")
    calls, bands, modes, grids, countries = set(), set(), set(), set(), set()
    band_bd, mode_bd, best = {}, {}, None
    reach = {}
    for q in _session_qsos:
        if q["call"]:
            calls.add(q["call"])
        if q["band"] and q["band"] != "—":
            bands.add(q["band"])
            band_bd[q["band"]] = band_bd.get(q["band"], 0) + 1
        if q["mode"]:
            modes.add(q["mode"])
            mode_bd[q["mode"]] = mode_bd.get(q["mode"], 0) + 1
        if q["country"]:
            countries.add(q["country"])
        if q["grid"]:
            grids.add(q["grid"][:4])
            if q["grid"][:4] not in reach:
                ll = grid_to_latlon(q["grid"])
                if ll:
                    reach[q["grid"][:4]] = [round(ll[0], 1), round(ll[1], 1)]
            if my_ll:
                km = haversine_km(my_ll, grid_to_latlon(q["grid"]))
                if km and (best is None or km > best["km"]):
                    best = {"call": q["call"], "km": round(km),
                            "country": q["country"], "grid": q["grid"][:6]}
    recent = [dict(q) for q in _session_qsos[-15:][::-1]]
    map_recent = []
    for q in _session_qsos[::-1]:
        if len(map_recent) >= 60:
            break
        if q["grid"]:
            ll = grid_to_latlon(q["grid"])
            if ll:
                map_recent.append({
                    "lat": round(ll[0], 2), "lon": round(ll[1], 2),
                    "call": q["call"], "band": q["band"], "mode": q["mode"],
                    "date": q.get("date", ""), "time": q.get("time", ""),
                })
    STATE["log"].update({
        "total": len(_session_qsos), "unique_calls": len(calls),
        "bands": len(bands), "modes": len(modes), "countries": len(countries),
        "grids": len(grids), "best_dx": best, "recent": recent,
        "band_breakdown": band_bd, "mode_breakdown": mode_bd,
        "adif_path": "live session · WSJT-X UDP",
    })
    STATE["map"].update({
        "station": [round(my_ll[0], 3), round(my_ll[1], 3)] if my_ll else None,
        "reach": list(reach.values()),
        "recent": map_recent,
        "updated": time.time(),
    })
    # WSJT-X Type-5 QSO packets don't carry a US state, so WAS stays empty on the
    # live-session path (QRZ is the real award source); DXCC/grids still populate.
    was = sorted({q["state"] for q in _session_qsos
                  if (q.get("state") or "").upper() in US_STATES})
    STATE["awards"].update({
        "was": {"worked": was, "count": len(was)},
        "was_bands": {},
        "dxcc": len(countries),
        "grids": len(grids),
        "updated": time.time(),
    })
    daily, hours, by_year, day_all, ctry_first = {}, [0] * 24, {}, {}, {}
    for q in _session_qsos:
        d = q.get("date") or ""
        if len(d) == 8 and d.isdigit():
            by_year[d[:4]] = by_year.get(d[:4], 0) + 1
            daily[d] = daily.get(d, 0) + 1
            day_all[d] = day_all.get(d, 0) + 1
            c = q.get("country") or ""
            if c and (c not in ctry_first or d < ctry_first[c]):
                ctry_first[c] = d
        t = q.get("time") or ""
        if len(t) >= 2 and t[:2].isdigit():
            hr = int(t[:2])
            if 0 <= hr < 24:
                hours[hr] += 1
    STATE["activity"].update({
        "daily": daily, "hours": hours, "by_year": by_year, "updated": time.time(),
    })
    _compute_contest([{"date": q.get("date"), "time": q.get("time"),
                       "band": q.get("band"), "mode": q.get("mode")}
                      for q in _session_qsos])
    first_qso = min(day_all) if day_all else ""
    mid_date = max(day_all, key=lambda k: day_all[k]) if day_all else ""
    newest = max(ctry_first.items(), key=lambda kv: kv[1]) if ctry_first else ("", "")
    bb = max(band_bd.items(), key=lambda kv: kv[1]) if band_bd else ("", 0)
    bh = max(range(24), key=lambda h: hours[h]) if any(hours) else None
    years = 0
    if first_qso:
        try:
            fd = date(int(first_qso[:4]), int(first_qso[4:6]), int(first_qso[6:8]))
            years = max(0, int((date.today() - fd).days / 365.25))
        except Exception:
            years = 0
    STATE["records"].update({
        "first_qso": first_qso, "years_on_air": years,
        "most_in_day": {"count": (day_all[mid_date] if mid_date else 0), "date": mid_date},
        "longest_streak": _longest_streak(day_all.keys()),
        "newest_dxcc": {"country": newest[0], "date": newest[1]},
        "busiest_band": {"band": bb[0], "count": bb[1]},
        "busiest_hour": {"hour": bh, "count": (hours[bh] if bh is not None else 0)},
        "updated": time.time(),
    })


def _apply_log_records(records, source_label):
    """Compute logbook stats from parsed ADIF records and publish to STATE['log'].
    Shared by the local ADIF file watcher and the QRZ logbook sync so both paths
    produce identical stats. Prefers each record's own COUNTRY field (QRZ and most
    loggers fill it) and falls back to the approximate callsign-prefix guess."""
    my_grid = cfg("grid", "")
    my_ll = grid_to_latlon(my_grid) if my_grid else None

    calls, bands, modes, grids, countries = set(), set(), set(), set(), set()
    band_bd, mode_bd, best = {}, {}, None
    reach = {}  # grid-field -> [lat, lon] for the all-time map glow
    was = set()          # US states worked (any band) — Worked All States
    was_band = {}        # band -> set(state) for 5-band WAS progress
    daily, hours, by_year = {}, [0] * 24, {}   # activity heatmaps
    day_all, ctry_first = {}, {}                # all-time: for records ribbon
    day_cutoff = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y%m%d")
    for r in records:
        call = (r.get("call") or "").upper()
        band = (r.get("band") or "").lower()
        mode = (r.get("mode") or r.get("submode") or "").upper()
        grid = (r.get("gridsquare") or "").upper()
        ctry = (r.get("country") or "").strip() or callsign_country(call)
        st = (r.get("state") or "").strip().upper()
        if call:
            calls.add(call)
        if band:
            bands.add(band)
            band_bd[band] = band_bd.get(band, 0) + 1
        if mode:
            modes.add(mode)
            mode_bd[mode] = mode_bd.get(mode, 0) + 1
        if grid:
            grids.add(grid[:4])
            if grid[:4] not in reach:
                ll = grid_to_latlon(grid)
                if ll:
                    reach[grid[:4]] = [round(ll[0], 1), round(ll[1], 1)]
        if ctry:
            countries.add(ctry)
        if st in US_STATES:
            was.add(st)
            if band:
                was_band.setdefault(band, set()).add(st)
        d = r.get("qso_date") or ""
        if len(d) == 8 and d.isdigit():
            by_year[d[:4]] = by_year.get(d[:4], 0) + 1
            day_all[d] = day_all.get(d, 0) + 1
            if d >= day_cutoff:
                daily[d] = daily.get(d, 0) + 1
            if ctry and (ctry not in ctry_first or d < ctry_first[ctry]):
                ctry_first[ctry] = d
        t = r.get("time_on") or ""
        if len(t) >= 2 and t[:2].isdigit():
            hr = int(t[:2])
            if 0 <= hr < 24:
                hours[hr] += 1
        if my_ll and grid:
            km = haversine_km(my_ll, grid_to_latlon(grid))
            if km and (best is None or km > best["km"]):
                best = {"call": call, "km": round(km), "country": ctry or "", "grid": grid[:6]}

    # newest 15 UNIQUE QSOs (newest first). The dedup is applied only to this
    # visible list — so a QSO QRZ transiently returns twice can't show twice —
    # while total/stats above count every record, matching QRZ's own count.
    recent, seen = [], set()
    map_recent = []  # newest QSOs that carry a grid, for the map arcs
    for r in sorted(records, key=lambda x: (x.get("qso_date", ""), x.get("time_on", "")),
                    reverse=True):
        key = _qso_key(r.get("call"), r.get("qso_date"), r.get("time_on"),
                       r.get("band"), r.get("mode") or r.get("submode"))
        if key in seen:
            continue
        seen.add(key)
        grid = (r.get("gridsquare") or "").upper()
        if len(map_recent) < 60 and grid:
            ll = grid_to_latlon(grid)
            if ll:
                map_recent.append({
                    "lat": round(ll[0], 2), "lon": round(ll[1], 2),
                    "call": (r.get("call") or "").upper(),
                    "band": (r.get("band") or "").lower(),
                    "mode": (r.get("mode") or r.get("submode") or "").upper(),
                    "date": r.get("qso_date", ""),
                    "time": (r.get("time_on", "") or "")[:4],
                })
        if len(recent) < 15:
            recent.append({
                "date": r.get("qso_date", ""), "time": (r.get("time_on", "") or "")[:4],
                "call": (r.get("call") or "").upper(),
                "band": (r.get("band") or "").lower(),
                "freq": r.get("freq", ""),
                "mode": (r.get("mode") or r.get("submode") or "").upper(),
                "rst_s": r.get("rst_sent", ""), "rst_r": r.get("rst_rcvd", ""),
                "country": (r.get("country") or "").strip()
                           or callsign_country((r.get("call") or "").upper()) or "",
            })
        if len(recent) >= 15 and len(map_recent) >= 60:
            break

    with _lock:
        prev_total = STATE["log"]["total"]
        STATE["log"].update({
            "total": len(records), "unique_calls": len(calls), "bands": len(bands),
            "modes": len(modes), "countries": len(countries), "grids": len(grids),
            "best_dx": best, "recent": recent,
            "band_breakdown": band_bd, "mode_breakdown": mode_bd,
            "adif_path": source_label,
        })
        STATE["map"].update({
            "station": [round(my_ll[0], 3), round(my_ll[1], 3)] if my_ll else None,
            "reach": list(reach.values()),
            "recent": map_recent,
            "updated": time.time(),
        })
        STATE["awards"].update({
            "was": {"worked": sorted(was), "count": len(was)},
            "was_bands": {b: len(s) for b, s in was_band.items()},
            "dxcc": len(countries),
            "grids": len(grids),
            "updated": time.time(),
        })
        STATE["activity"].update({
            "daily": daily, "hours": hours, "by_year": by_year,
            "updated": time.time(),
        })
        _compute_contest([{"date": r.get("qso_date"), "time": r.get("time_on"),
                           "band": r.get("band"),
                           "mode": r.get("mode") or r.get("submode")}
                          for r in records])
        # station records / milestones
        first_qso = min(day_all) if day_all else ""
        mid_date, mid_cnt = ("", 0)
        if day_all:
            mid_date = max(day_all, key=lambda k: day_all[k])
            mid_cnt = day_all[mid_date]
        newest = max(ctry_first.items(), key=lambda kv: kv[1]) if ctry_first else ("", "")
        bb = max(band_bd.items(), key=lambda kv: kv[1]) if band_bd else ("", 0)
        bh = max(range(24), key=lambda h: hours[h]) if any(hours) else None
        years = 0
        if first_qso:
            try:
                fd = date(int(first_qso[:4]), int(first_qso[4:6]), int(first_qso[6:8]))
                years = max(0, int((date.today() - fd).days / 365.25))
            except Exception:
                years = 0
        STATE["records"].update({
            "first_qso": first_qso, "years_on_air": years,
            "most_in_day": {"count": mid_cnt, "date": mid_date},
            "longest_streak": _longest_streak(day_all.keys()),
            "newest_dxcc": {"country": newest[0], "date": newest[1]},
            "busiest_band": {"band": bb[0], "count": bb[1]},
            "busiest_hour": {"hour": bh, "count": (hours[bh] if bh is not None else 0)},
            "updated": time.time(),
        })
        if len(records) > prev_total and prev_total > 0:
            STATE["log"]["last_qso_ts"] = time.time()


def _recompute_log(path):
    global _file_log_active
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"[adif] read failed: {e}")
        return
    _apply_log_records(parse_adif(text), str(path))
    _file_log_active = True


def _adif_loop():
    global _file_log_active
    last_mtime = 0
    warned = False
    while True:
        path = _find_adif()
        if not path:
            _file_log_active = False
            if not warned:
                print("[adif] no ADIF file — logbook will build live from WSJT-X UDP")
                warned = True
            time.sleep(8)
            continue
        try:
            mt = path.stat().st_mtime
            if mt != last_mtime:
                last_mtime = mt
                _recompute_log(path)
        except Exception:
            pass
        time.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
#  QRZ.com Logbook API sync  (full log + all new contacts, any mode)
# ══════════════════════════════════════════════════════════════════════════════
# When a QRZ Logbook API key is present the logbook panel is driven entirely by
# QRZ — your complete history plus every new QSO your apps upload there, regardless
# of mode. READ-ONLY: only ACTION=FETCH is used; nothing is ever written to QRZ.
_qrz_log_active = False
# Incremental sync state: keep the whole book in memory keyed by QRZ log id, then
# each cycle fetch only records newer than the highest id we hold (tiny). A full
# re-sync runs occasionally to reconcile edits/deletes (which keep their old id).
_qrz_by_logid = {}
_qrz_no_logid = []
_qrz_last_full = 0.0


QRZ_API = "https://logbook.qrz.com/api"


def _qrz_response(body):
    """Parse a QRZ Logbook API response. The metadata fields (RESULT/COUNT/…) are
    '&'-joined key=value pairs, but the ADIF payload is the FINAL field and QRZ
    ships it whole — so slice it off after 'ADIF=' rather than URL-decoding/splitting
    the body (which shreds it). QRZ HTML-entity-encodes the ADIF (&lt;call:5&gt;…),
    so unescape it back to real <tags> before the ADIF parser sees it."""
    idx = body.find("ADIF=")
    meta = body[:idx] if idx != -1 else body
    fields = dict(urllib.parse.parse_qsl(meta, keep_blank_values=True))
    adif = body[idx + 5:] if idx != -1 else ""
    if "%3C" in adif or "%3c" in adif:      # some installs url-encode it
        adif = urllib.parse.unquote_plus(adif)
    adif = html.unescape(adif)              # QRZ sends &lt;call:5&gt; -> <call:5>
    fields["ADIF"] = adif
    return fields


def _qrz_fetch_records(api_key, after=0):
    """Download QRZ logbook records with LOGID > `after` as parsed ADIF records
    (read-only). after=0 fetches the whole book; a higher cursor fetches only the
    records newer than it. Pages through with the AFTERLOGID cursor."""
    records = []
    for _ in range(1000):  # safety cap on pages (any real logbook fits well under)
        data = urllib.parse.urlencode({
            "KEY": api_key, "ACTION": "FETCH", "OPTION": f"AFTERLOGID:{after}",
        }).encode()
        req = urllib.request.Request(QRZ_API, data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", "replace")
        fields = _qrz_response(body)
        if fields.get("RESULT") != "OK":
            raise RuntimeError(fields.get("REASON") or fields.get("STATUS") or body[:160])
        recs = parse_adif(fields.get("ADIF", ""))
        if not recs:
            if after == 0:   # first page empty despite RESULT=OK -> show what QRZ sent
                print(f"[qrz] empty result — COUNT={fields.get('COUNT')} "
                      f"fields={[k for k in fields if k != 'ADIF']} "
                      f"adif_chars={len(fields.get('ADIF', ''))} head={body[:120]!r}")
            break
        records.extend(recs)
        ids = [int(r["app_qrzlog_logid"]) for r in recs
               if (r.get("app_qrzlog_logid") or "").isdigit()]
        nxt = max(ids) if ids else 0
        if nxt <= after:   # cursor didn't advance -> that was the last/only batch
            break
        after = nxt

    # De-duplicate by QRZ log id (unique per record). QRZ's AFTERLOGID boundary
    # is inclusive, so the last QSO of a page is re-returned on the next page —
    # that echo shares the same logid and must be dropped, while genuinely
    # distinct QSOs (different logids) are all kept, so the total matches QRZ.
    seen, out = set(), []
    for r in records:
        lid = r.get("app_qrzlog_logid") or ""
        if lid.isdigit():
            if lid in seen:
                continue
            seen.add(lid)
        out.append(r)
    return out


def _qrz_sync_once(key, full_every=1800):
    """Run one QRZ sync cycle and publish to STATE. Full re-download on the first
    run or every `full_every` seconds (to catch edits/deletes, which keep their
    old log id); otherwise fetch only records newer than the highest id held and
    merge them in. Returns (total_qsos, kind) where kind describes what happened."""
    global _qrz_log_active, _qrz_last_full
    now = time.time()
    do_full = not _qrz_by_logid or (now - _qrz_last_full) >= full_every
    after = 0 if do_full else max(_qrz_by_logid)
    recs = _qrz_fetch_records(key, after)

    if do_full:
        _qrz_by_logid.clear()
        _qrz_no_logid.clear()
        _qrz_last_full = now

    new = 0
    for r in recs:
        lid = r.get("app_qrzlog_logid") or ""
        if lid.isdigit():
            if int(lid) not in _qrz_by_logid:
                new += 1
            _qrz_by_logid[int(lid)] = r
        else:
            _qrz_no_logid.append(r)
            new += 1

    total = len(_qrz_by_logid) + len(_qrz_no_logid)
    if do_full and total == 0:
        return 0, "empty"
    if do_full or new:
        _apply_log_records(list(_qrz_by_logid.values()) + _qrz_no_logid,
                           f"QRZ Logbook ({total} QSOs)")
        _qrz_log_active = True
        return total, ("full re-sync" if do_full else f"+{new} new")
    return total, "no change"


def _qrz_loop():
    key = os.environ.get("QRZ_API_KEY", "") or cfg("qrz_api_key", "")
    if not key:
        print("[qrz] no API key — set QRZ_API_KEY (or qrz_api_key). QRZ sync disabled.")
        return
    interval = int(cfg("qrz_sync_sec", 60))
    full_every = int(cfg("qrz_full_sync_sec", 1800))
    print(f"[qrz] logbook sync ON (read-only) — new IDs every {interval}s, "
          f"full re-sync every {full_every}s")
    while True:
        try:
            total, kind = _qrz_sync_once(key, full_every)
            if kind == "empty":
                print("[qrz] fetch returned 0 records — check API key / logbook not empty")
            elif kind != "no change":
                print(f"[qrz] synced {total} QSOs ({kind})")
        except Exception as e:
            print(f"[qrz] sync failed ({e}) — keeping last log, retrying")
        time.sleep(interval)


# ══════════════════════════════════════════════════════════════════════════════
#  Public data pollers  (solar / POTA / ISS / DX)
# ══════════════════════════════════════════════════════════════════════════════
def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


def _solar_loop():
    interval = int(cfg("poll_solar_sec", 600))
    while True:
        try:
            data = _get("https://www.hamqsl.com/solarxml.php").decode("utf-8", "replace")
            solar = ET.fromstring(data).find(".//solardata")
            if solar is not None:
                bands = []
                for name in ("80m-40m", "30m-20m", "17m-15m", "12m-10m"):
                    day = solar.findtext(
                        f"calculatedconditions/band[@name='{name}'][@time='day']", "—")
                    night = solar.findtext(
                        f"calculatedconditions/band[@name='{name}'][@time='night']", "—")
                    bands.append({"band": name, "day": day, "night": night})
                _set("solar", {
                    "sfi": solar.findtext("solarflux", "—").strip(),
                    "a_index": solar.findtext("aindex", "—").strip(),
                    "k_index": solar.findtext("kindex", "—").strip(),
                    "sunspots": solar.findtext("sunspots", "—").strip(),
                    "solar_wind": solar.findtext("solarwind", "—").strip(),
                    "xray": solar.findtext("xray", "—").strip(),
                    "aurora": solar.findtext("aurora", "—").strip(),
                    "bands": bands, "updated": time.time(),
                })
        except Exception as e:
            print(f"[solar] {e}")
        time.sleep(interval)


def _pota_loop():
    interval = int(cfg("poll_pota_sec", 60))
    while True:
        try:
            spots = json.loads(_get("https://api.pota.app/spot/activator"))
            out = []
            for s in spots[:14]:
                out.append({
                    "call": s.get("activator", ""),
                    "ref": s.get("reference", ""),
                    "name": s.get("name", "") or s.get("locationDesc", ""),
                    "freq": s.get("frequency", ""),
                    "mode": s.get("mode", ""),
                    "loc": s.get("locationDesc", ""),
                })
            with _lock:
                STATE["pota"] = out
        except Exception as e:
            print(f"[pota] {e}")
        time.sleep(interval)


def _iss_loop():
    interval = int(cfg("poll_iss_sec", 10))
    my_ll = grid_to_latlon(cfg("grid", "") or "")
    while True:
        try:
            d = json.loads(_get("https://api.wheretheiss.at/v1/satellites/25544"))
            lat, lon = d.get("latitude"), d.get("longitude")
            rng = haversine_km(my_ll, (lat, lon)) if my_ll else None
            _set("iss", {
                "lat": round(lat, 2), "lon": round(lon, 2),
                "alt_km": round(d.get("altitude", 0)),
                "vel_kmh": round(d.get("velocity", 0)),
                "range_km": round(rng) if rng else None,
            })
        except Exception as e:
            print(f"[iss] {e}")
        time.sleep(interval)


# A DX-cluster "DX de" spot line, e.g.
#   DX de EA4XYZ:     14074.0  K1ABC        FT8 -12 dB              1432Z
_DX_RE = re.compile(
    r"^DX de\s+([A-Z0-9/\-#]+)\s*:?\s+(\d+(?:\.\d+)?)\s+([A-Z0-9/\-]+)\s*(.*)$",
    re.I)


def _parse_dx_spot(line):
    """Parse one DX-cluster spot line into a panel row, or None."""
    m = _DX_RE.match(line.strip())
    if not m:
        return None
    spotter, khz_s, dxc, rest = m.groups()
    try:
        khz = float(khz_s)
    except ValueError:
        return None
    if khz <= 0:
        return None
    rest = re.sub(r"\s*\b\d{3,4}Z\b.*$", "", rest.strip()).strip()  # drop trailing NNNNZ + grid
    return {
        "dx": dxc.upper(),
        "spotter": spotter.upper().rstrip("#").rstrip("-"),
        "freq": round(khz / 1000, 3),
        "band": freq_to_band(khz * 1000),
        "comment": rest[:40],
    }


def _dx_loop():
    """Stream live spots from a DX-cluster telnet node. dxsummit.fi's HTTP API
    is unreachable from the cloud host (connections just time out), so we read
    a real cluster instead — those are built for automated clients and don't
    block datacenter IPs. Host/port default to a public node and can be
    overridden via config (dx_cluster_host/port) or env (DX_CLUSTER_HOST/PORT)."""
    host = os.environ.get("DX_CLUSTER_HOST") or cfg("dx_cluster_host", "dxc.nc7j.com")
    port = int(os.environ.get("DX_CLUSTER_PORT") or cfg("dx_cluster_port", 7373))
    mycall = (cfg("callsign", "W4GGJ") or "W4GGJ").upper()
    spots = []
    while True:
        sk = None
        try:
            sk = socket.create_connection((host, port), timeout=20)
            sk.settimeout(180)
            print(f"[dx] connected to cluster {host}:{port} as {mycall}")
            buf = b""
            logged_in = False
            while True:
                try:
                    data = sk.recv(4096)
                except socket.timeout:
                    print("[dx] cluster idle 180s — reconnecting")
                    break
                if not data:
                    print("[dx] cluster closed the connection")
                    break
                buf += data
                if not logged_in:
                    low = buf.lower()
                    if b"login:" in low or b"call:" in low or b"enter your call" in low:
                        sk.sendall((mycall + "\r\n").encode())
                        logged_in = True
                        buf = b""
                        continue
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if not line.startswith("DX de"):
                        continue
                    spot = _parse_dx_spot(line)
                    if spot:
                        spots.insert(0, spot)
                        del spots[16:]
                        with _lock:
                            STATE["dx"] = list(spots)
        except Exception as e:
            print(f"[dx] cluster {host}:{port} — {e}")
        finally:
            if sk:
                try:
                    sk.close()
                except Exception:
                    pass
        time.sleep(15)   # backoff before reconnect


# ── Boot ──────────────────────────────────────────────────────────────────────
# ── Cloud ingest (relay) ──────────────────────────────────────────────────────
# In the Render/cloud role the radio/decodes/signal/log come from the home agent
# via POST /api/ingest instead of local UDP/ADIF.
_last_ingest = 0
INGEST_SECTIONS = ("radio", "decodes", "signal", "log", "map", "awards",
                   "activity", "records", "contest", "settings", "field")


def ingest(sections):
    """Merge home-agent telemetry into STATE (called by the cloud server)."""
    global _last_ingest
    with _lock:
        for k in INGEST_SECTIONS:
            if k in sections and sections[k] is not None:
                STATE[k] = sections[k]
        _last_ingest = time.time()


def ingest_status():
    """Freshness of the last home-agent push — for the cloud /api/health probe."""
    with _lock:
        age = (time.time() - _last_ingest) if _last_ingest else None
        return {
            "last_ingest": _last_ingest or None,
            "age_sec": round(age, 1) if age is not None else None,
            "radio_online": STATE["radio"]["online"],
        }


def _ingest_watchdog():
    """Mark the radio offline if the home agent stops reporting."""
    while True:
        time.sleep(5)
        with _lock:
            if _last_ingest and time.time() - _last_ingest > 30:
                STATE["radio"]["online"] = False
                STATE["radio"]["source"] = "—"


# No live rig data for this long → the radio is offline. Both WSJT-X (Status
# packets) and Flrig (1 s polls) refresh last_seen well under this while running.
RADIO_STALE_SEC = 20


def _radio_watchdog():
    """Flip the radio card to Offline when every rig source has gone quiet.

    Individual source loops only clear their own 'cat_online' when they drop, and
    the WSJT-X staleness check only fires while it's the active source — so with
    WSJT-X + Flrig both closed nothing marked the radio offline and the card
    froze on the last frequency. This source-agnostic watchdog watches last_seen
    and clears the whole live-radio block (freq included) once it goes stale, so
    closing the rig apps reads as Offline. Runs on the home agent / local (where
    last_seen is set in local time), not the cloud."""
    while True:
        time.sleep(3)
        with _lock:
            rd = STATE["radio"]
            if rd.get("online") and rd.get("last_seen") and \
                    time.time() - rd["last_seen"] > RADIO_STALE_SEC:
                rd.update({
                    "online": False, "cat_online": False, "tx": False,
                    "decoding": False, "source": "—",
                    "dial_hz": 0, "rx_hz": 0, "freq_mhz": 0.0, "band": "—",
                    "dx_call": "", "report": "", "digital_mode": "",
                })


def home_telemetry():
    """Snapshot of just the sections the home agent pushes to the cloud."""
    with _lock:
        _refresh_field()
        return {k: json.loads(json.dumps(STATE[k], default=str)) for k in INGEST_SECTIONS}


# ══════════════════════════════════════════════════════════════════════════════
#  Ham Radio Deluxe (HRD) — direct TCP client (live power / gains / SWR flag)
# ══════════════════════════════════════════════════════════════════════════════
# For stations where HRD owns the rig (WSJT-X -> HRD) so rigctld can't reach the
# CAT port. Speaks HRD Rig Control's protocol on port 7809 directly, read-only.
# HRD exposes RF power, the gains, and status flags (High SWR, Busy) — but NOT the
# numeric PWR/SWR/ALC bar values, so we surface power + gains + a High-SWR warning.
_HRD_SIG1 = 0x1234ABCD
_HRD_SIG2 = 0xABCD1234


def _hrd_build(text):
    core = (struct.pack("<I", _HRD_SIG1) + struct.pack("<I", _HRD_SIG2)
            + b"\x00\x00\x00\x00" + text.encode("utf-16-le") + b"\x00\x00")
    return struct.pack("<I", 4 + len(core)) + core


def _hrd_cmd(sock, text):
    sock.sendall(_hrd_build(text))
    hdr = b""
    while len(hdr) < 4:
        b = sock.recv(4 - len(hdr))
        if not b:
            raise ConnectionError("HRD closed")
        hdr += b
    total = struct.unpack("<I", hdr)[0]
    body = b""
    while len(body) < total - 4:
        b = sock.recv(total - 4 - len(body))
        if not b:
            raise ConnectionError("HRD closed")
        body += b
    # body = sig1(4) + sig2(4) + zero(4) + UTF-16LE text
    return body[12:].decode("utf-16-le", "replace").replace("\x00", "").strip()


def _hrd_num(resp):
    """slider-pos returns 'raw,display' (e.g. '146,57', '14,14 Hz'); take the
    display field's leading number."""
    part = resp.split(",")[-1].strip() if resp else ""
    m = re.match(r"-?\d+(?:\.\d+)?", part)
    return (float(m.group()) if m else None), part


def _hrd_loop():
    global _hrd_online
    host = cfg("hrd_host", "127.0.0.1")
    port = int(cfg("hrd_port", 7809))
    interval = float(cfg("hrd_poll_sec", 1.0))
    max_w = float(cfg("rig_max_power_watts", 100))
    while True:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.settimeout(4)
                ctx = (_hrd_cmd(sock, "get context") or "").strip()
                rig = _hrd_cmd(sock, f"[{ctx}] get radio") if ctx else ""
                print(f"[hrd] connected {host}:{port} — context {ctx}, radio {rig!r}")

                def g(c):
                    try:
                        return _hrd_cmd(sock, f"[{ctx}] {c}")
                    except socket.timeout:
                        return ""

                while True:
                    pw, _ = _hrd_num(g(f"get slider-pos {rig} RF~power"))
                    swr_high = g("get button-select High~SWR").strip() == "1"
                    _, mic = _hrd_num(g(f"get slider-pos {rig} Mic~gain"))
                    _, rfg = _hrd_num(g(f"get slider-pos {rig} RF~gain"))
                    _, nr = _hrd_num(g(f"get slider-pos {rig} Noise~reduction"))

                    # HRD exposes frequency + mode (but not the S-meter value).
                    freq = g("get frequency").strip()
                    hz = int(freq) if freq.lstrip("-").isdigit() else 0
                    mode = g("get mode").strip().upper()

                    meters = {}
                    if swr_high:
                        meters["SWR"] = "HIGH"
                    if rfg:
                        meters["RF"] = rfg
                    if mic:
                        meters["MIC"] = mic
                    if nr:
                        meters["NR"] = nr

                    if mode:
                        _hrd_online = True
                    with _lock:
                        rd = STATE["radio"]
                        rd["cat_online"] = True
                        if pw is not None:
                            # FT-450 RF power level (0..100) maps straight to watts
                            rd["power_w"] = round(pw / 100 * max_w)
                        rd["meters"] = meters
                        # The rig's real mode always comes from HRD, even while
                        # WSJT-X owns freq — mode chip shows LSB/USB/CW and the
                        # FT8/FT4 submode rides alongside as digital_mode.
                        if mode:
                            rd["mode"] = mode
                        # HRD drives freq/online when WSJT-X (and rigctld) aren't
                        # feeding — so the panel stays live on voice with WSJT-X
                        # closed. TX/mic-PTT isn't exposed, so assume RECEIVE.
                        if (not _rigctld_online
                                and (rd["source"] != "WSJT-X"
                                     or time.time() - rd["last_seen"] > 10)):
                            rd.update({
                                "online": True, "source": "HRD",
                                "dial_hz": hz, "rx_hz": hz,
                                "freq_mhz": round(hz / 1e6, 6),
                                "band": freq_to_band(hz),
                                "tx": False, "last_seen": time.time(),
                            })
                            rd["dx_call"] = ""      # stale WSJT-X QSO target
                            # HRD can't read the S-meter value — show no reading
                            # rather than a frozen digital value from earlier.
                            STATE["signal"] = {"snr": None, "s_meter": "—", "s_pct": 0}
                    time.sleep(interval)
        except Exception as e:
            _hrd_online = False
            with _lock:
                STATE["radio"]["cat_online"] = False
            print(f"[hrd] link error ({e}) — retry in 8s")
            time.sleep(8)


# ══════════════════════════════════════════════════════════════════════════════
#  Flrig — XML-RPC client (rig control app that DOES expose the meters)
# ══════════════════════════════════════════════════════════════════════════════
# Flrig owns the FT-450's COM port and publishes frequency, mode, PTT and — unlike
# HRD — the actual S-meter, power-out and SWR over XML-RPC (port 12345). Use this
# when Flrig is your rig controller instead of HRD/rigctld. Read-only.

def _flrig_num(v):
    """First number out of a Flrig XML-RPC value (str/int/list)."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    m = re.match(r"-?\d+(?:\.\d+)?", str(v).strip())
    return float(m.group()) if m else None


def _flrig_smeter(v):
    """Map Flrig's 0-100 S-meter reading to a gauge %/S-label. Flrig's scale is
    treated as ~ the gauge scale (S1..S9 across 0-60%, S9+ above); calibrate the
    constants if it reads high/low against the rig's own meter."""
    pct = _flrig_num(v)
    if pct is None:
        return "—", 0
    pct = max(0.0, min(100.0, pct))
    if pct >= 60:
        over = round((pct - 60) / 40 * 40)          # 60% -> S9, 100% -> S9+40
        label = "S9" if over < 3 else f"S9+{min(40, over):02d}"
    else:
        s = round(1 + pct / 60 * 8)                  # 0% -> S1, 60% -> S9
        label = f"S{max(1, min(9, s))}"
    return label, round(pct)


def _flrig_loop():
    global _flrig_online
    import xmlrpc.client
    host = cfg("flrig_host", "127.0.0.1")
    port = int(cfg("flrig_port", 12345))
    interval = float(cfg("flrig_poll_sec", 1.0))
    max_w = float(cfg("rig_max_power_watts", 100))
    url = f"http://{host}:{port}/"
    while True:
        try:
            rig = xmlrpc.client.ServerProxy(url).rig
            name = str(rig.get_xcvr())
            mp = _flrig_num(rig.get_maxpwr())
            if mp:
                max_w = mp
            print(f"[flrig] connected {url} — rig {name!r}, max {max_w:.0f}W")
            _flrig_online = True
            while True:
                freq = str(rig.get_vfo()).strip()
                hz = int(freq) if freq.isdigit() else 0
                mode = str(rig.get_mode()).strip().upper()
                tx = _flrig_num(rig.get_ptt()) == 1
                sm = rig.get_smeter()                    # 0-100, drives the needle
                sunits = str(rig.get_Sunits()).replace(" ", "").upper()  # "S0".."S9"
                pset = _flrig_num(rig.get_power())       # power-knob setting (watts)

                # PO/SWR only read valid on TX — grab them while transmitting.
                po = swr = None
                if tx:
                    po = _flrig_num(rig.get_pwrmeter())
                    swr = _flrig_num(rig.get_SWR())      # real SWR ratio, e.g. 1.3

                _, pct = _flrig_smeter(sm)
                lbl = sunits if re.match(r"^S\d", sunits) else _flrig_smeter(sm)[0]
                with _lock:
                    rd = STATE["radio"]
                    rd["cat_online"] = True
                    # Keep PO/SWR on the card between overs: only refresh them while
                    # actually transmitting (that's when the meters read real
                    # values); on RX the last transmit's readings are held.
                    if tx:
                        held = dict(rd.get("meters") or {})
                        if po and po > 0:
                            held["PO"] = f"{round(po / 100 * max_w)} W"
                        if swr and swr >= 1.0:
                            held["SWR"] = f"{swr:.1f}"
                        rd["meters"] = held
                    if pset is not None:
                        rd["power_w"] = round(pset)
                    if mode:
                        rd["mode"] = mode
                    if rd["source"] != "WSJT-X" or time.time() - rd["last_seen"] > 10:
                        rd.update({
                            "online": True, "source": "Flrig",
                            "dial_hz": hz, "rx_hz": hz,
                            "freq_mhz": round(hz / 1e6, 6),
                            "band": freq_to_band(hz),
                            "tx": tx, "last_seen": time.time(),
                        })
                    # Real hardware S-meter drives the needle on RX (voice AND
                    # digital). On TX the meter reads 0, so hold the last value.
                    if not tx:
                        STATE["signal"]["s_meter"] = lbl
                        STATE["signal"]["s_pct"] = pct
                time.sleep(interval)
        except Exception as e:
            _flrig_online = False
            with _lock:
                STATE["radio"]["cat_online"] = False
            print(f"[flrig] link error ({e}) — retry in 8s")
            time.sleep(8)


# ══════════════════════════════════════════════════════════════════════════════
#  DXLab Commander bridge — feed a radio-less Commander for DXKeeper voice logging
# ══════════════════════════════════════════════════════════════════════════════
# The problem this solves: when Flrig owns the FT-450's CAT port (so the dashboard
# keeps its live S-meter/power/SWR), DXKeeper can't auto-fill frequency + mode for
# a VOICE QSO — that normally comes from DXLab Commander, which would need the same
# single CAT port Flrig is holding. WSJT-X only feeds the logger when IT logs an
# FT8 QSO (via SpotCollector), so voice contest logging is left typing frequencies.
#
# This bridge closes the gap without a second CAT app on the radio: it reads the
# live dial + mode the engine already gets from Flrig and pushes them to a
# *radio-less* Commander over its TCP command port (default 52002) using
# CmdSetFreqMode. Commander then serves freq + mode to DXKeeper's Capture window on
# demand — so voice QSOs auto-fill while Flrig and the meters stay untouched.
# One-directional and fail-soft: if Commander isn't running it just retries quietly,
# and it only transmits when the frequency or mode actually changes.
#
# Commander frequency format is kHz with 3 decimals; mode is a plain token (USB/
# LSB/CW/…). Because Commander has no radio, each CmdSetFreqMode makes it briefly
# flash a "Radio failed to QSY" notice — harmless (the value still lands in
# DXKeeper). Point Commander's radio at "None" (or a nonexistent COM port) to keep
# it off the real CAT port.

def _commander_msg(command, params=""):
    """Build one DXLab Commander TCP message in ADIF-field syntax."""
    return f"<command:{len(command)}>{command}<parameters:{len(params)}>{params}"


def _commander_mode(mode):
    """Map the rig's mode string to a Commander mode token."""
    m = (mode or "").strip().upper()
    if not m:
        return ""
    if "USB" in m:
        return "USB"
    if "LSB" in m:
        return "LSB"
    if m.startswith(("CW-R", "CWR")):
        return "CW-R"
    if m.startswith("CW"):
        return "CW"
    if m.startswith(("RTTY-R", "RTTYR")):
        return "RTTY-R"
    if m.startswith("RTTY") or m.startswith("FSK"):
        return "RTTY"
    if m.startswith("FM"):
        return "FM"
    if m.startswith("AM"):
        return "AM"
    if "PKT" in m or "DATA" in m or "DIG" in m:
        return "PKT"
    return m


def _commander_bridge_loop():
    host = cfg("commander_host", "127.0.0.1")
    port = int(cfg("commander_port", 52002))
    interval = float(cfg("commander_bridge_poll_sec", 0.5))
    sock = None
    last_sent = None
    last_err_log = 0.0
    while True:
        try:
            with _lock:
                rd = STATE["radio"]
                hz = rd.get("dial_hz") or 0
                mode = rd.get("mode") or ""
                online = rd.get("online")
            if online and hz:
                khz = f"{hz / 1000.0:.3f}"
                cmode = _commander_mode(mode)
                key = (khz, cmode)
                if key != last_sent:
                    if sock is None:
                        sock = socket.create_connection((host, port), timeout=4)
                        last_err_log = 0.0        # let the next drop log promptly
                        print(f"[commander] bridge connected {host}:{port}")
                    if cmode:
                        params = (f"<xcvrfreq:{len(khz)}>{khz}"
                                  f"<xcvrmode:{len(cmode)}>{cmode}")
                        sock.sendall(_commander_msg("CmdSetFreqMode", params).encode())
                    else:
                        params = f"<xcvrfreq:{len(khz)}>{khz}"
                        sock.sendall(_commander_msg("CmdSetFreq", params).encode())
                    last_sent = key
            time.sleep(interval)
        except Exception as e:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            sock = None
            last_sent = None           # resend current dial as soon as we reconnect
            # Keep retrying every 5s (so it reconnects fast when you open
            # Commander) but log at most once a minute so a digital session with
            # Commander closed doesn't spam the agent console.
            now = time.time()
            if now - last_err_log > 60:
                print(f"[commander] bridge offline ({e}) — retrying quietly every 5s")
                last_err_log = now
            time.sleep(5)


def start_engine(enable_wsjtx=None, enable_adif=True, enable_rigctld=None,
                 enable_pollers=True, enable_ingest_watchdog=False, enable_qrz=None,
                 enable_hrd=None, enable_flrig=None, enable_commander_bridge=None,
                 enable_radio_watchdog=None):
    STATE["engine_started"] = time.time()
    if enable_radio_watchdog is None:
        # On by default wherever real rig sources run (agent/local); the cloud
        # gets radio-offline from the ingest watchdog + the agent's own state.
        enable_radio_watchdog = not enable_ingest_watchdog
    if enable_wsjtx is None:
        enable_wsjtx = cfg("wsjtx_enabled", True)
    if enable_rigctld is None:
        enable_rigctld = cfg("rigctld_enabled", False)
    if enable_qrz is None:
        enable_qrz = bool(os.environ.get("QRZ_API_KEY") or cfg("qrz_api_key", "")) \
            or cfg("qrz_enabled", False)
    if enable_hrd is None:
        enable_hrd = cfg("hrd_enabled", False)
    if enable_flrig is None:
        enable_flrig = cfg("flrig_enabled", False)
    if enable_commander_bridge is None:
        enable_commander_bridge = cfg("commander_bridge_enabled", False)

    threads = []
    if enable_wsjtx:
        threads.append(("wsjtx", _wsjtx_loop))
    if enable_rigctld:
        threads.append(("rigctld", _rigctld_loop))
    if enable_hrd:
        threads.append(("hrd", _hrd_loop))
    if enable_flrig:
        threads.append(("flrig", _flrig_loop))
    if enable_commander_bridge:
        threads.append(("commander", _commander_bridge_loop))
    if enable_adif:
        threads.append(("adif", _adif_loop))
    if enable_qrz:
        threads.append(("qrz", _qrz_loop))
    if enable_pollers:
        threads += [("solar", _solar_loop), ("pota", _pota_loop),
                    ("iss", _iss_loop), ("dx", _dx_loop)]
    if enable_ingest_watchdog:
        threads.append(("ingest-wd", _ingest_watchdog))
    if enable_radio_watchdog:
        threads.append(("radio-wd", _radio_watchdog))

    for name, fn in threads:
        threading.Thread(target=fn, name=name, daemon=True).start()
    print(f"[engine] started {len(threads)} threads "
          f"({', '.join(n for n, _ in threads)}) for {STATE['identity']['callsign']}")


if __name__ == "__main__":
    # standalone test: print the state snapshot every few seconds
    start_engine()
    try:
        while True:
            time.sleep(4)
            s = snapshot()
            r = s["radio"]
            print(f"radio={r['source']} {r['freq_mhz']:.4f}MHz {r['band']} "
                  f"{r['mode']} tx={r['tx']} | decodes={len(s['decodes'])} "
                  f"| log={s['log']['total']} QSOs | SFI={s['solar']['sfi']}")
    except KeyboardInterrupt:
        print("\n[engine] stopped")
