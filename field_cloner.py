#!/usr/bin/env python3
"""
W4GGJ Field Cloner  —  laptop / POTA edition
============================================
Relays your WSJT-X UDP stream (freq, decodes, AND the logged-QSO packet) from a
portable site back to the home shack, so contacts you make in the field land in
the home logbook and on Mission Control.

    WSJT-X  ->  GridTracker  ->  (this script, :2235)  ->  home shack :2234

Why this instead of GridTracker's own forward: GridTracker's "Forward UDP
Messages" relays decodes/status but tends NOT to relay the "QSO Logged" packet,
so your contacts never reach home. This script forwards EVERY packet verbatim,
so the logged QSO gets home too.

SETUP
-----
1. Edit HOME_TARGETS below with your home address (see notes there).
2. Run it:            python field_cloner.py
3. In GridTracker, set "Forward UDP Messages" to  127.0.0.1 : 2235
   (or point WSJT-X's UDP Server straight here if you skip GridTracker).
4. Make a contact and log it in WSJT-X — you'll see
       [cloner] >>> QSO logged -> forwarded to home <<<
   here, and  [wsjtx] QSO logged: ...  on the home agent console.

Stdlib only. Ctrl-C to stop.
"""

import socket
import struct
import time

# ── CONFIG ────────────────────────────────────────────────────────────────────
LISTEN_PORT = 2235          # GridTracker (or WSJT-X) forwards its UDP here

HOME_TARGETS = [
    # Home shack — pick ONE addressing method and put it here:
    #
    #   Tailscale (recommended): the home PC's Tailscale IP. It never changes and
    #   works from any network with no port-forwarding. Get it by running
    #   `tailscale ip -4` on the HOME PC. Example:
    ("100.100.100.100", 2234),          # <-- REPLACE with your home Tailscale IP
    #
    #   eero DDNS (public-internet route, needs the home port-forward + firewall):
    # ("m9252594.eero.online", 2234),
    #
    #   You can also add LOCAL apps on this laptop to feed them too, e.g.:
    # ("127.0.0.1", 2237),              # a second local logger, etc.
]
# ──────────────────────────────────────────────────────────────────────────────

WSJTX_MAGIC = 0xADBCCBDA
TYPE_QSO_LOGGED = 5


def wsjtx_msg_type(data):
    """Return the WSJT-X message type, or None if it isn't a WSJT-X packet."""
    if len(data) < 12:
        return None
    magic, _schema, mtype = struct.unpack(">III", data[:12])
    if magic != WSJTX_MAGIC:
        return None
    return mtype


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("0.0.0.0", LISTEN_PORT))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("=" * 62)
    print("  W4GGJ Field Cloner  —  forwarding WSJT-X UDP to the home shack")
    print(f"  Point GridTracker's Forward UDP (or WSJT-X) at  127.0.0.1:{LISTEN_PORT}")
    for host, port in HOME_TARGETS:
        print(f"  -> home target: {host}:{port}")
    print("  (leave this window running)")
    print("=" * 62)

    count = 0
    while True:
        try:
            data, _ = rx.recvfrom(65535)
        except OSError as e:
            print(f"[cloner] recv error ({e}) — retry in 2s")
            time.sleep(2)
            continue

        for host, port in HOME_TARGETS:
            try:
                tx.sendto(data, (host, port))
            except OSError as e:
                # host may be a name that isn't resolving yet, or Tailscale down —
                # report and keep going; the next packet retries.
                print(f"[cloner] send to {host}:{port} failed ({e})")

        count += 1
        if wsjtx_msg_type(data) == TYPE_QSO_LOGGED:
            print("[cloner] >>> QSO logged -> forwarded to home <<<")
        if count % 100 == 0:
            print(f"[cloner] {count} packets forwarded")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[cloner] stopped")
