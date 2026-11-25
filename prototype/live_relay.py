#!/usr/bin/env python3
"""live-relay -- holds the last ten minutes of each station's rows and serves them to browsers.

One websocket server, two paths (nginx carries wss://wd30.wsprdaemon.org/live/ to it):

/live/ingest  a station.  The relay sends {"nonce": HEX}.  The station answers
              {"station": NAME, "sig": SSHSIG} -- the nonce signed with its
              hs-uploader key (ssh-keygen -Y sign -n live-stream).  The relay
              checks it against ALLOWED_SIGNERS, where each line names one
              station and its key.  Then each binary message carries one row
              (frames.py).  The name comes from the signature check, never from
              the rows.
/live/        a viewer.  ?station=AC0G-S&band=20m gives the backlog, then each new
              row, one binary message per row.  The first message is always JSON
              text: {"type": "hello", "stations": {name: {...}}}.  A viewer may
              switch by sending {"station": NAME} or {"band": NAME}.  The relay
              tells each station {"bands": [...]}, the bands its viewers have
              open, so the station runs only those.  Viewers never reach radiod.

Memory only: nothing touches disk or ClickHouse.
"""
import argparse
import asyncio
import collections
import json
import re
import logging
import secrets
import subprocess
import tempfile
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frames  # noqa: E402

from websockets.asyncio.server import serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

log = logging.getLogger("live-relay")

BACKLOG_SECONDS = 600
MAX_STATIONS = 64
MAX_STREAMS = 32
MAX_CLIENTS = 300
CLIENT_QUEUE = 2000          # rows; a viewer further behind than this gets dropped
STALE_SECONDS = 120
MAX_AHEAD = 30               # s: a row stamped further ahead than this is refused
MAX_ROWS = 40 * BACKLOG_SECONDS   # per stream: a hard cap whatever the stamps say
MAX_VERIFY = 8               # signature checks at once; a sign-in waits up to VERIFY_WAIT
VERIFY_WAIT = 5
HELLO_SECONDS = 5            # a station signs its nonce in well under a second
MAX_PENDING_PER_ADDR = 2     # sign-ins in flight from one address (nginx sets X-Forwarded-For)
SIG_NAMESPACE = "live-stream"
ALLOWED_SIGNERS = "/etc/live-relay/allowed_signers"


class Station:
    def __init__(self, name):
        self.name = name
        self.streams = {}        # stream name -> deque of (t, kind, frame bytes)
        self.writer = None
        self.last = 0.0
        self.viewers = set()
        self.grid = ""

    def add(self, d, frame, now=None):
        """Keep the row unless its stamp lies ahead of the relay's clock or behind the
        backlog.  The relay's clock, not the station's stamps, decides what ages out."""
        now = time.time() if now is None else now
        if not now - BACKLOG_SECONDS <= d["t"] <= now + MAX_AHEAD:
            return False
        q = self.streams.get(d["name"])
        if q is None:
            if len(self.streams) >= MAX_STREAMS:
                return False
            q = self.streams[d["name"]] = collections.deque(maxlen=MAX_ROWS)
        q.append((d["t"], d["kind"], frame))
        while q and q[0][0] < now - BACKLOG_SECONDS:
            q.popleft()
        self.last = time.time()
        return True

    def backlog(self):
        rows = [r for q in self.streams.values() for r in q]
        rows.sort(key=lambda r: r[0])
        return [r[2] for r in rows]

    def wanted_bands(self):
        """Bands viewers have open, most-watched first; the station caps how many it runs."""
        count = collections.Counter(v.band for v in self.viewers if v.band)
        return [b for b, _n in count.most_common()]

    async def tell(self):
        if self.writer is not None:
            try:
                await self.writer.send(json.dumps({"bands": self.wanted_bands()}))
            except ConnectionClosed:
                pass

    def feeds(self):
        """What this station offers: doppler, bands, then each series stream by name."""
        kinds = {q[-1][1] for q in self.streams.values() if q}
        out = [f for f, k in (("doppler", frames.DOPPLER), ("bands", frames.BAND)) if k in kinds]
        return out + sorted(n for n, q in self.streams.items() if q and q[-1][1] == frames.SERIES)

    def summary(self):
        return {"live": time.time() - self.last < STALE_SECONDS, "last": round(self.last, 1),
                "grid": self.grid, "feeds": self.feeds(),
                "streams": sorted(self.streams,
                                  key=lambda n: (self.streams[n][-1][1] if self.streams[n] else 9, n))}


