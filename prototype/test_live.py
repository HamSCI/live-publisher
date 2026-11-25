import unittest

import numpy as np

import frames
import live_spectrum as ls


class Frames(unittest.TestCase):
    def test_round_trip(self):
        db = np.linspace(-120, -40, 101)
        f = frames.encode(frames.DOPPLER, "wwv10", 1.7e9, -5.0, 0.1, db)
        d = frames.decode(f)
        self.assertEqual((d["kind"], d["name"], d["f0"]), (frames.DOPPLER, "wwv10", -5.0))
        self.assertAlmostEqual(d["df"], 0.1, places=6)
        back = d["db0"] + np.frombuffer(d["bins"], np.uint8) * d["step"]
        self.assertLess(np.max(np.abs(back - db)), 0.26)

    def test_decode_refuses_malformed(self):
        good = frames.encode(frames.BAND, "20m", 1.7e9, 14e6, 342.0, np.zeros(1024))
        for bad in (good[:-1], good + b"x", b"XX" + good[2:], good[:20],
                    frames.encode(frames.BAND, "20m", 1.7e9, 14e6, 342.0, np.zeros(4)).replace(b"20m", b"2 m")):
            with self.assertRaises(ValueError):
                frames.decode(bad)

    def test_bad_name_refused_at_encode(self):
        with self.assertRaises(ValueError):
            frames.encode(frames.BAND, "a b", 0, 0, 1, np.zeros(4))


class Doppler(unittest.TestCase):
    RATE = 24000

    def tone(self, hz, seconds, rate=RATE, amp=1.0):
        t = np.arange(int(seconds * rate)) / rate
        return (amp * np.exp(2j * np.pi * hz * t)).astype(np.complex64)

    def test_tone_lands_in_its_bin(self):
        dec = ls.Decimator(self.RATE)
        y = dec(self.tone(1.3, 12))
        f0, df, db = ls.doppler_row(y[-500:])
        self.assertAlmostEqual(df, 0.1)
        self.assertAlmostEqual(f0, -5.0)
        self.assertEqual(len(db), 101)
        self.assertAlmostEqual(f0 + df * int(np.argmax(db)), 1.3, places=6)

    def test_batches_do_not_change_the_output(self):
        x = self.tone(-2.2, 6) + self.tone(600.0, 6, amp=0.5)
        whole = ls.Decimator(self.RATE)(x)
        d = ls.Decimator(self.RATE)
        parts, i = [], 0
        for n in (1, 479, 4801, 7, 24000, 33333):
            parts.append(d(x[i:i + n]))
            i += n
        parts.append(d(x[i:]))
        split = np.concatenate(parts)
        self.assertEqual(len(split), len(whole))
        self.assertLess(np.max(np.abs(split - whole)), 1e-5)

    def test_output_rate_is_50_hz(self):
        y = ls.Decimator(self.RATE)(self.tone(0.0, 10))
        self.assertIn(len(y), (499, 500, 501))

    def test_wwv_tones_and_ticks_stay_out(self):
        # 100 Hz BCD, 500/600 Hz tones, a 1 kHz tick: all far below the carrier.
        x = self.tone(0.5, 12)
        for hz in (100.0, 500.0, 600.0, 1000.0, 33.0):
            x = x + self.tone(hz, 12)
        y = ls.Decimator(self.RATE)(x)
        _f0, _df, db = ls.doppler_row(y[-500:])
        peak = int(np.argmax(db))
        self.assertEqual(peak, 55)
        others = np.delete(db, range(peak - 3, peak + 4))
        self.assertLess(np.max(others), db[peak] - 40)

    def test_rate_must_divide_by_500(self):
        with self.assertRaises(ValueError):
            ls.Decimator(44100)


class BinOrder(unittest.TestCase):
    def test_centre_sits_at_n_minus_half(self):
        # SIGMA, 11-25: a 10 MHz carrier peaked in bin 51 of 101 at 0.1 Hz.
        self.assertAlmostEqual(ls.first_bin_hz(0.0, 101, 0.1) + 51 * 0.1, 0.0)
        self.assertAlmostEqual(ls.first_bin_hz(14.175e6, 1024, 342.0) + 512 * 342.0, 14.175e6)


