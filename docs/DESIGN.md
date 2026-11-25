# live-publisher: design

Draft for review, 2026-11-25.  Michael Hauan and Rob Robinett.

live-publisher sends a station's live, public data out to a relay, which
passes it to anyone with a browser.  It follows sigmond's public data policy
(`sigmond/docs/public-data-policy.md`): data goes out, nothing public comes
in, the operator decides, sigmond sets the budget.

A prototype has run on AC0G-S (SIGMA) since 2026-11-24 and draws its
Doppler strips and band waterfalls at `wss://wd30.wsprdaemon.org/live/`.
This design turns it into a sigmond client the fleet can run.

## What a visitor sees

A web page, embedded in the project's site.  The visitor picks a station
and sees, one row a second:

- six Doppler strips, one per time-station carrier (WWV and WWVH at 2.5,
  5, 10, 15, 20 and 25 MHz), each ±5 Hz wide in 0.1 Hz steps;
- ten ham-band waterfalls, 160 m through 10 m.

On opening, the page fills in the last ten minutes.  Later feeds, starting
with the magnetometer's one-second reading, join the same page.

## Three parts, three owners

```
station                                              relay                 browser
 radiod ── spectrum channels ──┐
                               ├── live-publisher ──wss──> live relay ──wss──> page
 mag-recorder ── 1 Hz feed ────┘   (signed, one link)  (memory only)  <── band choice
         ^                               ^
         └── sigmond: opt-in, budget, identity, registration
```

**live-publisher, on the station (this repository).**  One process per
station.  It owns the station's single public link: it opens it, signs in,
sends every feed's rows, and follows the relay's band requests.  Its first
feed, the spectrum feed, lives inside it: it asks radiod for spectrum
channels through ka9q-python, as ka9q-web does, and changes nothing in
radiod's configuration.  Other clients add feeds through the contract.

**sigmond, on the station.**  Sigmond decides; live-publisher acts.
Sigmond holds the operator's opt-in per feed, sets the CPU and memory
limits, keeps live-publisher off radiod's cores, tells it when to pause,
and carries the "live" scope to the fleet's registration.  Sigmond itself
stays stdlib-only and never touches the data.

**The relay, on a server.**  It checks each station's signature against
the fleet's registry, keeps ten minutes of every feed in memory, and
copies rows to viewers.  It writes nothing to disk.  It runs on wd30 now
and moves to wd20 when wd20 returns.  It belongs in wsprdaemon-server
beside the other server code, if Rob agrees.

## Why a client, not sigmond itself

| Question | Answer |
|---|---|
| What does it need to run? | ka9q-python and websockets.  smd's core stays stdlib-only so it runs before any venv exists. |
| How does it live? | A long-running service that talks to radiod every second.  sigmond's own units mostly check something and exit. |
| How does it ship and stop? | As a client it gets `smd install` and `smd disable`, a row in each release's manifest, a place in the CPU-affinity map, and drift checks.  A fault stays inside one component. |
| Who decides what goes public? | Sigmond: opt-in, budget and identity belong with the station's other policy. |

## The feeds

| Feed | Source | Rows | Cost on SIGMA |
|---|---|---|---|
| `doppler` (6 strips) | radiod spectrum channels, 101 bins of 0.1 Hz centred on each carrier | 6 a second, always on while enabled | radiod: nothing measurable |
| `bands` (up to 10) | radiod spectrum channels, one per band, 1024 bins or fewer | 1 a second per band, **only while someone watches it** | radiod: about 20 % of a core for all ten |
| `mag` (next) | mag-recorder's one-second reading | 1 a second | to measure |

**Bands on demand.**  The relay tells the station which bands its viewers
have open; the station starts those channels and drops a band 30 s after
its last viewer leaves.  With nobody watching, no band runs.  This uses
the policy's one exception: the relay passes only band names, and the
station starts only bands from its own list of ten, never more than the
cap sigmond sets.

