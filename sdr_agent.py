"""
W4GGJ Mission Control — SDR panadapter agent
============================================
Drives an SDR as a band scope that FOLLOWS the radio. It reads the rig's live
dial frequency from the dashboard engine (GET /api/state), tunes the SDR to
match, runs an FFT, and POSTs the spectrum frame to the engine (POST
/api/spectrum) ~10x/sec. The in-shack /console page renders it as a live
spectrum + waterfall, exactly like an SDR receiver console.

Runs as its own process (like station_agent.py) so its heavy deps stay out of
the stdlib-only dashboard engine, and so it can live on whichever PC the SDR is
plugged into and reach the engine over the LAN.

Backends (config: sdr_driver):
  * "synthetic" — no hardware; generates a believable moving spectrum. Use this
    first to confirm the console + waterfall pipeline works end to end.
  * "soapy"     — any SoapySDR device (RTL-SDR, SDRplay RSP, Airspy, HackRF…).
                  Needs:  pip install numpy  + SoapySDR with the device module
                  (Windows: the PothosSDR bundle).
  * "rtlsdr"    — RTL-SDR via pyrtlsdr.  Needs:  pip install numpy pyrtlsdr

HF note: a plain RTL-SDR can't hear HF (7 MHz) on its own. Use one of:
  * an SDR that covers HF natively (RSP/Airspy HF+)  -> sdr_hf_mode "native"
  * an upconverter (e.g. Ham-It-Up, +125 MHz)        -> sdr_hf_mode "upconverter"
  * the RTL direct-sampling Q-branch mod             -> sdr_hf_mode "direct"
    (also add  direct_samp=2  to sdr_device_args)

Run:  python sdr_agent.py           (reads station.config.json)
      python sdr_agent.py --synthetic
"""

import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULTS = {
    "sdr_enabled": False,
    "sdr_engine_url": "http://127.0.0.1:8770",
    "sdr_driver": "synthetic",          # synthetic | soapy | rtlsdr
    "sdr_device_args": "driver=rtlsdr",
    "sdr_sample_rate": 1200000,
    "sdr_fft_bins": 1024,
    "sdr_fps": 10,
    "sdr_gain": "auto",                 # "auto" or a dB number
    "sdr_hf_mode": "native",            # native | upconverter | direct
    "sdr_upconverter_offset_hz": 125000000,
    "sdr_center_offset_hz": 0,          # nudge SDR center off the dial (DC-spike dodge)
    "sdr_ppm": 0,
    # remote waterfall relay — decimate + push to the cloud so /console works
    # away from home. Reuses cloud_url + ingest_token from agent.config.json.
    "sdr_relay_cloud": False,
    "sdr_relay_bins": 256,
    "sdr_relay_fps": 4,
}


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((HERE / "station.config.json").read_text(encoding="utf-8"))
        for k, v in raw.items():
            if k in DEFAULTS:
                cfg[k] = v
        if raw.get("web_port") and "sdr_engine_url" not in raw:
            cfg["sdr_engine_url"] = f"http://127.0.0.1:{raw['web_port']}"
    except Exception:
        pass
    for k in DEFAULTS:
        env = os.environ.get(k.upper())
        if env is not None:
            cfg[k] = type(DEFAULTS[k])(env) if not isinstance(DEFAULTS[k], bool) else env.lower() == "true"
    # cloud relay target reuses the home agent's credentials (gitignored /
    # env), never committed here
    cfg["cloud_url"] = ""
    cfg["ingest_token"] = ""
    try:
        a = json.loads((HERE / "agent.config.json").read_text(encoding="utf-8"))
        cfg["cloud_url"] = a.get("cloud_url", "")
        cfg["ingest_token"] = a.get("ingest_token", "")
    except Exception:
        pass
    cfg["cloud_url"] = os.environ.get("CLOUD_URL", cfg["cloud_url"]).rstrip("/")
    cfg["ingest_token"] = os.environ.get("INGEST_TOKEN", cfg["ingest_token"])
    if "--synthetic" in sys.argv:
        cfg["sdr_driver"] = "synthetic"
    return cfg


def decimate_max(bins, out_n):
    """Shrink a spectrum to out_n bins using block-max so peaks survive."""
    n = len(bins)
    if out_n >= n or out_n <= 0:
        return [round(v, 1) for v in bins]
    step = n / out_n
    out = []
    for i in range(out_n):
        a, b = int(i * step), int((i + 1) * step)
        seg = bins[a:b] or bins[a:a + 1]
        out.append(round(max(seg), 1))
    return out


def get_dial_hz(urls):
    """Read the rig's current dial frequency (Hz) from the first engine that
    answers (local server first, then the cloud), or 0."""
    for url in urls:
        if not url:
            continue
        try:
            req = urllib.request.Request(url.rstrip("/") + "/api/state")
            with urllib.request.urlopen(req, timeout=5) as r:
                s = json.loads(r.read())
            radio = s.get("radio", {})
            hz = int(radio.get("dial_hz") or radio.get("rx_hz") or 0)
            if hz:
                return hz
        except Exception:
            continue
    return 0


