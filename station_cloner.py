"""
W4GGJ Mission Control — UDP Cloner + Web Logger Bridge (folded into the agent)
=============================================================================
This is the standalone "udp_cloner_with_adif.py" refactored into a config-driven
module so it can run inside the same process as the dashboard home agent.

Why it exists: WSJT-X can only send its UDP stream to ONE destination, but two
things want it — the cloner (fan-out to your other ham apps) and the dashboard
engine (live radio/decodes for the public site + shack display). The fix is to
let WSJT-X feed the cloner, and have the cloner clone a verbatim copy to the
dashboard engine too. So point WSJT-X at the cloner's local port (2235); the
engine keeps binding 2242 and receives a relayed copy — no engine changes.

What it does (unchanged from the standalone script):
  * Listens on the local WSJT-X port (default 2235) and a remote/field port
    (default 2234) and clones every packet to your fan-out targets.
  * Web logger bridge on HTTP :12061 — builds a real WSJT-X Type-5 (QSO Logged)
    packet from web-logger JSON and injects it into the same pipeline, plus a
    QRZ callsign-lookup/auth proxy so the web logger needs no CORS proxy.
  * Auto park-reference watcher (reads the newest field-notes.md "POTA Ref:").

Differences from the standalone version:
  * All settings (ports, targets, callsign, grid, paths) come from config —
    nothing is hardcoded.
  * The dashboard engine's WSJT-X port (wsjtx_udp_port) is auto-added to the
    fan-out targets, so enabling the cloner also feeds the dashboard.
  * ADIF-to-file writing is OPTIONAL: it only happens when cloner_adif_path is
    set. Left blank (the default) nothing is written to disk — QSOs are still
    forwarded to every target (including the dashboard) and QRZ remains the
    logbook source of truth.
  * The park-ref watcher only runs when cloner_blog_folder is set.

Standard library only. Threads are daemons; call start(cfg) and let the caller
(station_agent) own the main loop.
"""

import datetime
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─────────────────────────────────────────────
#  RUNTIME CONFIG (populated by start())
# ─────────────────────────────────────────────

PORT_LOCAL = 2235
PORT_REMOTE = 2234
PORT_BRIDGE = 12061
TARGETS = []                # list of (ip, port) — engine port auto-appended
ADIF_OUTPUT_PATH = ""       # "" -> no file write
BLOG_FOLDER = ""            # "" -> park-ref watcher disabled
SCAN_INTERVAL = 30
MY_CALLSIGN = "W4GGJ"
MY_GRID = "EL87"

WSJTX_MAGIC = 0xADBCCBDA
TYPE_QSO_LOGGED = 5
TYPE_LOGGED_ADIF = 15

_lock = threading.Lock()
_park_ref = ""

send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
cnt = {"local": 0, "remote": 0, "adif": 0, "web": 0}


# ─────────────────────────────────────────────
#  AUTO PARK REFERENCE — reads field-notes.md
# ─────────────────────────────────────────────

def get_park_ref():
    with _lock:
        return _park_ref


def set_park_ref(ref):
    global _park_ref
    with _lock:
        _park_ref = ref.strip()


def find_latest_field_notes(blog_folder):
    latest_path, latest_mtime = None, 0
    if not blog_folder or not os.path.isdir(blog_folder):
        return None
    for entry in os.scandir(blog_folder):
        if not entry.is_dir():
            continue
        notes_path = os.path.join(entry.path, "field-notes.md")
        if os.path.isfile(notes_path):
            mtime = os.path.getmtime(notes_path)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = notes_path
    return latest_path


