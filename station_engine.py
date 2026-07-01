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

import json
import math
import os
import re
import socket
import struct
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "W4GGJ-MissionControl/1.0"}


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
    },
    # live radio (WSJT-X / rigctld)
    "radio": {
        "online": False,
        "source": "—",          # "WSJT-X" | "rigctld" | "—"
        "dial_hz": 0,
        "rx_hz": 0,             # dial + rx audio offset
        "freq_mhz": 0.0,
        "band": "—",
        "mode": "—",
        "tx": False,
        "decoding": False,
        "dx_call": "",
        "report": "",
        "last_seen": 0,
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
            _set("radio", {
                "online": True, "source": "WSJT-X", "dial_hz": dial,
                "rx_hz": dial + rx_df, "freq_mhz": round(dial / 1e6, 6),
                "band": freq_to_band(dial), "mode": mode or "—",
                "tx": transmitting, "decoding": decoding,
                "dx_call": dx_call, "report": report,
                "last_seen": time.time(),
            })

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
                STATE["log"]["last_qso_ts"] = time.time()
                _session_qsos.append(qso)
                if not _file_log_active:
                    _rebuild_log_from_session()
            print(f"[wsjtx] QSO logged: {dx_call} {qso['band']} {mode}")
    except Exception:
        pass  # malformed / partial datagram — ignore


# ══════════════════════════════════════════════════════════════════════════════
#  Hamlib rigctld poller (optional — for SSB/CW freq when not on FT8)
# ══════════════════════════════════════════════════════════════════════════════
def _rigctld_cmd(sock, cmd):
    sock.sendall((cmd + "\n").encode())
    return sock.recv(1024).decode("utf-8", "replace").strip()


def _rigctld_loop():
    host = cfg("rigctld_host", "127.0.0.1")
    port = int(cfg("rigctld_port", 4532))
    while True:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.settimeout(3)
                print(f"[rigctld] connected {host}:{port}")
                while True:
                    freq = _rigctld_cmd(sock, "f")
                    mode = _rigctld_cmd(sock, "m").splitlines()[0] if True else ""
                    hz = int(freq) if freq.isdigit() else 0
                    with _lock:
                        # WSJT-X wins when it's actively feeding us
                        if STATE["radio"]["source"] != "WSJT-X" or \
                           time.time() - STATE["radio"]["last_seen"] > 10:
                            STATE["radio"].update({
                                "online": True, "source": "rigctld",
                                "dial_hz": hz, "rx_hz": hz,
                                "freq_mhz": round(hz / 1e6, 6),
                                "band": freq_to_band(hz), "mode": mode or "—",
                                "last_seen": time.time(),
                            })
                    time.sleep(0.5)
        except Exception:
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


def _recompute_log(path):
    global _file_log_active
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"[adif] read failed: {e}")
        return
    recs = parse_adif(text)
    my_grid = cfg("grid", "")
    my_ll = grid_to_latlon(my_grid) if my_grid else None

    calls, bands, modes, grids, countries = set(), set(), set(), set(), set()
    band_bd, mode_bd = {}, {}
    best = None
    recent = []
    for r in recs:
        call = (r.get("call") or "").upper()
        band = (r.get("band") or "").lower()
        mode = (r.get("mode") or "").upper()
        grid = (r.get("gridsquare") or "").upper()
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
        ctry = callsign_country(call)
        if ctry:
            countries.add(ctry)
        if my_ll and grid:
            km = haversine_km(my_ll, grid_to_latlon(grid))
            if km and (best is None or km > best["km"]):
                best = {"call": call, "km": round(km), "country": ctry or "", "grid": grid[:6]}

    for r in recs[-15:][::-1]:
        recent.append({
            "date": r.get("qso_date", ""), "time": r.get("time_on", "")[:4],
            "call": (r.get("call") or "").upper(),
            "band": (r.get("band") or "").lower(),
            "freq": r.get("freq", ""),
            "mode": (r.get("mode") or "").upper(),
            "rst_s": r.get("rst_sent", ""), "rst_r": r.get("rst_rcvd", ""),
            "country": callsign_country((r.get("call") or "").upper()) or "",
        })

    with _lock:
        prev_total = STATE["log"]["total"]
        STATE["log"].update({
            "total": len(recs), "unique_calls": len(calls), "bands": len(bands),
            "modes": len(modes), "countries": len(countries), "grids": len(grids),
            "best_dx": best, "recent": recent,
            "band_breakdown": band_bd, "mode_breakdown": mode_bd,
            "adif_path": str(path),
        })
        if len(recs) > prev_total and prev_total > 0:
            STATE["log"]["last_qso_ts"] = time.time()
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


def _dx_loop():
    interval = int(cfg("poll_dx_sec", 120))
    while True:
        try:
            spots = json.loads(_get("https://www.dxsummit.fi/api/v1/spots?limit=20"))
            out = []
            for s in spots[:16]:
                fq = float(s.get("frequency", 0) or 0)
                out.append({
                    "dx": s.get("dx_call", ""), "spotter": s.get("spotter_call", ""),
                    "freq": round(fq / 1000, 3), "band": freq_to_band(fq * 1000),
                    "comment": (s.get("comment", "") or "")[:40],
                })
            with _lock:
                STATE["dx"] = out
        except Exception as e:
            print(f"[dx] {e}")
        time.sleep(interval)


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


def start_engine(enable_wsjtx=None, enable_adif=True, enable_rigctld=None,
                 enable_pollers=True, enable_ingest_watchdog=False):
    STATE["engine_started"] = time.time()
    if enable_wsjtx is None:
        enable_wsjtx = cfg("wsjtx_enabled", True)
    if enable_rigctld is None:
        enable_rigctld = cfg("rigctld_enabled", False)

    threads = []
    if enable_wsjtx:
        threads.append(("wsjtx", _wsjtx_loop))
    if enable_rigctld:
        threads.append(("rigctld", _rigctld_loop))
    if enable_adif:
        threads.append(("adif", _adif_loop))
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
