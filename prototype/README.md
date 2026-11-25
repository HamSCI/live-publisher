# The prototype

The code that runs today: AC0G-S (SIGMA) sends through it, and wd30's relay
serves it at `wss://wd30.wsprdaemon.org/live/`.  It runs by hand, not yet as
a sigmond client; step 2 of [the design](../docs/DESIGN.md#from-prototype-to-client)
turns it into one.

| File | Runs on | Does |
|---|---|---|
| `frames.py` | both ends | the row frame: Doppler, band waterfall, series |
| `live_spectrum.py` | the station | radiod spectrum channels, the feed socket, the signed link |
| `live_relay.py` | the server | checks each station's signature, keeps ten minutes in memory, copies rows to viewers |
| `live.html` | a browser | Doppler strips and band waterfalls |
| `test_live.py` | anywhere | 24 tests |

Tests need numpy, websockets and pytest:

```
uv run --no-project --with numpy --with websockets --with pytest \
    python -m pytest -q test_live.py
```

The station side, as SIGMA runs it (user hsupload, group sigmond, with
`RuntimeDirectory=live-publisher` for the feed socket):

```
live_spectrum.py --status sigma-status.local --station AC0G-S \
    --key /etc/hs-uploader/keys/id_ed25519_host --grid EM38ww --feed mag
```

`--feed mag` takes the magnetometer rows mag-recorder offers on
`/run/live-publisher/feeds.sock`.  `--grid` gives the relay the station's
six-character square; the relay passes nothing finer.

The relay reads a hand-kept `allowed_signers` file for now; the design moves
it to the fleet's registration.  The gmag-webui public build reads the `mag`
feed from the same relay.