def parse_park_ref_from_notes(notes_path):
    try:
        with open(notes_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.lower().startswith("pota ref:"):
                    return stripped.split(":", 1)[1].strip()
        return ""
    except Exception as e:
        print(f"[cloner] error reading {notes_path}: {e}")
        return ""


def park_ref_watcher():
    last_ref, last_path = None, None
    while True:
        notes_path = find_latest_field_notes(BLOG_FOLDER)
        if notes_path:
            ref = parse_park_ref_from_notes(notes_path)
            if notes_path != last_path or ref != last_ref:
                set_park_ref(ref)
                folder_name = os.path.basename(os.path.dirname(notes_path))
                if ref:
                    print(f"[cloner] park ref -> {ref}  (from {folder_name}/field-notes.md)")
                else:
                    print(f"[cloner] no park ref in {folder_name}/field-notes.md — general logging")
                last_ref, last_path = ref, notes_path
        else:
            if last_path is not None:
                print("[cloner] no field-notes.md found — general logging")
                set_park_ref("")
                last_ref = last_path = None
        time.sleep(SCAN_INTERVAL)


# ─────────────────────────────────────────────
#  ADIF FILE HELPERS (only used when ADIF_OUTPUT_PATH is set)
# ─────────────────────────────────────────────

def ensure_adif_header(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write("W4GGJ Live Activation Log\n")
            f.write("Generated by W4GGJ Mission Control cloner\n")
            f.write("<ADIF_VER:5>3.1.0\n")
            f.write("<PROGRAMID:12>UDP-Cloner\n")
            f.write("<EOH>\n\n")
        print(f"[cloner] created ADIF: {path}")


def adif_field(tag, val):
    v = str(val).strip() if val is not None else ""
    return f"<{tag}:{len(v)}>{v}" if v else ""


def append_qso(path, fields):
    ensure_adif_header(path)
    parts = [p for p in (adif_field(t, v) for t, v in fields.items()) if p]
    parts.append("<EOR>")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n\n")
    park = f" [{fields.get('MY_SIG_INFO', '')}]" if fields.get("MY_SIG_INFO") else ""
    print(f"[cloner] ADIF logged -> {fields.get('CALL','?')} {fields.get('BAND','?')} "
          f"{fields.get('MODE','?')}{park}")


# ─────────────────────────────────────────────
#  WSJT-X PACKET PARSING
# ─────────────────────────────────────────────

class PR:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def read(self, n):
        c = self.d[self.p:self.p + n]
        self.p += n
        return c

    def u32(self):
        return struct.unpack(">I", self.read(4))[0]

    def u64(self):
        return struct.unpack(">Q", self.read(8))[0]

    def utf8(self):
        n = self.u32()
        return "" if n == 0xFFFFFFFF else self.read(n).decode("utf-8", errors="replace")

    def dt(self):
        jd = self.u64()
        ms = self.u32()
        self.read(1)
        l = int(jd) + 68569
        n = (4 * l) // 146097
        l -= (146097 * n + 3) // 4
        i = (4000 * (l + 1)) // 1461001
        l -= (1461 * i) // 4 - 31
        j = (80 * l) // 2447
        day = l - (2447 * j) // 80
        l = j // 11
        mon = j + 2 - 12 * l
        yr = 100 * (n - 49) + i + l
        s = ms // 1000
        try:
            return datetime.datetime(yr, mon, day, s // 3600, (s % 3600) // 60, s % 60)
        except Exception:
            return datetime.datetime.utcnow()


def freq_to_band(mhz):
    for lo, hi, b in [
        (1.8, 2.0, "160M"), (3.5, 4.0, "80M"), (5.3, 5.4, "60M"),
        (7.0, 7.3, "40M"), (10.1, 10.15, "30M"), (14.0, 14.35, "20M"),
        (18.0, 18.17, "17M"), (21.0, 21.45, "15M"), (24.8, 24.99, "12M"),
        (28.0, 29.7, "10M"), (50.0, 54.0, "6M"),
    ]:
        if lo <= mhz <= hi:
            return b
    return f"{mhz:.4f}MHz"


def get_msg_type(data):
    # WSJT-X header is magic(4) + schema(4) + type(4) = 12 bytes, all uint32.
    if len(data) < 12:
        return None
    magic, _, t = struct.unpack(">III", data[:12])
    return t if magic == WSJTX_MAGIC else None


def parse_type5(data):
    try:
        r = PR(data)
        r.u32(); r.u32(); r.u32(); r.utf8()
        t_off = r.dt()
        call = r.utf8()
        grid = r.utf8()
        hz = r.u64()
        mode = r.utf8()
        rs = r.utf8()
        rr = r.utf8()
        pwr = r.utf8()
        cmt = r.utf8()
        name = r.utf8()
        t_on = r.dt()
        mhz = hz / 1_000_000
        fields = {
            "CALL": call,
            "QSO_DATE": t_on.strftime("%Y%m%d"),
            "TIME_ON": t_on.strftime("%H%M%S"),
            "QSO_DATE_OFF": t_off.strftime("%Y%m%d"),
            "TIME_OFF": t_off.strftime("%H%M%S"),
            "BAND": freq_to_band(mhz),
            "FREQ": f"{mhz:.4f}",
            "MODE": mode,
            "RST_SENT": rs,
            "RST_RCVD": rr,
            "TX_PWR": pwr,
            "GRIDSQUARE": grid,
            "NAME": name,
            "COMMENT": cmt,
            "STATION_CALLSIGN": MY_CALLSIGN,
            "OPERATOR": MY_CALLSIGN,
        }
        park = get_park_ref()
        if park:
            fields["MY_SIG"] = "POTA"
            fields["MY_SIG_INFO"] = park
        return fields
    except Exception as e:
        print(f"[cloner] Type-5 parse error: {e}")
        return None


def parse_type15(data):
    try:
        r = PR(data)
        r.u32(); r.u32(); r.u32(); r.utf8()
        return r.utf8()
    except Exception as e:
        print(f"[cloner] Type-15 parse error: {e}")
        return None


# ─────────────────────────────────────────────
#  FORWARDING + ADIF HANDLER
# ─────────────────────────────────────────────

def forward(data):
    for ip, port in TARGETS:
        send_sock.sendto(data, (ip, port))


def handle_adif(data):
    if not ADIF_OUTPUT_PATH:
        return
    t = get_msg_type(data)
    if t == TYPE_QSO_LOGGED:
        fields = parse_type5(data)
        if fields and fields.get("CALL"):
            append_qso(ADIF_OUTPUT_PATH, fields)
            cnt["adif"] += 1
    elif t == TYPE_LOGGED_ADIF:
        raw = parse_type15(data)
        if raw:
            ensure_adif_header(ADIF_OUTPUT_PATH)
            with open(ADIF_OUTPUT_PATH, "a", encoding="utf-8") as f:
                f.write(raw.strip() + "\n<EOR>\n\n")
            cnt["adif"] += 1
            print("[cloner] ADIF logged raw (type 15)")


def listen_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT_LOCAL))
    print(f"[cloner] local UDP {PORT_LOCAL} — forward only "
          f"(point WSJT-X here; relays to {len(TARGETS)} targets incl. dashboard)")
    while True:
        data, _ = s.recvfrom(65535)
        forward(data)
        cnt["local"] += 1
        if cnt["local"] % 1000 == 0:
            print(f"[cloner] local {cnt['local']} packets forwarded")


def listen_remote():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT_REMOTE))
    tail = "forward + ADIF write" if ADIF_OUTPUT_PATH else "forward only"
    print(f"[cloner] remote UDP {PORT_REMOTE} — {tail}")
    while True:
        data, _ = s.recvfrom(65535)
        forward(data)
        handle_adif(data)
        cnt["remote"] += 1
        if cnt["remote"] % 1000 == 0:
            print(f"[cloner] remote {cnt['remote']} packets | {cnt['adif']} QSOs logged")