class Relay:
    # ---- station side ----
    def __init__(self, allowed=ALLOWED_SIGNERS):
        self.stations = {}
        self.clients = 0
        self.verifying = asyncio.Semaphore(MAX_VERIFY)
        self.pending = collections.Counter()     # address -> sign-ins in flight
        self.allowed = allowed

    def verify(self, name, nonce, sig):
        """True when `sig` signs `nonce` with the key ALLOWED_SIGNERS gives `name`."""
        with tempfile.NamedTemporaryFile("w", suffix=".sig") as f:
            f.write(sig)
            f.flush()
            p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", self.allowed, "-I", name,
                                "-n", SIG_NAMESPACE, "-s", f.name],
                               input=nonce.encode(), capture_output=True, timeout=10)
        return p.returncode == 0

    async def on_station(self, ws, addr="local"):
        nonce = secrets.token_hex(16)
        name = None
        try:
            self.pending[addr] += 1
            try:
                await ws.send(json.dumps({"nonce": nonce}))
                m = json.loads(await asyncio.wait_for(ws.recv(), HELLO_SECONDS))
                name, sig = m.get("station", ""), m.get("sig", "")
                if not (isinstance(name, str) and frames.NAME_RE.match(name.encode())
                        and isinstance(sig, str) and len(sig) < 4096):
                    raise ValueError("bad hello")
                # only the ssh-keygen check, the costly step, counts against the global cap
                await asyncio.wait_for(self.verifying.acquire(), VERIFY_WAIT)
                try:
                    ok = await asyncio.to_thread(self.verify, name, nonce, sig)
                finally:
                    self.verifying.release()
            finally:
                self.pending[addr] -= 1
                if self.pending[addr] <= 0:
                    del self.pending[addr]
            if not ok:
                raise ValueError("%s: signature refused" % name)
            st = self.stations.get(name)
            if st is None:
                if len(self.stations) >= MAX_STATIONS:
                    raise ValueError("too many stations")
                st = self.stations[name] = Station(name)
            if st.writer is not None:
                log.info("%s: new link replaces the old one", name)
                await st.writer.close(1000, "replaced")
            st.writer = ws
            st.grid = grid6(m.get("grid"))
            await ws.send(json.dumps({"ok": True}))
            await st.tell()
            log.info("%s: linked from %s", name, addr)
            async for buf in ws:
                if not isinstance(buf, bytes):
                    continue
                d = frames.decode(buf)
                if st.add(d, buf):
                    for v in list(st.viewers):
                        v.offer(buf)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except (ValueError, AttributeError, TypeError, subprocess.TimeoutExpired) as e:
            log.warning("ingest refused: %s", e)
            await ws.close(1008, "refused")
        finally:
            st = self.stations.get(name) if name else None
            if st is not None and st.writer is ws:
                st.writer = None
                log.info("%s: link closed", name)

    async def route(self, ws):
        path = urlsplit(ws.request.path).path.rstrip("/")
        if path.endswith("/ingest"):
            addr = ws.request.headers.get("X-Forwarded-For", "local")
            if self.pending[addr] >= MAX_PENDING_PER_ADDR:
                await ws.close(1013, "busy")
                return
            await self.on_station(ws, addr)
        else:
            await self.on_viewer(ws)

    # ---- viewer side ----
    def hello(self):
        return json.dumps({"type": "hello", "stations": {n: s.summary() for n, s in
                                                          sorted(self.stations.items())}})

    async def on_viewer(self, ws):
        if self.clients >= MAX_CLIENTS:
            await ws.close(1013, "busy")
            return
        self.clients += 1
        viewer = Viewer(ws)
        sender = None
        try:
            q = parse_qs(urlsplit(ws.request.path).query)
            await ws.send(self.hello())
            viewer.band = band_name((q.get("band") or [""])[0])
            await self.watch(viewer, (q.get("station") or [""])[0])
            sender = asyncio.create_task(viewer.pump())
            async for msg in ws:
                if isinstance(msg, str) and len(msg) < 200:
                    try:
                        m = json.loads(msg)
                        want, band = m.get("station"), m.get("band")
                    except (ValueError, AttributeError):
                        continue
                    if band is not None:
                        viewer.band = band_name(band)
                    if want is not None:
                        await ws.send(self.hello())
                        await self.watch(viewer, want)
                    elif viewer.station is not None:
                        await viewer.station.tell()
        except ConnectionClosed:
            pass
        finally:
            if sender is not None:
                sender.cancel()
            await self.watch(viewer, "")
            self.clients -= 1

    async def watch(self, viewer, name):
        old = viewer.station
        if old is not None:
            old.viewers.discard(viewer)
            viewer.station = None
            await old.tell()
        viewer.clear()
        st = self.stations.get(name) if isinstance(name, str) else None
        if st is not None:
            viewer.station = st
            rows = st.backlog()
            viewer.limit = len(rows) + CLIENT_QUEUE     # the backlog never counts as lag
            for buf in rows:
                viewer.offer(buf)
            st.viewers.add(viewer)
            await st.tell()


GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([a-x]{2})?$")


def grid6(g):
    """A Maidenhead square of 4 or 6 characters, else empty: rule 6, never a position."""
    if not isinstance(g, str) or len(g) not in (4, 6):
        return ""
    g = g[:2].upper() + g[2:4] + g[4:6].lower()
    return g if GRID_RE.match(g) else ""


def band_name(b):
    return b if isinstance(b, str) and frames.NAME_RE.match(b.encode()) and len(b) <= 8 else ""


class Viewer:
    def __init__(self, ws):
        self.ws, self.station, self.band = ws, None, ""
        self.q = collections.deque()
        self.ev = asyncio.Event()
        self.limit = CLIENT_QUEUE

    def clear(self):
        self.q.clear()

    def offer(self, buf):
        if len(self.q) >= self.limit:
            self.q.clear()
            asyncio.ensure_future(self.ws.close(1008, "too slow"))
            return
        self.q.append(buf)
        self.ev.set()

    async def pump(self):
        while True:
            await self.ev.wait()
            self.ev.clear()
            while self.q:
                await self.ws.send(self.q.popleft())


async def main_async(a):
    relay = Relay(a.allowed)
    async with serve(relay.route, a.host, a.port, max_size=16384, ping_interval=30):
        log.info("ws://%s:%d/live/ (viewers) and /live/ingest (stations); signers %s",
                 a.host, a.port, a.allowed)
        await asyncio.Future()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8130)
    p.add_argument("--allowed", default=ALLOWED_SIGNERS, help="ssh allowed_signers file")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main_async(a))


if __name__ == "__main__":
    main()
