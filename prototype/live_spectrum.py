#!/usr/bin/env python3
"""live-spectrum -- one row a second of Doppler strips and band waterfalls, sent to wd30.

Doppler: joins the IQ streams hf-timestd already receives from radiod (one per
time-station carrier, centred on the carrier), decimates each to 50 samples a
second, and every second takes a 10-second FFT: 0.1 Hz bins, keeping ±5 Hz.
Nothing new is asked of radiod for these.

Waterfalls: one radiod spectrum channel per ham band (ka9q-python SpectrumStream),
polled once a second.

Rows leave through one websocket to wd30's relay.  The relay sends a nonce;
this program signs it with the station's hs-uploader key (ssh-keygen -Y sign),
and the relay checks the signature against its list of station keys.  A dead
link never blocks the receivers: rows queue (two minutes' worth) and the oldest
drop.

    live-spectrum --status sigma-status.local --station AC0G-S \
                  --key /etc/hs-uploader/keys/id_ed25519_host
"""
import argparse
import collections
import logging
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frames  # noqa: E402

log = logging.getLogger("live-spectrum")

CARRIERS_HZ = (2_500_000, 5_000_000, 10_000_000, 15_000_000, 20_000_000, 25_000_000)
# (name, low Hz, high Hz): IARU Region 2 band edges.
BANDS = (("160m", 1_800_000, 2_000_000), ("80m", 3_500_000, 4_000_000),
         ("60m", 5_330_000, 5_410_000), ("40m", 7_000_000, 7_300_000),
         ("30m", 10_100_000, 10_150_000), ("20m", 14_000_000, 14_350_000),
         ("17m", 18_068_000, 18_168_000), ("15m", 21_000_000, 21_450_000),
         ("12m", 24_890_000, 24_990_000), ("10m", 28_000_000, 29_700_000))
BAND_BINS = 1024             # at most
MAX_BANDS = 10               # at once: all of them.  SIGMA, 11-25: ten bands at these widths
                             # add about 21 % of a core to radiod while anyone watches
BAND_LINGER = 30             # seconds a band runs on after its last viewer leaves
# Bin widths a band may use.  Above radiod's 200 Hz crossover a spectrum channel takes one
# FFT of the raw input per poll, of samprate / width points; at or below it radiod runs a
# filter channel all the time.  These widths, like ka9q-web's zoom steps, give FFT sizes of
# small primes at 129.6 and 64.8 Msps.  SIGMA, 11-25: 20 m at 342 Hz (379,173 = 3 x 97 x 1303
# points) cost radiod +12..24 % of a core; at 400 or 500 Hz nothing measurable.
BAND_RBWS = (250.0, 400.0, 500.0, 800.0, 1000.0, 2000.0, 4000.0)
DOPPLER_RATE = 50            # samples a second after decimation
DOPPLER_SECONDS = 10         # FFT length: 0.1 Hz bins
DOPPLER_SPAN_HZ = 5.0
QUEUE_ROWS = 120 * (len(CARRIERS_HZ) + len(BANDS))
SIG_NAMESPACE = "live-stream"
RELAY = "wss://wd30.wsprdaemon.org/live/ingest"


def lowpass_taps(n, cutoff, rate):
    """Windowed-sinc low-pass FIR, unit DC gain."""
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / rate * k) * np.blackman(n)
    return (h / h.sum()).astype(np.float32)


class Decimator:
    """Complex IQ at `rate` -> DOPPLER_RATE.  Stage 1 block-averages down to
    500 Hz (nulls on every multiple of 500 Hz, where WWV's tones and ticks sit);
    stage 2 low-passes at 20 Hz and keeps every 10th sample.  Carries state
    across calls, so callers may hand it any batch size."""

    def __init__(self, rate):
        if rate % 500:
            raise ValueError("input rate %d is not a multiple of 500 Hz" % rate)
        self.block = rate // 500
        self.taps = lowpass_taps(101, 20.0, 500.0)
        self.pending = np.zeros(0, np.complex64)
        self.hist = np.zeros(len(self.taps) - 1, np.complex64)
        self.phase = 0

    def __call__(self, x):
        x = np.concatenate([self.pending, np.asarray(x, np.complex64)])
        n = len(x) // self.block * self.block
        self.pending = x[n:]
        y = x[:n].reshape(-1, self.block).mean(axis=1)
        if not len(y):     # np.convolve would swap its arguments and invent samples
            return np.zeros(0, np.complex64)
        z =np.concatenate([self.hist, y])
        self.hist = z[-(len(self.taps) - 1):]
        f = np.convolve(z, self.taps, mode="valid")
        out = f[(-self.phase) % 10::10]
        self.phase = (self.phase + len(f)) % 10
        return out.astype(np.complex64)


