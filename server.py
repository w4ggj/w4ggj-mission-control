"""
W4GGJ Mission Control — Web Server
==================================
Runs in two roles:

  * LOCAL  (default, on your LAN dashboard PC) — full engine: WSJT-X UDP + ADIF +
           public pollers. Serves the UI + /api/state. HTTPS if TavaOne certs exist.

  * CLOUD  (Render) — set env ROLE=cloud. Runs only the public pollers and accepts
           live telemetry from the home agent at POST /api/ingest (token-auth),
           then serves it publicly at /api/state. Binds 0.0.0.0:$PORT (Render).

Pure stdlib http.server. Run:  python server.py
"""

import json
import os
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import station_engine as engine

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

ROLE = os.environ.get("ROLE", "local").lower()
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
# Gate for the private visitor-analytics view. Falls back to the ingest token so
# no new secret is needed; set ANALYTICS_KEY to use a separate one. On the LAN
# (local role) the analytics API is open — it's already behind your firewall.
ANALYTICS_KEY = os.environ.get("ANALYTICS_KEY", "") or INGEST_TOKEN

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        self.wfile.write(body)

    def _client_ip(self):
        # Render (and most proxies) put the real client first in X-Forwarded-For.
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.headers.get("X-Real-IP", "") or self.client_address[0]

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/analytics":
            # Private visitor stats. Token-gated on the cloud; open on the LAN.
            if ANALYTICS_KEY:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                key = (q.get("key") or [""])[0] or self.headers.get("X-Analytics-Key", "")
                if key != ANALYTICS_KEY:
                    self._send(401, json.dumps({"error": "bad key"}),
                               "application/json; charset=utf-8")
                    return
            self._send(200, json.dumps(engine.analytics_summary()),
                       "application/json; charset=utf-8")
            return

        if path == "/api/state":
            self._send(200, json.dumps(engine.snapshot()),
                       "application/json; charset=utf-8")
            return
        if path == "/api/settings":
            self._send(200, json.dumps(engine.get_settings()),
                       "application/json; charset=utf-8")
            return
        if path == "/api/health":
            health = {"ok": True, "role": ROLE}
            if ROLE == "cloud":
                # Surface agent-link freshness so "site not updating" is diagnosable
                # from the public URL: age_sec null/large => no telemetry arriving.
                health["ingest"] = engine.ingest_status()
            self._send(200, json.dumps(health),
                       "application/json; charset=utf-8")
            return
        if path == "/api/spectrum":
            # Latest SDR panadapter frame — polled fast by the /console page.
            self._send(200, json.dumps(engine.get_spectrum()),
                       "application/json; charset=utf-8")
            return

        # Clean routes: /console, /analytics, /config, /features → their .html
        route = path
        if path == "/console":
            path = "/console.html"
        elif path == "/analytics":
            path = "/analytics.html"
        elif path == "/config":
            path = "/config.html"
        elif path == "/features":
            path = "/features.html"

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB)
        except ValueError:
            self._send(403, "forbidden")
            return
        if target.is_file():
            ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            is_html = target.suffix == ".html"
            # Count real page views (HTML documents only) — never static assets or
            # the 1 Hz /api/state polls. The analytics page itself isn't counted.
            if is_html and route not in ("/analytics", "/analytics.html",
                                         "/config", "/config.html"):
                engine.record_visit(self._client_ip(), route,
                                    self.headers.get("Referer", ""),
                                    self.headers.get("User-Agent", ""),
                                    self.headers.get("Host", ""))
            self._send(200, target.read_bytes(), ctype, cache=not is_html)
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path == "/api/settings":
            # Live dashboard settings from the shack Config page. The home agent
            # (local role) is authoritative and relays settings to the cloud, so
            # writes are accepted on the LAN and rejected on the cloud (where they
            # would just be overwritten by the next ingest) with a clear hint.
            if ROLE == "cloud":
                self._send(403, json.dumps(
                    {"error": "read-only here — change settings from the shack "
                              "Config page on your home network"}),
                    "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                changed = engine.update_settings(data)
                self._send(200, json.dumps({"ok": True, "changed": changed,
                                            "settings": engine.get_settings()}),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}),
                           "application/json; charset=utf-8")
            return
        if self.path == "/api/spectrum":
            # SDR agent pushes FFT frames here. On the LAN (local role) it's
            # trusted shack traffic — no token. On the cloud it's the remote
            # waterfall relay, so require the same ingest token as /api/ingest
            # to keep randos from spamming frames at the public app.
            if ROLE == "cloud":
                token = self.headers.get("X-Ingest-Token", "")
                if not INGEST_TOKEN or token != INGEST_TOKEN:
                    self._send(401, json.dumps({"error": "bad token"}),
                               "application/json; charset=utf-8")
                    return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                engine.set_spectrum(data)
                self._send(200, json.dumps({"ok": True}),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}),
                           "application/json; charset=utf-8")
            return
        if self.path == "/api/ingest":
            # Only meaningful in cloud role; token-protected.
            token = self.headers.get("X-Ingest-Token", "")
            if not INGEST_TOKEN or token != INGEST_TOKEN:
                self._send(401, json.dumps({"error": "bad token"}),
                           "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                engine.ingest(data)
                self._send(200, json.dumps({"ok": True}),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}),
                           "application/json; charset=utf-8")
            return
        self._send(404, "not found")


def _load_cfg():
    try:
        raw = json.loads((HERE / "station.config.json").read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}


def main():
    cfg = _load_cfg()

    if ROLE == "cloud":
        # Render/cloud: public pollers + accept ingest, no local radio access.
        # Explicitly disable every home-only source — WSJT-X, ADIF, QRZ, and the
        # rig links (rigctld/HRD). Those are enabled by default in the shared
        # station.config.json for the home agent, but the cloud has no radio, so
        # leaving them on made it retry-spam "connection refused" against HRD/
        # rigctld on 127.0.0.1. Live rig telemetry reaches the cloud via the home
        # agent's ingest (the radio section), not from the cloud itself.
        engine.start_engine(enable_wsjtx=False, enable_adif=False,
                            enable_rigctld=False, enable_hrd=False, enable_qrz=False,
                            enable_commander_bridge=False,
                            enable_pollers=True, enable_ingest_watchdog=True)
        port = int(os.environ.get("PORT", cfg.get("web_port", 8770)))
        host = "0.0.0.0"
        httpd = ThreadingHTTPServer((host, port), Handler)
        if not INGEST_TOKEN:
            print("[server] WARNING: INGEST_TOKEN not set — /api/ingest will reject all posts")
        print(f"[server] CLOUD role — public dashboard on 0.0.0.0:{port} (Render terminates TLS)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[server] stopped")
        return

    # LOCAL role — full engine on the LAN
    engine.start_engine()
    port = int(cfg.get("web_port", 8770))
    host = cfg.get("bind_host", "0.0.0.0")
    httpd = ThreadingHTTPServer((host, port), Handler)

    scheme = "http"
    if cfg.get("use_https", True):
        cert = HERE.parent / "homeeye_cert.pem"
        key = HERE.parent / "homeeye_key.pem"
        if cert.exists() and key.exists():
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(str(cert), str(key))
                httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
                scheme = "https"
            except Exception as e:
                print(f"[server] HTTPS failed ({e}) — HTTP fallback")

    print(f"[server] LOCAL role on {scheme}://localhost:{port}/")
    ts_ip = cfg.get("tailscale_ip", "")
    if ts_ip:
        print(f"[server] Tailscale:  {scheme}://{ts_ip}:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped")


if __name__ == "__main__":
    main()
