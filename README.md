# cartochron-gpsd

A small [GPSD](https://gpsd.io/) acquisition adapter for
[Cartochron](https://github.com/cartochron/cartochron). It subscribes to GPSD
TPV reports and posts observations to Cartochron's existing `/api/position`
endpoint. Cartochron can run **locally** while GPSD runs on a **VPS**, reached
through an SSH tunnel.

Python 3.11 or newer, standard library only. No GPS hardware, libgps bindings,
Metasploit, Docker, or extra service is needed to run the unit tests. This repository does not
install or change the Cartochron receiver.

## Deployment with multiple phones

For **two or more known GPSD inputs**, use the [two-service Docker Compose
walkthrough](docs/compose.md). It runs one GPSD and one `cartochron-gpsd`
collector, restarts them after crashes, and uses a persistent `sources.toml`
registry to map independent GPSD channels to optional enrolled Cartochron
sources. **No process or CLI invocation per phone.** The same registry selects
GPSD's UDP listeners and the collector's per-device recording state.

`sources.toml.example` starts with **empty source IDs** for experiments;
each configured channel gets a separate automatically named run, without
pretending to have persistent enrolled identity. To group subsequent runs by
source, enroll the channel once and configure its `src_...` identifier. An
empty or omitted source ID never prevents GPSD from starting.

The registry is configured once per channel, not each time a feed reconnects.
UDP does not authenticate the phone behind a port; the association is with an
input channel and must be protected at the network boundary.

Run `make check` for unit tests and `make check-integration
CARTOCHRON_DIR=../cartochron` for a synthetic two-device end-to-end test.
The original single-source CLI remains supported below.

## Quick start (single GPSD source)

Start the Cartochron receiver from its own checkout:

```sh
make -C ../cartochron              # Fetch verified Leaflet assets, if absent
python3 ../cartochron/map_server.py
```

If GPSD is on the same machine, start the adapter:

```sh
python3 cartochron_gpsd.py
```

Open <http://127.0.0.1:8765> to view observations. The receiver above uses
**in-memory history**, lost when the server stops. Use Cartochron's documented
`--db` option or Compose deployment for persistence.

The adapter creates a **new random recording-run ID** each time it starts,
prints it on startup, and keeps it across transient GPSD reconnects. To
associate separate runs with one operator-enrolled source, supply `--source-id`
(see below).

### GPSD on a VPS, Cartochron locally

Keep GPSD bound to the VPS loopback interface. From the **local** machine,
forward its TCP port over SSH:

```sh
ssh -N -L 127.0.0.1:12947:127.0.0.1:2947 operator@vps.example.com
```

In a second local terminal, start Cartochron and this adapter:

```sh
python3 cartochron_gpsd.py --gpsd-host 127.0.0.1 --gpsd-port 12947
```

This connection carries GPSD reports **through the SSH tunnel**, then writes
to the local Cartochron server over loopback HTTP. GPSD's TCP protocol does
not provide transport encryption or client authorization: **do not expose
port 2947 publicly**. If your VPS has no GNSS feed or valid fix, the adapter
will have nothing to submit; a working tunnel does not manufacture GPS data.

The GPSD device path, if needed, is the path as reported by **remote GPSD**,
not a path on the local computer:

```sh
python3 cartochron_gpsd.py --gpsd-port 12947 --device /dev/ttyUSB0
```

In single-source mode, if GPSD lists more than one device, `--device` is
required. For multiple registered devices, use `--sources sources.toml` instead.
Otherwise the adapter locks the recording run to the first identifiable device. It refuses
to silently switch that run to another device. An untagged TPV is accepted
only when GPSD advertises exactly one device.

## Authentication and source identity

A Cartochron Compose receiver requires Basic authentication. If running the
adapter on the same computer as Cartochron, use the receiver's existing
private `data/auth` file directly:

```sh
python3 cartochron_gpsd.py \
  --auth-file ../cartochron/data/auth \
  --gpsd-port 12947
```

The file contains one `user:password` line, has mode `0600`, and must be a
regular file (not a symlink). Never put the password in the URL or shell
arguments. For a remote Cartochron receiver, `--endpoint` requires HTTPS:

```sh
python3 cartochron_gpsd.py \
  --endpoint https://map.example.com/api/position \
  --auth-file ~/.config/cartochron-gpsd/auth
```

You can register a source against your persistent Cartochron database before
starting the adapter:

```sh
python3 ../cartochron/sources.py \
  --db "$HOME/.local/share/cartochron/observations.sqlite3" \
  add 'VPS GPSD receiver'
```

Use the returned `src_...` value as `--source-id`. With Compose, use the
receiver's documented `docker compose exec cartochron python3 sources.py ...`
form instead. **A source ID is not an authentication credential, and GPSD
identification is not proof of physical device ownership.**

An optional `--track-id` can set a single recording-run ID explicitly. Normally,
leave it unset: a new process should be a new run, while source identity
remains stable across runs.

## What is actually recorded?

This adapter uses GPSD's `?WATCH={"enable":true,"json":true}` stream, **not**
`?POLL`, which can return cached positions. It forwards only valid 2D/3D
`TPV` reports with coordinates and an explicit parseable, timezone-aware
measurement time. Requiring the timestamp lets us suppress multiple TPV
reports for the **same device/measurement epoch**, including old epochs seen
after a reconnect. A report without a usable timestamp is *not* forwarded.

Each accepted HTTP observation contains:

| Cartochron field | Origin |
| --- | --- |
| `lat`, `lon` | GPSD TPV reported coordinates |
| `measured_ms` | GPSD TPV `time`, converted to epoch milliseconds |
| `provider` | Literal `gpsd` (a producer claim, not proof) |
| `accuracy_m` | Optional GPSD `eph` horizontal error estimate |
| `track_id` | Random per adapter process, or operator-provided |
| `source_id` | Optional operator-enrolled ID |

Cartochron separately records **server receipt time**. GPSD's fix time may
be inaccurate before satellite synchronization, and its error estimate has
uncertain confidence; neither field should be interpreted as independently
verified. Unknown `eph` remains absent, never inferred from `epx`/`epy`.
See [GPSD's JSON protocol documentation](https://gpsd.io/gpsd_json.html).

Each configured source in multi-device mode gets its own independent
recording run and deduplication state. A new collector process starts new
runs, while a GPSD reconnect within that process retains them.

A disconnect creates a gap in observable history. We do not backfill it, hold
an offline queue, or resend an observation when an HTTP response is lost:
that would risk duplicate records. **Exactly-once delivery is not guaranteed**,
especially after restarting the adapter. Continuous reports with identical
coordinates but *different* epochs remain distinct observations.

## Operation and troubleshooting

The process logs startup, GPSD reconnects, failures and a summary; it does not
log coordinates or passwords. Ctrl-C stops it. It reconnects to GPSD every
three seconds when disconnected, with a 15-second read timeout to detect
silent connections.

If an HTTP observation receives `401`, `403`, `404`, `409`, or `422`, the
adapter stops: check credentials, `--source-id`, receiver URL, and the run's
source association. A transient HTTP failure is reported but the observation
is **not** retried. GPSD `mode: 1` means no fix; `SKY` messages do not contain
position observations. If no fixes appear, verify GPSD independently before
checking Cartochron.

For a first manual connection test to GPSD, you can observe the JSON stream
without this adapter:

```sh
printf '?WATCH={"enable":true,"json":true}\n' | nc 127.0.0.1 12947
```

`nc` is optional, only for manual diagnostics. On some systems it exits when
standard input closes; use a tool with persistent socket input if necessary.

## Testing

```sh
make check
```

This runs deterministic Python `unittest` tests using fake GPSD reports and
mocked HTTP outcomes. For a full local TCP → HTTP → SQLite test against a
**separate checkout** of Cartochron:

```sh
make check-integration CARTOCHRON_DIR=../cartochron
```

The integration test uses a temporary database, a local fake GPSD TCP server,
and a real Cartochron HTTP server. It exercises authenticated ingestion,
source association, deduplication and persistence after server restart.
No live GPS hardware, network access, or private coordinates are involved.

## Scope and license

This is one authorized GPSD acquisition adapter, not a tracking daemon,
position database, device enrollment authority, or remote-access service.
The Cartochron receiver and Metasploit adapter live in independent repositories.
Use only GPS devices and feeds for which you have authorization.

GPL-3.0-or-later; see `COPYING` and `COPYRIGHT`.