def post_frame(url, frame, token=None):
    """POST a frame; return (ok, error-or-None). Caller logs sparingly."""
    try:
        body = json.dumps(frame).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Ingest-Token"] = token
        req = urllib.request.Request(url.rstrip("/") + "/api/spectrum", data=body,
                                     method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=5).read()
        return True, None
    except Exception as e:
        return False, e


def sdr_center_for(dial_hz, cfg):
    """Where to physically tune the SDR so `dial_hz` lands in the passband."""
    c = dial_hz + int(cfg["sdr_center_offset_hz"])
    if cfg["sdr_hf_mode"] == "upconverter":
        c += int(cfg["sdr_upconverter_offset_hz"])
    return c


# ── Backends ───────────────────────────────────────────────────────────────────
class SyntheticSDR:
    """No hardware — a believable noise floor with a few carriers + one drifter.
    Lets you verify the /console waterfall before touching real SDR drivers."""
    def __init__(self, cfg):
        self.n = int(cfg["sdr_fft_bins"])
        self.t = 0

    def read_power(self, dial_hz):
        import random
        n, t = self.n, self.t
        self.t += 1
        floor = -110.0
        bins = [floor + random.uniform(-3, 3) for _ in range(n)]
        # a few steady carriers
        for pos, amp, w in ((0.30, 55, 2), (0.5, 40, 1), (0.62, 48, 3), (0.78, 30, 2)):
            c = int(pos * n)
            for i in range(-w * 4, w * 4 + 1):
                j = c + i
                if 0 <= j < n:
                    bins[j] = max(bins[j], floor + amp * math.exp(-(i * i) / (2 * w * w)))
        # one signal drifting across the band
        d = int((0.5 + 0.35 * math.sin(t / 25)) * n)
        for i in range(-8, 9):
            j = d + i
            if 0 <= j < n:
                bins[j] = max(bins[j], floor + 60 * math.exp(-(i * i) / 8))
        return bins


class NumpySDR:
    """SoapySDR or pyrtlsdr front-end with a numpy FFT. Selected by sdr_driver."""
    def __init__(self, cfg):
        import numpy as np
        self.np = np
        self.cfg = cfg
        self.n = int(cfg["sdr_fft_bins"])
        self.win = np.hanning(self.n).astype(np.float32)
        self.sr = int(cfg["sdr_sample_rate"])
        self.driver = cfg["sdr_driver"]
        self.center = None
        self._open()

    def _open(self):
        cfg = self.cfg
        if self.driver == "rtlsdr":
            from rtlsdr import RtlSdr
            self.dev = RtlSdr()
            # HF (e.g. 40m) on a plain RTL2832U (like the Nooelec NESDR SMArt)
            # needs direct sampling — the R820T2 tuner is bypassed and the ADC
            # samples HF straight off the Q branch. No upconverter offset then.
            if cfg["sdr_hf_mode"] == "direct":
                try:
                    self.dev.set_direct_sampling(2)   # 2 = Q branch (HF)
                    print("[sdr] RTL-SDR direct sampling ON (Q branch) for HF")
                except Exception as e:
                    print(f"[sdr] direct sampling not set ({e})")
            self.dev.sample_rate = self.sr
            self.dev.freq_correction = int(cfg["sdr_ppm"]) or 1
            try:
                self.dev.gain = "auto" if cfg["sdr_gain"] == "auto" else float(cfg["sdr_gain"])
            except Exception:
                pass                                    # gain fixed in direct mode
            self._read = lambda: self.dev.read_samples(self.n)
        else:  # soapy
            import SoapySDR
            from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
            args = dict(kv.split("=") for kv in cfg["sdr_device_args"].split(",") if "=" in kv)
            self.dev = SoapySDR.Device(args)
            self.dev.setSampleRate(SOAPY_SDR_RX, 0, self.sr)
            if cfg["sdr_gain"] == "auto":
                try:
                    self.dev.setGainMode(SOAPY_SDR_RX, 0, True)
                except Exception:
                    pass
            else:
                self.dev.setGain(SOAPY_SDR_RX, 0, float(cfg["sdr_gain"]))
            self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
            self.dev.activateStream(self.stream)
            self._buf = self.np.zeros(self.n, self.np.complex64)

            def _rd():
                sr = self.dev.readStream(self.stream, [self._buf], self.n)
                return self.np.array(self._buf) if sr.ret > 0 else self._buf
            self._read = _rd
            self._SOAPY_SDR_RX = SOAPY_SDR_RX

    def tune(self, center_hz):
        if center_hz == self.center:
            return
        self.center = center_hz
        if self.driver == "rtlsdr":
            self.dev.center_freq = center_hz
        else:
            self.dev.setFrequency(self._SOAPY_SDR_RX, 0, float(center_hz))

    def read_power(self, dial_hz):
        np = self.np
        x = np.asarray(self._read(), np.complex64)[:self.n]
        if x.size < self.n:
            x = np.concatenate([x, np.zeros(self.n - x.size, np.complex64)])
        X = np.fft.fftshift(np.fft.fft(x * self.win))
        p = 20.0 * np.log10(np.abs(X) + 1e-9)
        p -= 20.0 * math.log10(self.n)          # normalize by FFT size
        mid = self.n // 2                        # blank the DC spike
        p[mid] = (p[mid - 1] + p[mid + 1]) / 2
        return [round(float(v), 1) for v in p]