**Round bin widths.**  radiod's wide spectrum path takes one FFT of
(sample rate ÷ bin width) points per update.  A width that leaves a large
prime factor costs far more: on SIGMA, 20 m at 342 Hz (an FFT of 379,173 =
3 × 97 × 1303 points) cost radiod 12-24 % of a core, while 400 or 500 Hz
cost nothing measurable.  live-publisher picks from 250, 400, 500, 800,
1000, 2000 and 4000 Hz, as ka9q-web's zoom steps do; a test checks that
every band's FFT factors into 2, 3 and 5 at both sample rates the fleet
runs (64.8 and 129.6 Msps).

**One status listener.**  ka9q-python's `SpectrumStream` decodes every
radiod status packet once per channel; with sixteen channels that cost
40 % of a core.  live-publisher decodes each packet once and hands it to
the channel it names: 3-4 % of a core in all.

## Adding a feed from another client

A client offers a feed by declaring it in its `deploy.toml`, in a new
contract section beside the sink section:

```toml
[[live.feeds]]
name = "mag"                     # unique on the station; letters, digits, . _ -
kind = "series"                  # series (one value set a row) or spectrum
rate_hz = 1
socket = "/run/live-publisher/feeds.sock"
```

The client sends each row as one datagram to that socket, in the frame
format below.  live-publisher reads the socket, checks the frame, and
forwards rows only for feeds the operator has turned on.  A client never
opens its own public connection.  This matches how recorders already wake
the uploader through a local datagram socket: a lost datagram loses one
row, never the link.

## The magnetometer page: gmag-webui's public build

gmag-webui already draws the magnetometer in the browser: live plots,
baseline, rotation, moving average, export, `.log` upload.  Its small Deno
server only serves the files and relays mag-usb's local feed at `/ws`.
Under this design the relay takes over the feed and the project site
serves the page, so the same interface goes public without opening any
station.

- **Feed.**  mag-recorder declares a `mag` feed (`kind = "series"`,
  `rate_hz = 1`) and sends each mag-usb sample, about 100 bytes, to
  live-publisher's socket.  mag-usb stays bound to the station's
  loopback.  Before a sample leaves, mag-recorder removes any precise
  position it carries (policy rule 6); to confirm against mag-usb's
  message format.
- **Page.**  A second build target of the gmag-webui repository, not a
  fork: one HTML file of about 150 KB, with Plotly and mqtt.js from a
  pinned CDN instead of the 14 MB vendored copies, so it fits a Sites
  embed.  It adds a "relay" source type and a station picker filled from
  the relay's station list.  Every analysis feature runs in the viewer's
  browser and costs the station nothing.
- **Local dashboard.**  Operators keep the full gmag-webui on their own
  network, as now.  Fixes land once and reach both builds.
- **Cost.**  One row a second per station; about 0.4 MB of backlog per
  station on the relay.

## The link

- **Outbound only.**  The station opens `wss://<relay>/live/ingest`
  through port 443, so it works wherever uploads already work.  Nothing
  listens on the station.
- **Identity.**  The relay sends a random nonce; the station signs it with
  `/etc/hs-uploader/keys/id_ed25519_host` (`ssh-keygen -Y sign -n
  live-stream`); the relay checks the signature against the registry.  The
  relay takes the station's name from the registry entry that matched,
  never from the rows.
- **Reconnects** on its own after a drop, with backoff, and stops every
  band channel while the link stays down.
