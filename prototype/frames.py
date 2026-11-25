"""Row frames shared by live-spectrum (station), live-ingest and live-relay (wd30).

One frame carries one row of one stream: a Doppler strip, a band waterfall, or
a series row (a few float32 values, e.g. the magnetometer's x, y, z, rt).
Little-endian:

    magic   2s   b"LS"
    version B    1
    kind    B    0 = Doppler, 1 = band waterfall, 2 = series
    t       d    UTC seconds of the row's end
    f0      d    Hz of the first bin
    df      f    Hz per bin
    db0     f    dB of byte value 0
    step    f    dB per byte step
    nlen    B    length of the stream name
    nbins   H    number of bins
    name    nlen bytes, ASCII
    bins    nbins bytes (series: nbins/4 float32 values; f0 0, df 1, db0 0, step 1)

Each frame travels as one binary websocket message, station to relay and relay
to browser, unchanged.
"""
import re
import struct

import numpy as np

MAGIC = b"LS"
VERSION = 1
DOPPLER, BAND, SERIES = 0, 1, 2
MAX_VALUES = 16
HEAD = struct.Struct("<2sBBddfffBH")
MAX_BINS = 4096
MAX_FRAME = HEAD.size + 255 + MAX_BINS
NAME_RE = re.compile(rb"^[A-Za-z0-9._-]{1,32}$")
STEP_DB = 0.5


def quantize(db):
    """dB values -> (db0, step, uint8 bins).  db0 sits on a whole dB below the row's
    minimum, so 255 steps of 0.5 dB cover 127.5 dB above it."""
    db = np.asarray(db, dtype=np.float32)
    db0 = float(np.floor(np.nanmin(db))) if db.size and np.isfinite(np.nanmin(db)) else -150.0
    q = np.clip(np.round((np.nan_to_num(db, nan=db0) - db0) / STEP_DB), 0, 255).astype(np.uint8)
    return db0, STEP_DB, q


def encode(kind, name, t, f0, df, db):
    nb = name.encode("ascii")
    if not NAME_RE.match(nb):
        raise ValueError("bad stream name %r" % name)
    db0, step, q = quantize(db)
    if not 1 <= q.size <= MAX_BINS:
        raise ValueError("bin count %d out of range" % q.size)
    return HEAD.pack(MAGIC, VERSION, kind, t, f0, df, db0, step, len(nb), q.size) + nb + q.tobytes()


def encode_series(name, t, values):
    nb = name.encode("ascii")
    if not NAME_RE.match(nb):
        raise ValueError("bad stream name %r" % name)
    values = [float(v) for v in values]
    if not 1 <= len(values) <= MAX_VALUES or not all(np.isfinite(values)):
        raise ValueError("bad series values")
    payload = np.asarray(values, dtype="<f4").tobytes()
    return HEAD.pack(MAGIC, VERSION, SERIES, t, 0.0, 1.0, 0.0, 1.0, len(nb), len(payload)) + nb + payload


def decode(buf):
    """frame bytes -> dict; raises ValueError on anything malformed."""
    if len(buf) < HEAD.size or len(buf) > MAX_FRAME:
        raise ValueError("frame size %d" % len(buf))
    magic, ver, kind, t, f0, df, db0, step, nlen, nbins = HEAD.unpack_from(buf)
    if magic != MAGIC or ver != VERSION or kind not in (DOPPLER, BAND, SERIES):
        raise ValueError("bad header")
    if len(buf) != HEAD.size + nlen + nbins or not 1 <= nbins <= MAX_BINS:
        raise ValueError("length mismatch")
    name = bytes(buf[HEAD.size:HEAD.size + nlen])
    if not NAME_RE.match(name):
        raise ValueError("bad stream name")
    vals = (t, f0, df, db0, step)
    if not all(np.isfinite(v) for v in vals) or df <= 0 or step <= 0:
        raise ValueError("bad numbers")
    if kind == SERIES:
        if nbins % 4 or nbins // 4 > MAX_VALUES:
            raise ValueError("bad series length")
        v = np.frombuffer(buf, dtype="<f4", count=nbins // 4, offset=HEAD.size + nlen)
        if not np.all(np.isfinite(v)):
            raise ValueError("bad series values")
    return {"kind": kind, "name": name.decode(), "t": t, "f0": f0, "df": df, "db0": db0,
            "step": step, "bins": bytes(buf[HEAD.size + nlen:])}
