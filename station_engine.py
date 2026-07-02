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

import html
import json
import math
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Browser-like User-Agent. Some public feeds (notably the Cloudflare-fronted
# dxsummit.fi DX-cluster API) 403 non-browser agents like "Python-urllib" or a
# bare product token, which left the DX panel permanently empty on the cloud.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"}


# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    cfg_path = HERE / "station.config.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception as e:
        print(f"[engine] config load failed ({e}) — using defaults")
        return {}


CONFIG = load_config()


def cfg(key, default=None):
    return CONFIG.get(key, default)


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
    "server_time": 0,
    "engine_started": 0,
}


def _set(section, patch):
    with _lock:
        if section in STATE and isinstance(STATE[section], dict):
            STATE[section].update(patch)
        else:
            STATE[section] = patch


def snapshot():
    with _lock:
        STATE["server_time"] = time.time()
        return json.loads(json.dumps(STATE, default=str))


# ── Helpers ───────────────────────────────────────────────────────────────────
BANDS = [
    (0.1357, 0.1378, "2200m"), (0.472, 0.479, "630m"), (1.8, 2.0, "160m"),
    (3.5, 4.0, "80m"), (5.3, 5.4, "60m"), (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"), (14.0, 14.35, "20m"), (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"), (24.89, 24.99, "12m"), (28.0, 29.7, "10m"),
    (50.0, 54.0, "6m"), (144.0, 148.0, "2m"), (222.0, 225.0, "1.25m"),
    (420.0, 450.0, "70cm"),
]


def freq_to_band(hz):
    mhz = hz / 1e6
    for lo, hi, b in BANDS:
        if lo <= mhz <= hi:
            return b
    return "—"


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
# True while the Hamlib rigctld daemon is connected and feeding the rig's real
# mode/freq. When set, WSJT-X's "mode" (its FT8/FT4 submode) is published as the
# separate digital_mode instead of overwriting the radio mode chip.
_rigctld_online = False


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
            # The rig's real mode comes from rigctld (LSB/USB/CW). Only fall back
            # to the WSJT-X submode for the mode chip when rigctld isn't feeding.
            if not _rigctld_online:
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
                label, pct = _snr_to_smeter(best)
                STATE["signal"] = {"snr": best, "s_meter": label, "s_pct": pct}

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
                    STATE["log"]["last_qso_ts"] = time.time()
                    _session_qsos.append(qso)
                    # QRZ or a local ADIF file is the authority when present; only
                    # build a live session log when neither is driving the panel.
                    if not _file_log_active and not _qrz_log_active:
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
            if my_ll:
                km = haversine_km(my_ll, grid_to_latlon(q["grid"]))
                if km and (best is None or km > best["km"]):
                    best = {"call": q["call"], "km": round(km),
                            "country": q["country"], "grid": q["grid"][:6]}
    recent = [dict(q) for q in _session_qsos[-15:][::-1]]
    STATE["log"].update({
        "total": len(_session_qsos), "unique_calls": len(calls),
        "bands": len(bands), "modes": len(modes), "countries": len(countries),
        "grids": len(grids), "best_dx": best, "recent": recent,
        "band_breakdown": band_bd, "mode_breakdown": mode_bd,
        "adif_path": "live session · WSJT-X UDP",
    })