def make_backend(cfg):
    d = cfg["sdr_driver"]
    if d == "synthetic":
        return SyntheticSDR(cfg), True
    try:
        return NumpySDR(cfg), False
    except Exception as e:
        print(f"[sdr] {d} backend unavailable ({e}). Falling back to synthetic.")
        print("[sdr] install:  pip install numpy" +
              ("" if d == "soapy" else " pyrtlsdr") +
              ("   plus SoapySDR + the device module (Windows: PothosSDR)." if d == "soapy" else "."))
        return SyntheticSDR(cfg), True


def main():
    cfg = load_cfg()
    url = cfg["sdr_engine_url"]
    if not cfg["sdr_enabled"] and "--synthetic" not in sys.argv and cfg["sdr_driver"] != "synthetic":
        print("[sdr] sdr_enabled is false in station.config.json — nothing to do.")
        print("[sdr] set sdr_enabled=true (and sdr_driver), or run:  python sdr_agent.py --synthetic")
        return

    backend, synthetic = make_backend(cfg)
    span = int(cfg["sdr_sample_rate"])
    period = 1.0 / max(1, int(cfg["sdr_fps"]))
    dial_urls = [url, cfg["cloud_url"]]

    relay = bool(cfg["sdr_relay_cloud"] and cfg["cloud_url"] and cfg["ingest_token"])
    relay_period = 1.0 / max(1, int(cfg["sdr_relay_fps"]))
    relay_bins = int(cfg["sdr_relay_bins"])
    last_relay = 0.0
    if cfg["sdr_relay_cloud"] and not relay:
        print("[sdr] sdr_relay_cloud is on but cloud_url/ingest_token are missing "
              "(set them in agent.config.json) — remote relay disabled.")

    print(f"[sdr] driver={'synthetic' if synthetic else cfg['sdr_driver']} "
          f"span={span/1e6:.3f} MHz bins={cfg['sdr_fft_bins']} fps={cfg['sdr_fps']} -> {url}")
    if relay:
        print(f"[sdr] remote relay ON -> {cfg['cloud_url']} "
              f"({relay_bins} bins @ {cfg['sdr_relay_fps']} fps)")

    last_dial = 0
    lfail = rfail = 0            # local / relay failure counters (sparse logging)
    while True:
        t0 = time.time()
        dial = get_dial_hz(dial_urls)
        if not dial:
            dial = last_dial or 7074000     # idle default so the scope still draws
        last_dial = dial
        try:
            if not synthetic:
                backend.tune(sdr_center_for(dial, cfg))
            bins = backend.read_power(dial)
            frame = {
                "dial_hz": dial, "center_hz": dial, "span_hz": span,
                "bins": bins, "n": len(bins), "synthetic": synthetic,
                "ts": time.time(),
            }
            # full-res -> local server (LAN console). Skip if no local target set.
            if url:
                ok, err = post_frame(url, frame)
                if ok:
                    lfail = 0
                else:
                    lfail += 1
                    if lfail == 1 or lfail % 60 == 0:
                        print(f"[sdr] local post to {url} failing ({err}) — is the "
                              f"dashboard server running there? (relay still works)")
            # remote relay — decimated + throttled so the cloud/viewer bandwidth
            # stays small while the LAN console keeps the full-res frame above.
            if relay and t0 - last_relay >= relay_period:
                last_relay = t0
                rframe = dict(frame, bins=decimate_max(bins, relay_bins))
                rframe["n"] = len(rframe["bins"])
                ok, err = post_frame(cfg["cloud_url"], rframe, token=cfg["ingest_token"])
                if ok:
                    rfail = 0
                else:
                    rfail += 1
                    if rfail == 1 or rfail % 30 == 0:
                        print(f"[sdr] relay post to {cfg['cloud_url']} failing ({err})")
        except Exception as e:
            if lfail % 30 == 0:
                print(f"[sdr] read error: {e}")
            lfail += 1
            time.sleep(min(5, lfail))
        dt = time.time() - t0
        if dt < period:
            time.sleep(period - dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[sdr] stopped")