class Bands(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.started, self.stopped = [], []
        b = ls.Bands(ctl=None, emit=None)
        b.start = lambda name: (self.started.append(name), b.running.__setitem__(name, object()))
        def stop(name):
            self.stopped.append(name)
            b.running.pop(name); b.unwanted_since.pop(name, None)
        b.stop = stop
        self.b = b
        self._mono = ls.time.monotonic
        ls.time.monotonic = lambda: self.now
        self._cap, ls.MAX_BANDS = ls.MAX_BANDS, 2      # the logic, whatever the shipped cap

    def tearDown(self):
        ls.time.monotonic = self._mono
        ls.MAX_BANDS = self._cap

    def test_shipped_cap_is_every_band(self):
        self.assertEqual(self._cap, len(ls.BANDS))

    def test_all_means_every_band(self):
        ls.MAX_BANDS = len(ls.BANDS)
        self.b.want(["all"])
        self.assertEqual(self.started, [b[0] for b in ls.BANDS])

    def test_round_widths_give_small_prime_ffts(self):
        def smooth(n):
            for p in (2, 3, 5):
                while n % p == 0:
                    n //= p
            return n == 1
        for name, lo, hi in ls.BANDS:
            rbw, n = ls.band_grid(lo, hi)
            self.assertGreater(rbw, 200, name)                  # radiod's cheap path
            self.assertGreaterEqual(rbw * n, hi - lo, name)      # covers the band
            self.assertLessEqual(n, ls.BAND_BINS, name)
            for rate in (129.6e6, 64.8e6):
                self.assertTrue(smooth(round(rate / rbw)), (name, rbw, rate))

    def test_waiting_band_takes_an_idle_slot_at_once(self):
        ls.MAX_BANDS = 1
        self.b.want(["20m"])
        self.b.want(["80m"])            # 20m now idle; 80m must not wait out the linger
        self.assertEqual((self.started, self.stopped), (["20m", "80m"], ["20m"]))

    def test_starts_only_what_is_wanted_and_caps_at_max(self):
        self.b.want(["20m", "40m", "15m", "bogus"])
        self.assertEqual(self.started, ["20m", "40m"])
        self.assertEqual(sorted(self.b.running), ["20m", "40m"])

    def test_unknown_band_never_starts(self):
        self.b.want(["2m", "../x"])
        self.assertEqual(self.started, [])

    def test_lingers_then_stops(self):
        self.b.want(["20m"])
        self.b.want([])
        self.now += ls.BAND_LINGER - 1
        self.b.tick()
        self.assertEqual(self.stopped, [])
        self.now += 2
        self.b.tick()
        self.assertEqual(self.stopped, ["20m"])

    def test_wanted_again_before_linger_keeps_it(self):
        self.b.want(["20m"]); self.b.want([])
        self.now += 10
        self.b.want(["20m"])
        self.now += ls.BAND_LINGER * 2
        self.b.tick()
        self.assertEqual((self.started, self.stopped), (["20m"], []))

    def test_cap_holds_while_every_band_is_watched(self):
        self.b.want(["20m", "40m", "15m"])
        self.assertEqual(self.started, ["20m", "40m"])
        self.now += ls.BAND_LINGER * 3
        self.b.tick()
        self.assertEqual((self.started, self.stopped), (["20m", "40m"], []))


class Series(unittest.TestCase):
    def test_round_trip(self):
        d = frames.decode(frames.encode_series("mag", 1795629601.25, [1.5, -2.25, 40000.0, 21.5]))
        self.assertEqual((d["kind"], d["name"], d["t"]), (frames.SERIES, "mag", 1795629601.25))
        self.assertEqual(np.frombuffer(d["bins"], "<f4").tolist(), [1.5, -2.25, 40000.0, 21.5])

    def test_decode_refuses_bad_series(self):
        good = frames.encode_series("mag", 1.0, [1, 2, 3, 4])
        nan = good[:-4] + np.float32("nan").tobytes()
        with self.assertRaises(ValueError):
            frames.decode(nan)
        with self.assertRaises(ValueError):
            frames.encode_series("mag", 1.0, [0.0] * (frames.MAX_VALUES + 1))

    def test_mag_recorder_frames_decode_here(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mag-recorder/src"))
        try:
            from mag_recorder.core.live_feed import sample_frame
        except ImportError:
            self.skipTest("mag-recorder checkout not beside this one")
        f = sample_frame({"ts": "2026-11-25T18:00:01.250Z", "rt": 21.5, "x": 1.5, "y": -2.25, "z": 4e4})
        d = frames.decode(f)
        self.assertEqual((d["kind"], d["name"], d["t"]), (frames.SERIES, "mag", 1795629601.25))

    def test_feed_socket_takes_only_turned_on_series(self):
        import tempfile, os
        got = []
        with tempfile.TemporaryDirectory(dir="/var/tmp") as td:
            fs = ls.FeedSocket(os.path.join(td, "feeds.sock"), ["mag"], got.append)
            self.assertTrue(fs.take(frames.encode_series("mag", 1.0, [1, 2, 3, 4])))
            self.assertFalse(fs.take(frames.encode_series("other", 1.0, [1])))
            self.assertFalse(fs.take(frames.encode(frames.BAND, "mag", 1.0, 0, 1, [1.0])))
            self.assertFalse(fs.take(b"junk"))
        self.assertEqual((len(got), fs.taken, fs.refused), (1, 1, 3))


class RelayHello(unittest.TestCase):
    def test_grid6_never_passes_anything_finer_or_odd(self):
        import live_relay as lr
        self.assertEqual(lr.grid6("em38WW"), "EM38ww")
        self.assertEqual(lr.grid6("EM38"), "EM38")
        for bad in ("EM38ww12", "39.1,-94.5", "", None, "ZZ99zz"):
            self.assertEqual(lr.grid6(bad), "")

    def test_rows_age_by_the_relay_clock_not_the_stamp(self):
        import live_relay as lr
        st = lr.Station("AC0G-S")
        row = lambda t: frames.encode_series("mag", t, [1.0])
        now = 1_000_000.0
        self.assertFalse(st.add(frames.decode(row(now + 3600)), row(now + 3600), now=now))
        self.assertFalse(st.add(frames.decode(row(now - 3600)), row(now - 3600), now=now))
        for i in range(5):
            self.assertTrue(st.add(frames.decode(row(now - 590 + i)), row(now - 590 + i), now=now))
        later = now + 30
        st.add(frames.decode(row(later)), row(later), now=later)
        self.assertEqual(len(st.streams["mag"]), 1)      # the five aged out by the relay's clock
        self.assertEqual(st.streams["mag"].maxlen, lr.MAX_ROWS)

    def test_feeds_name_series_streams(self):
        import live_relay as lr
        st = lr.Station("AC0G-S")
        for kind, name in ((frames.DOPPLER, "WWV10"), (frames.SERIES, "mag")):
            buf = (frames.encode_series(name, 100.0, [1.0]) if kind == frames.SERIES
                   else frames.encode(kind, name, 100.0, 0, 1, [1.0]))
            st.add(frames.decode(buf), buf, now=100.0)
        self.assertEqual(st.feeds(), ["doppler", "mag"])
        self.assertIn("grid", st.summary())


if __name__ == "__main__":
    unittest.main()