def _apply_log_records(records, source_label):
    """Compute logbook stats from parsed ADIF records and publish to STATE['log'].
    Shared by the local ADIF file watcher and the QRZ logbook sync so both paths
    produce identical stats. Prefers each record's own COUNTRY field (QRZ and most
    loggers fill it) and falls back to the approximate callsign-prefix guess."""
    my_grid = cfg("grid", "")
    my_ll = grid_to_latlon(my_grid) if my_grid else None

    # Collapse duplicate records (e.g. QRZ transiently returning a fresh QSO
    # twice after two apps upload it) so stats and the recent list stay honest.
    seen, deduped = set(), []
    for r in records:
        key = _qso_key(r.get("call"), r.get("qso_date"),
                       r.get("time_on"), r.get("band"),
                       r.get("mode") or r.get("submode"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    records = deduped

    calls, bands, modes, grids, countries = set(), set(), set(), set(), set()
    band_bd, mode_bd, best = {}, {}, None
    for r in records:
        call = (r.get("call") or "").upper()
        band = (r.get("band") or "").lower()
        mode = (r.get("mode") or r.get("submode") or "").upper()
        grid = (r.get("gridsquare") or "").upper()
        ctry = (r.get("country") or "").strip() or callsign_country(call)
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
        if ctry:
            countries.add(ctry)
        if my_ll and grid:
            km = haversine_km(my_ll, grid_to_latlon(grid))
            if km and (best is None or km > best["km"]):
                best = {"call": call, "km": round(km), "country": ctry or "", "grid": grid[:6]}

    # newest 15 by QSO date/time, independent of file/record order
    recent = []
    for r in sorted(records, key=lambda x: (x.get("qso_date", ""), x.get("time_on", "")))[-15:][::-1]:
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

    with _lock:
        prev_total = STATE["log"]["total"]
        STATE["log"].update({
            "total": len(records), "unique_calls": len(calls), "bands": len(bands),
            "modes": len(modes), "countries": len(countries), "grids": len(grids),
            "best_dx": best, "recent": recent,
            "band_breakdown": band_bd, "mode_breakdown": mode_bd,
            "adif_path": source_label,
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


def _qrz_fetch_records(api_key):
    """Download the full QRZ logbook as parsed ADIF records (read-only).

    Pages through the log with the AFTERLOGID cursor (per-record APP_QRZLOG_LOGID);
    most logs return in one batch and the loop then stops on the empty next page."""
    records, after = [], 0
    for _ in range(1000):  # safety cap on pages (any real logbook fits well under)
        data = urllib.parse.urlencode({
            "KEY": api_key, "ACTION": "FETCH", "OPTION": f"AFTERLOGID:{after}",
        }).encode()
        req = urllib.request.Request(QRZ_API, data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    return records


def _qrz_loop():
    global _qrz_log_active
    key = os.environ.get("QRZ_API_KEY", "") or cfg("qrz_api_key", "")
    if not key:
        print("[qrz] no API key — set QRZ_API_KEY (or qrz_api_key). QRZ sync disabled.")
        return
    interval = int(cfg("qrz_sync_sec", 300))
    print(f"[qrz] logbook sync ON (read-only) — refresh every {interval}s")
    while True:
        try:
            recs = _qrz_fetch_records(key)
            if recs:
                _apply_log_records(recs, f"QRZ Logbook ({len(recs)} QSOs)")
                _qrz_log_active = True
                print(f"[qrz] synced {len(recs)} QSOs from QRZ")
            else:
                print("[qrz] fetch returned 0 records — check API key / logbook not empty")
        except Exception as e:
            print(f"[qrz] sync failed ({e}) — keeping last log, retrying")
        time.sleep(interval)


# ══════════════════════════════════════════════════════════════════════════════
#  Public data pollers  (solar / POTA / ISS / DX)
# ══════════════════════════════════════════════════════════════════════════════
def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
INGEST_SECTIONS = ("radio", "decodes", "signal", "log")


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


def home_telemetry():
    """Snapshot of just the sections the home agent pushes to the cloud."""
    with _lock:
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

                    meters = {}
                    if swr_high:
                        meters["SWR"] = "HIGH"
                    if rfg:
                        meters["RF"] = rfg
                    if mic:
                        meters["MIC"] = mic
                    if nr:
                        meters["NR"] = nr
                    with _lock:
                        rd = STATE["radio"]
                        rd["cat_online"] = True
                        if pw is not None:
                            # FT-450 RF power level (0..100) maps straight to watts
                            rd["power_w"] = round(pw / 100 * max_w)
                        rd["meters"] = meters
                    time.sleep(interval)
        except Exception as e:
            with _lock:
                STATE["radio"]["cat_online"] = False
            print(f"[hrd] link error ({e}) — retry in 8s")
            time.sleep(8)


def start_engine(enable_wsjtx=None, enable_adif=True, enable_rigctld=None,
                 enable_pollers=True, enable_ingest_watchdog=False, enable_qrz=None,
                 enable_hrd=None):
    STATE["engine_started"] = time.time()
    if enable_wsjtx is None:
        enable_wsjtx = cfg("wsjtx_enabled", True)
    if enable_rigctld is None:
        enable_rigctld = cfg("rigctld_enabled", False)
    if enable_qrz is None:
        enable_qrz = bool(os.environ.get("QRZ_API_KEY") or cfg("qrz_api_key", "")) \
            or cfg("qrz_enabled", False)
    if enable_hrd is None:
        enable_hrd = cfg("hrd_enabled", False)

    threads = []
    if enable_wsjtx:
        threads.append(("wsjtx", _wsjtx_loop))
    if enable_rigctld:
        threads.append(("rigctld", _rigctld_loop))
    if enable_hrd:
        threads.append(("hrd", _hrd_loop))
    if enable_adif:
        threads.append(("adif", _adif_loop))
    if enable_qrz:
        threads.append(("qrz", _qrz_loop))
    if enable_pollers:
        threads += [("solar", _solar_loop), ("pota", _pota_loop),
                    ("iss", _iss_loop), ("dx", _dx_loop)]
    if enable_ingest_watchdog:
        threads.append(("ingest-wd", _ingest_watchdog))

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