def doppler_row(x):
    """DOPPLER_SECONDS of 50 Hz complex samples -> (f0, df, dB) within ±DOPPLER_SPAN_HZ."""
    n = len(x)
    w = np.hanning(n).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(x * w))
    df = DOPPLER_RATE / n
    freqs = (np.arange(n) - n // 2) * df
    keep = np.abs(freqs) <= DOPPLER_SPAN_HZ + 1e-9
    p = np.abs(spec[keep]) ** 2 / (w.sum() ** 2)
    with np.errstate(divide="ignore"):
        db = 10 * np.log10(np.maximum(p, 1e-30))
    return float(freqs[keep][0]), df, db


class DopplerChannel:
    def __init__(self, name, rate, emit):
        self.name, self.emit, self.rate = name, emit, rate
        self.dec = Decimator(rate)
        self.ring = collections.deque(maxlen=DOPPLER_RATE * DOPPLER_SECONDS)
        self.since = 0
        self.gaps = self.gap_samples = 0

    def on_samples(self, samples, quality):
        for g in quality.batch_gaps:
            self.gaps += 1
            self.gap_samples += g.duration_samples
        y = self.dec(samples)
        self.ring.extend(y)
        self.since += len(y)
        if self.since >= DOPPLER_RATE and len(self.ring) == self.ring.maxlen:
            self.since = 0
            f0, df, db = doppler_row(np.fromiter(self.ring, np.complex64, len(self.ring)))
            self.emit(frames.DOPPLER, self.name, time.time(), f0, df, db)


class Sender:
    """Queue of rows -> the relay's websocket.  Reconnects with backoff."""

    def __init__(self, url, station, key, on_message=None, tick=None, grid=""):
        self.url, self.station, self.key, self.grid = url, station, key, grid
        self.on_message, self.tick = on_message, tick
        self.q = collections.deque(maxlen=QUEUE_ROWS)
        self.cv = threading.Condition()
        self.dropped = 0
        self.sent = 0

    def put(self, frame):
        with self.cv:
            if len(self.q) == self.q.maxlen:
                self.dropped += 1
            self.q.append(frame)
            self.cv.notify()

    def poll(self, ws):
        """Relay messages ({"bands": [...]}) without blocking; then the band manager's tick."""
        while True:
            try:
                m = ws.recv(timeout=0)
            except TimeoutError:
                break
            if isinstance(m, str) and self.on_message is not None:
                try:
                    self.on_message(json.loads(m))
                except (ValueError, AttributeError, TypeError) as e:
                    log.warning("relay message ignored: %s", e)
        if self.tick is not None:
            self.tick()

    def sign(self, nonce):
        p = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", self.key, "-n", SIG_NAMESPACE],
                           input=nonce.encode(), capture_output=True, timeout=10, check=True)
        return p.stdout.decode()

    def run(self):
        from websockets.sync.client import connect
        from websockets.exceptions import WebSocketException
        backoff = 1
        while True:
            started = time.monotonic()
            try:
                with connect(self.url, open_timeout=20, ping_interval=30, max_size=4096) as ws:
                    nonce = json.loads(ws.recv(timeout=20))["nonce"]
                    ws.send(json.dumps({"station": self.station, "sig": self.sign(nonce),
                                        "grid": self.grid}))
                    if not json.loads(ws.recv(timeout=20)).get("ok"):
                        raise RuntimeError("relay refused the link")
                    log.info("linked to %s as %s", self.url, self.station)
                    while True:
                        self.poll(ws)
                        with self.cv:
                            if not self.q:
                                self.cv.wait(1)
                            batch = list(self.q)
                            self.q.clear()
                        for frame in batch:
                            ws.send(frame)
                        self.sent += len(batch)
            except (OSError, WebSocketException, RuntimeError, ValueError, KeyError,
                    subprocess.SubprocessError) as e:
                log.warning("link dropped: %s", e)
            if self.on_message is not None:
                self.on_message({"bands": []})     # no relay, no viewers: let bands linger out
            backoff = 1 if time.monotonic() - started > 60 else min(backoff * 2, 60)
            log.warning("reconnecting in %d s (rows dropped so far: %d)", backoff, self.dropped)
            time.sleep(backoff)