# ─────────────────────────────────────────────
#  WSJT-X PACKET BUILDER  (for web bridge)
# ─────────────────────────────────────────────

def _pu32(v):
    return struct.pack(">I", v)


def _pu64(v):
    return struct.pack(">Q", v)


def _putf8(s):
    """WSJT-X length-prefixed UTF-8 string. Empty/None -> null (0xFFFFFFFF)."""
    if not s:
        return _pu32(0xFFFFFFFF)
    enc = s.encode("utf-8")
    return _pu32(len(enc)) + enc


def _pdt(dt):
    """Qt QDateTime on-wire: qint64 Julian day, quint32 ms since midnight, quint8 timespec=UTC."""
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3
    jd = (dt.day + (153 * m + 2) // 5 + 365 * y
          + y // 4 - y // 100 + y // 400 - 32045)
    ms = (dt.hour * 3600 + dt.minute * 60 + dt.second) * 1000
    return struct.pack(">qIB", jd, ms, 1)


def build_wsjtx_type5(entry):
    """Build a WSJT-X Message Type 5 — QSO Logged — from web-logger JSON."""
    try:
        dt_on = datetime.datetime.strptime(
            f"{entry.get('date', '2000-01-01')} {entry.get('time', '00:00')}",
            "%Y-%m-%d %H:%M")
    except Exception:
        dt_on = datetime.datetime.utcnow()
    dt_off = dt_on

    try:
        freq_hz = int(float(entry.get("freq", 0)) * 1_000_000)
    except (ValueError, TypeError):
        freq_hz = 0

    prog_parts = []
    for pid, pdata in (entry.get("programs") or {}).items():
        if pdata and pdata.get("ref"):
            role_char = "A" if pdata.get("role", "") == "activator" else "H"
            prog_parts.append(f"{pid.upper()}:{pdata['ref']}({role_char})")
    comment = " ".join(prog_parts)
    if entry.get("notes"):
        comment = (comment + " " + entry["notes"]).strip() if comment else entry["notes"]

    my_grid = entry.get("my_grid") or MY_GRID

    buf = _pu32(WSJTX_MAGIC)
    buf += _pu32(2)                               # schema version
    buf += _pu32(TYPE_QSO_LOGGED)                 # message type 5
    buf += _putf8("W4GGJ-WebLogger")              # client ID
    buf += _pdt(dt_off)                           # DateTimeOff
    buf += _putf8(entry.get("call", ""))          # DXCall
    buf += _putf8(entry.get("their_grid", ""))    # DXGrid
    buf += _pu64(freq_hz)                         # TxFreq (Hz)
    buf += _putf8(entry.get("mode", ""))          # Mode
    buf += _putf8(entry.get("rst_s", "59"))       # ReportSent
    buf += _putf8(entry.get("rst_r", "59"))       # ReportRcvd
    buf += _putf8("")                             # TxPower
    buf += _putf8(comment)                        # Comments
    buf += _putf8(entry.get("name", ""))          # Name
    buf += _pdt(dt_on)                            # DateTimeOn
    buf += _putf8(MY_CALLSIGN)                    # OperatorCall
    buf += _putf8(MY_CALLSIGN)                    # MyCall
    buf += _putf8(my_grid)                        # MyGrid
    buf += _putf8("")                             # ExchangeSent
    buf += _putf8("")                             # ExchangeRcvd
    buf += _putf8("")                             # ADIFPropMode
    return bytes(buf)


# ─────────────────────────────────────────────
#  PROGRAM -> ADIF FIELD MAPPING
# ─────────────────────────────────────────────

# (activator_field, hunter_field, sig_name or None)
PROG_ADIF = {
    "pota": ("MY_POTA_REF", "POTA_REF", "POTA"),
    "wwff": ("MY_WWFF_REF", "WWFF_REF", "WWFF"),
    "sota": ("MY_SOTA_REF", "SOTA_REF", "SOTA"),
    "iota": ("MY_IOTA", "IOTA", None),
    "bota": ("MY_SIG_INFO", "SIG_INFO", "BOTA"),
    "gma": ("MY_SIG_INFO", "SIG_INFO", "GMA"),
    "lota": ("MY_SIG_INFO", "SIG_INFO", "LOTA"),
    "wca": ("MY_WCA_REF", "WCA_REF", None),
}


def build_adif_fields_from_web(entry):
    """Build the ADIF fields dict from a web-logger JSON entry (all 8 programs)."""
    try:
        dt_on = datetime.datetime.strptime(
            f"{entry.get('date', '2000-01-01')} {entry.get('time', '00:00')}",
            "%Y-%m-%d %H:%M")
    except Exception:
        dt_on = datetime.datetime.utcnow()

    try:
        mhz = float(entry.get("freq", 0))
    except (ValueError, TypeError):
        mhz = 0.0

    band = entry.get("band") or freq_to_band(mhz)

    fields = {
        "CALL": entry.get("call", ""),
        "QSO_DATE": dt_on.strftime("%Y%m%d"),
        "TIME_ON": dt_on.strftime("%H%M%S"),
        "QSO_DATE_OFF": dt_on.strftime("%Y%m%d"),
        "TIME_OFF": dt_on.strftime("%H%M%S"),
        "BAND": band,
        "FREQ": f"{mhz:.4f}" if mhz else "",
        "MODE": entry.get("mode", ""),
        "RST_SENT": entry.get("rst_s", "59"),
        "RST_RCVD": entry.get("rst_r", "59"),
        "NAME": entry.get("name", ""),
        "GRIDSQUARE": entry.get("their_grid", ""),
        "MY_GRIDSQUARE": entry.get("my_grid") or MY_GRID,
        "COMMENT": entry.get("notes", ""),
        "STATION_CALLSIGN": MY_CALLSIGN,
        "OPERATOR": MY_CALLSIGN,
    }

    programs = entry.get("programs") or {}
    primary_sig = None

    for pid, pdata in programs.items():
        if not pdata or not pdata.get("ref"):
            continue
        ref = pdata["ref"].upper()
        role = pdata.get("role", "activator")
        prog = PROG_ADIF.get(pid.lower())
        if not prog:
            continue

        act_field, hnt_field, sig_name = prog
        adif_field_name = act_field if role == "activator" else hnt_field

        if adif_field_name == "MY_SIG_INFO":
            if primary_sig is None and role == "activator":
                fields["MY_SIG"] = sig_name or pid.upper()
                fields["MY_SIG_INFO"] = ref
                primary_sig = pid
        else:
            fields[adif_field_name] = ref

        if sig_name and role == "activator" and adif_field_name != "MY_SIG_INFO":
            if "MY_SIG" not in fields:
                fields["MY_SIG"] = sig_name
                fields["MY_SIG_INFO"] = ref

    if "MY_SIG" not in fields:
        park = get_park_ref()
        if park:
            fields["MY_SIG"] = "POTA"
            fields["MY_SIG_INFO"] = park

    return fields


# ─────────────────────────────────────────────
#  WEB LOGGER BRIDGE — HTTP handler
# ─────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):
    """Receives JSON QSOs from the web logger, builds a WSJT-X packet, and
    injects it into the pipeline. Also proxies QRZ callsign lookups."""

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path in ("", "/health"):
            body = json.dumps({
                "status": "ok",
                "bridge": "W4GGJ Mission Control cloner bridge",
                "targets": [f"{ip}:{p}" for ip, p in TARGETS],
                "park_ref": get_park_ref() or "(none)",
                "adif_logged": cnt["adif"],
                "web_logged": cnt["web"],
            }).encode()
            self._respond(200, None, body)

        elif path == "/qrz/lookup":
            session = (params.get("session", [""])[0]).strip()
            call = (params.get("call", [""])[0]).strip().upper()
            if not session or not call:
                self._respond(400, {"error": "session and call required"})
                return
            try:
                import re
                import urllib.parse
                import urllib.request
                qrz_url = (
                    "https://xmldata.qrz.com/xml/current/"
                    f"?s={urllib.parse.quote(session)}"
                    f"&callsign={urllib.parse.quote(call)}")
                with urllib.request.urlopen(qrz_url, timeout=10) as r:
                    xml = r.read().decode("utf-8", errors="replace")

                def get_field(tag):
                    m = re.search(fr"<{tag}>([^<]*)</{tag}>", xml, re.I)
                    return m.group(1).strip() if m else ""

                fname = get_field("fname")
                lname = get_field("name")
                grid = get_field("grid")
                city = get_field("addr2")
                state = get_field("state")
                country = get_field("country")
                dxcc = get_field("dxcc")

                if fname or lname or grid:
                    result = {
                        "call": call, "fname": fname, "lname": lname,
                        "name": f"{fname} {lname}".strip(),
                        "grid": grid[:6].upper() if grid else "",
                        "city": city, "state": state,
                        "country": country, "dxcc": dxcc,
                    }
                    print(f"[cloner] QRZ lookup {call} -> {result['name']} {grid}")
                    self._respond(200, result)
                else:
                    err_m = re.search(r"<Error>([^<]+)</Error>", xml)
                    err = err_m.group(1) if err_m else "Not found"
                    if "session" in err.lower() or "timeout" in err.lower():
                        self._respond(401, {"error": err, "session_expired": True})
                    else:
                        self._respond(404, {"error": err})
            except Exception as exc:
                print(f"[cloner] QRZ lookup error for {call}: {exc}")
                self._respond(500, {"error": str(exc)})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip("/")

        if path not in ("/log", "/qrz/auth"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._respond(400, {"error": str(exc)})
            return

        if path == "/qrz/auth":
            user = (data.get("user") or "").strip()
            pw = (data.get("pass") or "").strip()
            if not user or not pw:
                missing = [f for f, v in (("user", user), ("pass", pw)) if not v]
                print(f"[cloner] QRZ auth missing fields: {', '.join(missing)}")
                self._respond(400, {"error": "user and pass required"})
                return
            try:
                import re
                import urllib.parse
                import urllib.request
                qrz_url = (
                    "https://xmldata.qrz.com/xml/current/"
                    f"?username={urllib.parse.quote(user)}"
                    f"&password={urllib.parse.quote(pw)}"
                    f"&agent=W4GGJ-Bridge")
                with urllib.request.urlopen(qrz_url, timeout=10) as r:
                    xml = r.read().decode("utf-8", errors="replace")
                key_m = re.search(r"<Key>([^<]+)</Key>", xml)
                if key_m:
                    sub_m = re.search(r"<SubExp>([^<]+)</SubExp>", xml)
                    sub = sub_m.group(1) if sub_m else ""
                    print(f"[cloner] QRZ session created for {user} (sub: {sub or 'unknown'})")
                    self._respond(200, {"session": key_m.group(1), "sub": sub})
                else:
                    err_m = re.search(r"<Error>([^<]+)</Error>", xml)
                    err = err_m.group(1) if err_m else "No session key returned"
                    print(f"[cloner] QRZ auth failed for {user}: {err}")
                    self._respond(401, {"error": err})
            except Exception as exc:
                print(f"[cloner] QRZ auth error: {exc}")
                self._respond(500, {"error": str(exc)})
            return

        # /log
        call = data.get("call", "?").upper()
        try:
            pkt = build_wsjtx_type5(data)
            forward(pkt)

            if ADIF_OUTPUT_PATH:
                fields = build_adif_fields_from_web(data)
                if fields.get("CALL"):
                    append_qso(ADIF_OUTPUT_PATH, fields)
                    cnt["adif"] += 1

            cnt["web"] += 1
            ts = f"{data.get('date', '')} {data.get('time', '')}"
            print(f"[cloner] web {ts}  {call:12s}  {data.get('band', ''):5s}  "
                  f"{data.get('mode', ''):6s}  -> {len(TARGETS)} targets  "
                  f"({len(pkt)}B)  [web #{cnt['web']}]")
            self._respond(200, {"ok": True, "bytes": len(pkt), "targets": len(TARGETS)})
        except Exception as exc:
            print(f"[cloner] web error processing {call}: {exc}")
            self._respond(500, {"error": str(exc)})

    def _respond(self, code, data, raw_body=None):
        body = raw_body if raw_body is not None else json.dumps(data).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def listen_web_bridge():
    server = HTTPServer(("0.0.0.0", PORT_BRIDGE), BridgeHandler)
    print(f"[cloner] web bridge HTTP :{PORT_BRIDGE} — web logger + QRZ proxy active")
    server.serve_forever()


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def start(cfg):
    """Configure from a settings dict and launch the cloner's daemon threads.

    Recognized cfg keys (all optional except that ports must not clash):
      cloner_local_port, cloner_remote_port, cloner_bridge_port,
      cloner_targets      -> list of [ip, port]
      engine_udp_port     -> dashboard engine's WSJT-X port (auto-added as a target)
      cloner_adif_path    -> "" disables file writing
      cloner_blog_folder  -> "" disables the park-ref watcher
      cloner_scan_sec, callsign, grid

    Returns the list of started threads (daemons). The caller owns the main loop.
    """
    global PORT_LOCAL, PORT_REMOTE, PORT_BRIDGE, TARGETS
    global ADIF_OUTPUT_PATH, BLOG_FOLDER, SCAN_INTERVAL, MY_CALLSIGN, MY_GRID

    PORT_LOCAL = int(cfg.get("cloner_local_port", 2235))
    PORT_REMOTE = int(cfg.get("cloner_remote_port", 2234))
    PORT_BRIDGE = int(cfg.get("cloner_bridge_port", 12061))
    SCAN_INTERVAL = int(cfg.get("cloner_scan_sec", 30))
    ADIF_OUTPUT_PATH = (cfg.get("cloner_adif_path") or "").strip()
    BLOG_FOLDER = (cfg.get("cloner_blog_folder") or "").strip()
    MY_CALLSIGN = (cfg.get("callsign") or "W4GGJ").strip().upper()
    MY_GRID = (cfg.get("grid") or "EL87").strip()

    # Build the fan-out target list, then append the dashboard engine's WSJT-X
    # port so enabling the cloner also feeds the local dashboard. De-dupe.
    targets = []
    for t in (cfg.get("cloner_targets") or []):
        try:
            ip, port = t[0], int(t[1])
            targets.append((ip, port))
        except Exception:
            print(f"[cloner] ignoring malformed target: {t!r}")
    engine_port = int(cfg.get("engine_udp_port", 2242))
    engine_target = ("127.0.0.1", engine_port)
    if engine_target not in targets:
        targets.append(engine_target)
    TARGETS = targets

    print("[cloner] combined cloner starting")
    print(f"[cloner]   WSJT-X should point at this PC UDP {PORT_LOCAL}")
    print(f"[cloner]   fan-out targets: " +
          ", ".join(f"{ip}:{p}" for ip, p in TARGETS) +
          f"  (dashboard engine = 127.0.0.1:{engine_port})")
    print(f"[cloner]   ADIF file: {ADIF_OUTPUT_PATH or '(disabled — QRZ is the logbook)'}")
    print(f"[cloner]   park-ref watcher: {BLOG_FOLDER or '(disabled)'}")

    threads = [
        (listen_local, "ClonerLocal"),
        (listen_remote, "ClonerRemote"),
        (listen_web_bridge, "ClonerWebBridge"),
    ]
    if BLOG_FOLDER:
        # seed the current park ref before the watcher loop begins
        notes = find_latest_field_notes(BLOG_FOLDER)
        if notes:
            set_park_ref(parse_park_ref_from_notes(notes))
        threads.append((park_ref_watcher, "ClonerParkWatcher"))

    started = []
    for fn, name in threads:
        th = threading.Thread(target=fn, daemon=True, name=name)
        th.start()
        started.append(th)
    return started
