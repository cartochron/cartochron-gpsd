# Two-phone GPSD / Cartochron deployment (Linux Docker Engine)

This deployment runs **one GPSD and one Cartochron GPSD collector**. Cartochron
itself runs independently (native or in its own Compose project), listening on
host loopback `127.0.0.1:8765` with Basic authentication.

The collector uses Linux **host networking** only so that it can reach the
receiver's and GPSD's loopback-only published TCP endpoints. GPSD itself runs
in Docker's bridge network. Do not use this Compose configuration unchanged on
Docker Desktop or a shared/untrusted Docker host.

## First-time setup

1. Start an authenticated, persistent Cartochron receiver using
   [Cartochron's operations guide](https://github.com/cartochron/cartochron/blob/sources-identities/docs/operations.md).
   **For experiments, source enrollment is optional.** Empty or absent
   `source_id` fields create separate *unassigned* runs, with IDs like
   `oneplus-<random suffix>`. They are still separate recording runs but are
   not permanently associated with an enrolled source.

   When you want persistent source grouping across collector restarts, enroll
   the input channels once (while Cartochron is running):

   ```sh
   docker compose exec cartochron python3 sources.py --db /data/observations.sqlite3 add 'OnePlus'
   docker compose exec cartochron python3 sources.py --db /data/observations.sqlite3 add 'LG'
   ```

   The enrollment commands above run from the **Cartochron** repository. Copy
   the returned `src_...` values into the `sources.toml` entries before
   starting a new collector run. Enrollment is not required for temporary
   phones, and no UDP sender can create an enrolled source by itself.

2. In the `cartochron-gpsd` repository:

   ```sh
   cp sources.toml.example sources.toml
   cp .env.example .env
   ```

   The example registry works as-is for two *unassigned* lab inputs. Its
   `[sources.oneplus]` and `[sources.lg]` keys are stable channel IDs: each
   new collector run has a fresh `track_id` but keeps the same `channel_id`.
   When multiple collectors send to one receiver, configure distinct IDs
   such as `channel_id = "labA.oneplus"`. This is an input association, not a
   verified phone identity. Upgrade Cartochron to schema v4 **first**, after
   backing up its SQLite database; older receivers ignore the new field.

   ```toml
   [sources.oneplus]
   device = "udp://*:29998"
   source_id = ""  # unassigned; may also be omitted

   [sources.lg]
   device = "udp://*:29999"
   source_id = ""
   ```

   For an enrolled input, replace its empty value with the actual registered
   `src_...` ID. Restart the collector to begin a new run with that source
   association; previously recorded unassigned runs stay unassigned.

3. Edit `.env`: set `GPSD_BIND_IP` to your host's **trusted LAN IPv4 address**,
   such as `192.168.1.20`, so your authorized phones can send NMEA over UDP.
   Leave it `127.0.0.1` for localhost-only synthetic tests. Set
   `COLLECTOR_UID` and `COLLECTOR_GID` to the results of `id -u` and `id -g`;
   these must be allowed to read the Cartochron authentication file (mode
   `0600`). The default `CARTOCHRON_AUTH_FILE` assumes sibling checkouts.

4. Stop any existing standalone `docker-gpsd` container using the same ports.
   Bring up the new stack:

   ```sh
   make check-config           # Validates source registry, without starting GPSD
   docker compose config -q
   docker compose up --build -d
   docker compose ps
   docker compose logs -f gpsd collector
   ```

   `make check-config` uses the default `29998-30017` range; if your `.env`
   uses a different range, pass it explicitly as
   `make check-config GPSD_UDP_PORT_RANGE=FIRST-LAST`.

   Point the OnePlus forwarder to `GPSD_BIND_IP:29998/udp` and the LG forwarder
   to `GPSD_BIND_IP:29999/udp`. **Never direct two phones to the same input
   port**: GPSD cannot authenticate or reliably separate two senders whose
   NMEA sentences enter one socket. The UDP feeds must be limited to trusted
   devices/network paths, preferably an authenticated VPN on a VPS.

   Verify GPSD independently:

   ```sh
   docker compose exec gpsd gpspipe -w -n 30
   ```

   Look for TPV reports with `device` matching the configured `udp://*` paths.
   If the paths reported by your GPSD build differ, investigate the actual
   device representation before trusting source attribution. Do not guess.

## Restart, failure and continuity

Both services use `restart: unless-stopped`. A crashed GPSD or collector
restarts automatically under Docker Engine. GPSD interruptions cause gaps:
Cartochron does not invent movement between observations. A collector restart
creates **a new run per registered source**, preserving each source ID. A
transient GPSD reconnect within a running collector retains each source's run
ID and last measurement epoch.

```sh
docker compose restart gpsd
docker compose restart collector
docker compose logs --tail=100 collector
```

For a crash rehearsal, use `docker kill` on the relevant test container and
check `docker compose ps` and the logs. Ordinary `docker compose stop` is an
intentional stop and disables automatic restart until `docker compose start`.

A wrong receiver credential, malformed nonempty source ID, or unrecognized
source ID causes a configuration failure. `restart: unless-stopped` also
restarts a container on *permanent* configuration errors: repeated short logs
are not proof of GPSD/network failure. **Stop both services before correcting
`source_id` or port configuration**:

```sh
docker compose stop gpsd collector
# Edit sources.toml or .env; source_id = "" is allowed for lab use.
docker compose up -d
```

For a receiver HTTP 401/422, correct the credentials or enrollment before
restarting. The collector does not queue or replay unacknowledged observations.
The named registry source identifies a UDP input channel, not an authenticated
physical phone.

## Growing beyond two input channels

`sources.toml` configures **both** GPSD's UDP inputs and the collector's source
routing. Adding an entry does not require another Python process or another
Docker service. The default Compose UDP publication range is ports
`29998-30017` (20 available inputs). Only registered ports have GPSD listeners.

To support, for example, 100 individually configured sources, change
`GPSD_UDP_PORT_RANGE=29998-30097` in `.env`, add the matching registry entries,
and recreate the containers with `docker compose up -d`. Do not expose a large
range unless you actually need it. Practical throughput, GPSD device limits,
and host firewall behavior must be measured under live load; synthetic tests
are not a 100-device deployment qualification.

**Important authority boundary:** A registry associates one *input channel*
with one operator-enrolled Cartochron source. It cannot establish which
physical phone is transmitting to an unauthenticated UDP socket. DHCP, NAT,
spoofing, or port reuse can lead to incorrect attribution. Secure onboarding
of unknown devices would require authenticated transport and a separate,
trusted enrollment mechanism; it is not part of this deployment.

For a GPSD service already running on a VPS, the existing single-device CLI
and SSH-forwarded TCP connection remain supported; see the main README.