- **Frames.**  Each row travels as one websocket binary message: a short
  header (magic `LS`, version, kind, time, first frequency, bin width,
  floor and step in dB), the feed name, then one byte per bin in 0.5 dB
  steps.  Sixteen streams at one row a second come to about 10 KB/s.
  A series row (kind 2, such as the magnetometer's) carries its values as
  float32 instead, at most sixteen: `mag` sends x, y, z and rt.

## Registration: one key, two scopes

Today a station registers its upload key with the fleet at install, and
the servers that take its uploads trust that key.  The prototype's relay
reads a hand-kept `allowed_signers` file instead.  The design:

1. The operator turns a feed on in the site profile.  Sigmond records it
   and adds the "live" scope to the station's registration, beside
   "upload".
2. The relay reads the same registry the upload servers read, and accepts
   only keys that hold the "live" scope.
3. Withdrawing a station stops its uploads and its live feeds together;
   turning off its last feed drops the "live" scope.

A station that registers and opts in then appears in the viewer's station
list with no change on any server.  Rob knows the upload registry best;
the open questions below ask him how the relay should read it.

## Budget and pause

Sigmond runs live-publisher with limits drawn from what SIGMA measured
(about 32 MB and 4.5 % of a core with sixteen streams):

- `CPUQuota=25%`, `MemoryMax=128M`, `Nice=10`, worker cores only;
- `OOMScoreAdjust=500`, so the kernel stops it long before any recorder;
- at most `max_bands` band channels at once (default 10; an operator or
  sigmond may lower it on a small host).

Sigmond pauses live-publisher by writing `/run/live-publisher/pause`:
on a satellite link while it runs short, or when the host's memory
pressure climbs.  live-publisher stops band channels first, then the
Doppler channels, and tells the relay the station has paused, so viewers
see "paused" rather than a frozen screen.

## Installing and running

- Repository `HamSCI/live-publisher`; catalog entry in sigmond, off by
  default, discretionary (not in the core profile).
- `install.sh` in the fleet's usual form: `ensure_uv`, a venv at
  `/opt/live-publisher/venv`, `uv sync` against the committed lock, with
  ka9q-python from the sibling checkout.
- One unit, `live-publisher.service`, running as `hsupload` (it reads the
  station key) in group `sigmond`.
- Configuration: radiod's status address, the station name and the relay
  address come from sigmond; the operator's choices sit in the site
  profile.
- `live-publisher inventory --json` and `validate --json`, per the
  contract, so `smd status` shows its feeds, link state and viewer-driven
  bands.

## The relay

- One asyncio process behind nginx at `/live/`: stations on
  `/live/ingest`, viewers on `/live/`.
- Ten minutes of every stream in memory, about 6 MB per station; thirty
  stations fit easily in 512 MB.
- Viewers may send only `{"station": ...}` and `{"band": ...}`.  The
  relay checks both names against what the station offers.
- nginx limits each address to eight connections.
- Memory only; no disk, no database.

## Non-goals

- Anything a viewer can tune or control (tier 2 of the policy).
- Audio, raw IQ, logs or configuration.
- Archives: the project's records travel the upload paths.
- Replacing ka9q-web, which stays local (tier 1).

## From prototype to client

1. Done 2026-11-25: the prototype lives in `prototype/` with its 24
   tests, the series frame and the feed socket.  Still to do in step 2:
   drop numpy from the spectrum path.
2. Add `install.sh`, `deploy.toml`, the unit and the contract verbs.
3. Sigmond: catalog entry, site-profile opt-in, limits, CPU-affinity
   entry, pause file, "live" scope at registration.
4. Server: the relay into wsprdaemon-server; registry instead of the
   hand-kept file.
5. SIGMA moves from the hand-started prototype onto the client.
6. The `mag` feed in mag-recorder, and gmag-webui's public build (relay
   source, station picker, CDN libraries, one-file build).  The feed
   shipped in mag-recorder f7b6f8e and runs on SIGMA; gmag-webui's public
   build waits on its maintainers' review.
7. Two stations with room to spare and willing operators, one of them
   with a magnetometer; then the release that offers it fleet-wide.

## Open questions

1. **Rob:** how should the relay read the upload registry: a file the
   registrar exports, a query, or something else?  And may the relay
   live in wsprdaemon-server?
2. **Both:** the magnetometer can join the first release if gmag-webui's
   changes stay as small as they look.  Agree?  And who in gmag-webui's
   project should review its public build?
3. **Both:** which page hosts the public view: the project site alone, or
   also each station's own station-web page?
4. **Phil:** worth knowing for radiod: a spectrum channel whose sample
   rate ÷ bin width leaves a large prime factor costs radiod far more than
   a round width does.
