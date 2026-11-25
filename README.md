# live-publisher

Sends a sigmond station's live, public data (Doppler strips, band
waterfalls, later the magnetometer) out to a relay that browsers read.
Outbound only: nothing on the station listens, and no viewer reaches
radiod.  Runs as a sigmond client, off by default; the operator turns
each feed on.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Policy it follows: sigmond's
  [public data policy](https://github.com/HamSCI/sigmond/blob/main/docs/public-data-policy.md)

Status: design under review.  A prototype runs on AC0G-S.