def start_doppler(status, emit):
    from ka9q import RadiodStream, discover_channels
    chans = discover_channels(status)
    found = []
    for want in CARRIERS_HZ:
        hits = [c for c in chans.values()
                if abs(c.frequency - want) < 1 and c.preset == "iq" and c.sample_rate % 500 == 0]
        if not hits:
            log.warning("no IQ channel at %.1f MHz; that Doppler strip stays empty", want / 1e6)
            continue
        c = hits[0]
        name = "wwv%g" % (want / 1e6)
        ch = DopplerChannel(name.replace(".", "_"), c.sample_rate, emit)
        s = RadiodStream(channel=c, on_samples=ch.on_samples,
                         samples_per_packet=max(2, c.sample_rate // 50))
        s.start()
        found.append((s, ch))
        log.info("Doppler %s: ssrc %d, %d Hz IQ", name, c.ssrc, c.sample_rate)
    return found


class StatusHub:
    """One socket on radiod's status group for every spectrum channel this process runs.
    Each ka9q SpectrumStream otherwise opens its own socket and decodes every status packet
    on the group, then drops those for other channels: sixteen streams decoded everything
    sixteen times (SIGMA, 11-25: ~40 % of a core).  Here each packet is decoded once and
    handed to the stream whose SSRC it carries."""

    def __init__(self, ctl):
        import socket
        import struct
        self.subs = {}
        self.lock = threading.Lock()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("0.0.0.0", 5006))
        mreq = struct.pack("=4s4s", socket.inet_aton(ctl.status_mcast_addr),
                           socket.inet_aton(ctl.interface or "0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock = sock
        threading.Thread(target=self.run, name="status-hub", daemon=True).start()

    def add(self, ssrc, cb):
        with self.lock:
            self.subs[ssrc] = cb

    def remove(self, ssrc):
        with self.lock:
            self.subs.pop(ssrc, None)

    def run(self):
        from ka9q.status import decode_status_packet
        while True:
            buf, _addr = self.sock.recvfrom(65536)
            st = decode_status_packet(buf)
            if st is None or st.ssrc is None:
                continue
            with self.lock:
                cb = self.subs.get(st.ssrc)
            sp = st.spectrum
            if cb is None or sp is None or (sp.bin_data is None and sp.bin_byte_data is None):
                continue
            try:
                cb(st)
            except Exception:  # noqa: BLE001 -- one bad row must not stop the others
                log.warning("spectrum callback failed", exc_info=True)


def hub_stream(hub, **kw):
    """A ka9q SpectrumStream that leaves receiving to `hub`.  It still creates, polls and
    removes its radiod channel itself.  Leans on SpectrumStream internals (_create_status_socket,
    _recv_loop, _ssrc, _running) as of ka9q-python 3.28."""
    from ka9q import SpectrumStream
    import socket

    class HubSpectrum(SpectrumStream):
        def _create_status_socket(self):
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    # never read

        def _recv_loop(self):
            while self._running:
                time.sleep(0.25)

        def start(self):
            ssrc = super().start()
            hub.add(ssrc, self._on_spectrum)
            return ssrc

        def stop(self):
            if self._ssrc is not None:
                hub.remove(self._ssrc)
            super().stop()

    return HubSpectrum(**kw)


def first_bin_hz(center, n, bw):
    """Frequency of bin 0.  Spectrum bins reach us in frequency order, lowest first, with
    the centre at index n - n//2 (measured on SIGMA 11-25: a 10 MHz carrier peaks in bin
    51 of 101).  spectrum.c fills them DC-first; the reordering happens before here."""
    return center - (n - n // 2) * bw


def start_doppler_spectrum(ctl, hub, emit):
    """Six narrow radiod spectrum channels, 101 bins of 0.1 Hz on each carrier.  radiod
    runs a very narrow filter channel per carrier; this process only reads the bins."""
    n = int(round(2 * DOPPLER_SPAN_HZ * DOPPLER_SECONDS)) + 1
    rbw = 1.0 / DOPPLER_SECONDS
    streams = []
    for want in CARRIERS_HZ:
        name = ("wwv%g" % (want / 1e6)).replace(".", "_")

        def on_spectrum(st, name=name):
            sp = st.spectrum
            db = sp.bin_power_db if sp is not None else None
            if db is None or len(db) < 3:
                return
            bw = sp.resolution_bw or rbw
            emit(frames.DOPPLER, name, time.time(), first_bin_hz(0.0, len(db), bw), bw, db)

        s = hub_stream(hub, control=ctl, frequency_hz=want, bin_count=n, resolution_bw=rbw,
                       on_spectrum=on_spectrum, poll_interval_sec=1.0)
        s.start()
        streams.append(s)
        log.info("Doppler %s: radiod spectrum, %d bins of %.1f Hz", name, n, rbw)
    return streams


def band_grid(lo, hi):
    """(bin width, bin count) for a band: the narrowest BAND_RBWS width that covers it in
    BAND_BINS bins or fewer."""
    rbw = next((w for w in BAND_RBWS if (hi - lo) / w <= BAND_BINS), BAND_RBWS[-1])
    return rbw, min(BAND_BINS, int(np.ceil((hi - lo) / rbw)))


class Bands:
    """Band waterfalls on demand.  The relay sends {"bands": [...]}, the bands someone is
    watching; this keeps a radiod spectrum channel for each, at most MAX_BANDS, and drops
    one BAND_LINGER seconds after the last viewer leaves.  Ten bands at once cost SIGMA's
    radiod about 70 % of a core (11-25); one or two cost a fraction of that."""

    def __init__(self, ctl, emit, allowed=None, hub=None):
        self.ctl, self.emit, self.hub = ctl, emit, hub
        self.allowed = set(allowed if allowed is not None else (b[0] for b in BANDS))
        self.running = {}            # name -> SpectrumStream
        self.unwanted_since = {}     # name -> monotonic time the relay stopped asking
        self.wanted = []

    def want(self, names):
        if "all" in names:
            names = [b[0] for b in BANDS if b[0] in self.allowed]
        names = [n for n in names if n in self.allowed][:MAX_BANDS]
        if names != self.wanted:
            log.info("relay asks for bands: %s", ", ".join(names) or "none")
        self.wanted = names
        self.tick()

    def tick(self):
        now = time.monotonic()
        for name in list(self.running):
            if name in self.wanted:
                self.unwanted_since.pop(name, None)
            elif now - self.unwanted_since.setdefault(name, now) >= BAND_LINGER:
                self.stop(name)
        for name in self.wanted:
            if name in self.running:
                continue
            if len(self.running) >= MAX_BANDS:
                idle = [n for n in self.running if n not in self.wanted]
                if not idle:
                    continue
                self.stop(idle[0])      # someone is waiting: no linger for an idle band
            self.start(name)

    def start(self, name):
        lo, hi = next((lo, hi) for n, lo, hi in BANDS if n == name)
        rbw, nbins = band_grid(lo, hi)

        def on_spectrum(st):
            sp = st.spectrum
            db = sp.bin_power_db if sp is not None else None
            if db is None or len(db) < 2:
                return
            bw = sp.resolution_bw or rbw
            f0 = first_bin_hz(st.frequency or (lo + hi) / 2, len(db), bw)
            self.emit(frames.BAND, name, time.time(), f0, bw, db)

        s = hub_stream(self.hub, control=self.ctl, frequency_hz=(lo + hi) / 2, bin_count=nbins,
                       resolution_bw=rbw, on_spectrum=on_spectrum, poll_interval_sec=1.0)
        s.start()
        self.running[name] = s
        log.info("band %s on: %.3f-%.3f MHz, %d bins of %.0f Hz", name, lo / 1e6, hi / 1e6,
                 nbins, rbw)

    def stop(self, name):
        s = self.running.pop(name)
        self.unwanted_since.pop(name, None)
        try:
            s.stop()
        except Exception as e:  # noqa: BLE001 -- radiod drops an unpolled channel anyway
            log.warning("band %s stop: %s", name, e)
        log.info("band %s off", name)

    def stop_all(self):
        for name in list(self.running):
            self.stop(name)


class FeedSocket:
    """Rows other clients offer (live-publisher's contract): one LS frame per datagram on a
    local socket.  Forwards only well-formed series rows of feeds the operator turned on."""

    def __init__(self, path, feeds, put):
        import grp
        import os
        import socket
        self.feeds, self.put = set(feeds), put
        self.taken = self.refused = 0
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(path)
        try:
            os.chown(path, -1, grp.getgrnam("sigmond").gr_gid)
        except (KeyError, PermissionError):
            pass
        os.chmod(path, 0o660)

    def take(self, buf):
        try:
            d = frames.decode(buf)
        except ValueError:
            d = None
        if d is None or d["kind"] != frames.SERIES or d["name"] not in self.feeds:
            self.refused += 1
            return False
        self.taken += 1
        self.put(buf)
        return True

    def run(self):
        while True:
            self.take(self.sock.recv(frames.MAX_FRAME + 1))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--status", required=True, help="radiod status name, e.g. sigma-status.local")
    p.add_argument("--station", required=True, help="the name the relay knows this station by")
    p.add_argument("--key", required=True, help="ssh private key that signs the relay's nonce")
    p.add_argument("--relay", default=RELAY)
    p.add_argument("--grid", default="", help="six-character grid square the relay shows")
    p.add_argument("--feed", action="append", default=[],
                   help="a series feed the operator turned on (repeat), e.g. mag")
    p.add_argument("--feed-socket", default="/run/live-publisher/feeds.sock")
    p.add_argument("--no-bands", action="store_true")
    p.add_argument("--no-doppler", action="store_true")
    p.add_argument("--doppler", choices=("spectrum", "iq"), default="spectrum",
                   help="spectrum: radiod narrow spectrum channels (default); iq: decode "
                        "hf-timestd's IQ streams here (costs this process far more CPU)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    bands = None

    def on_message(m):
        if bands is not None and isinstance(m.get("bands"), list):
            bands.want([str(x) for x in m["bands"]])

    sender = Sender(a.relay, a.station, a.key, on_message=on_message,
                    tick=lambda: bands.tick() if bands is not None else None, grid=a.grid)
    feeds = None
    if a.feed:
        feeds = FeedSocket(a.feed_socket, a.feed, sender.put)
        threading.Thread(target=feeds.run, daemon=True).start()

    def emit(kind, name, t, f0, df, db):
        try:
            sender.put(frames.encode(kind, name, t, f0, df, db))
        except ValueError as e:
            log.warning("row %s dropped: %s", name, e)

    logging.getLogger("ka9q").setLevel(logging.ERROR)
    from ka9q import RadiodControl
    ctl = RadiodControl(a.status)
    hub = StatusHub(ctl)
    keep, dop = [], []
    if not a.no_doppler and a.doppler == "iq":
        dop = start_doppler(a.status, emit)
        keep += [s for s, _ch in dop]
    elif not a.no_doppler:
        keep += start_doppler_spectrum(ctl, hub, emit)
    if not a.no_bands:
        bands = Bands(ctl, emit, hub=hub)
    def summary():
        while True:
            time.sleep(60)
            if feeds is not None:
                log.info("feeds: %d rows taken, %d refused", feeds.taken, feeds.refused)
                feeds.taken = feeds.refused = 0
            log.info("minute: %d rows sent, %d dropped; IQ gaps %s", sender.sent, sender.dropped,
                     ", ".join("%s %d (%.1f s)" % (ch.name, ch.gaps, ch.gap_samples / ch.rate)
                               for _s, ch in dop) or "-")
            sender.sent = 0
            for _s, ch in dop:
                ch.gaps = ch.gap_samples = 0
    threading.Thread(target=summary, daemon=True).start()
    try:
        sender.run()
    finally:
        if bands is not None:
            bands.stop_all()
        for s in keep:
            try:
                s.stop()
            except Exception:  # noqa: BLE001 -- best effort on the way out
                pass


if __name__ == "__main__":
    main()
