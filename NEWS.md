# NEWS

## 0.1.0 — 2026-11-04

Initial independent GPSD acquisition adapter for Cartochron.

- Forward valid timestamped GPSD TPV 2D/3D fixes to Cartochron, retaining
  reported measurement time, provider and optional horizontal uncertainty.
- Support multiple configured GPSD inputs in one Python collector, with
  independently generated recording runs, per-input duplicate suppression,
  stable channel identifiers and optional enrolled Cartochron source IDs.
- Allow unassigned channels for temporary laboratory devices without inventing
  source enrollment or hardware identities.
- Provide a two-service Docker Compose deployment for GPSD and the collector,
  with bounded UDP input ranges, private credentials and restart policies.
- Preserve observation gaps across transport failures; do not replay ambiguous
  HTTP submissions. Detect a receiver that does not acknowledge a configured
  channel ID instead of silently discarding provenance.
- Include unit, registry, configuration and real HTTP/SQLite integration tests.

### Important boundaries

- GPSD device paths and unauthenticated NMEA/UDP channels identify acquisition
  inputs, not verified phones. Configure the network boundary accordingly.
- Fix time and uncertainty are producer reports, not independently established
  location truth. Exactly-once delivery is not guaranteed.
- Upgrade the Cartochron receiver before enabling this adapter's channel IDs.

See `docs/compose.md` for deployment and recovery procedures.
