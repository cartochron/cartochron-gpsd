#!/usr/bin/env python3
"""Forward fresh GPSD TPV epochs to a Cartochron observation receiver.

Single-source mode owns one recording run; registry mode owns one per device.
Neither mode polls cached GPSD locations, replays failed HTTP submissions,
nor asserts physical device identity on Cartochron's behalf.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from source_registry import RegistryError, load_sources

LOG = logging.getLogger("cartochron-gpsd")
WATCH = b'?WATCH={"enable":true,"json":true}\n'
MAX_LINE = 16 * 1024
RECONNECT_SECONDS = 3
TRACK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SOURCE_ID = re.compile(r"src_[0-9a-f]{24}\Z")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ConfigurationError(ValueError):
    """A misconfiguration requiring operator intervention, not a retry."""


def endpoint_url(value):
    """Require HTTPS for remote receivers, or HTTP to explicit loopback hosts."""
    try:
        url = urlsplit(value)
        port = url.port  # Validate invalid port text before starting the stream.
    except ValueError as exc:
        raise ConfigurationError("invalid Cartochron endpoint") from exc
    if (url.scheme not in ("http", "https") or not url.hostname or
            url.path != "/api/position" or url.query or url.fragment or
            url.username is not None or url.password is not None):
        raise ConfigurationError("endpoint must be an http(s) /api/position URL without credentials")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigurationError("remote Cartochron receiver requires HTTPS")
    return value


def read_auth_file(filename):
    """Read one private user:password line; never expose credentials in logs."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(filename, flags)
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigurationError("auth file must be a private regular file (chmod 600)")
        content = stream.read(1025)
    if len(content) > 1024:
        raise ConfigurationError("auth file is too long")
    try:
        line = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("auth file must contain ASCII credentials") from exc
    if line.endswith("\n"):
        line = line[:-1]
    if ("\n" in line or "\r" in line or ":" not in line or
            not line.split(":", 1)[0] or not line.split(":", 1)[1]):
        raise ConfigurationError("auth file must contain one user:password line")
    return "Basic " + base64.b64encode(line.encode("ascii")).decode("ascii")


def measured_milliseconds(value):
    """Convert GPSD's timezone-qualified ISO8601 timestamp without float drift."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        delta = timestamp.astimezone(timezone.utc) - EPOCH
        milliseconds = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    except (ValueError, OverflowError):
        return None
    return milliseconds if 0 <= milliseconds <= 253402300799999 else None


def number(value, low, high):
    """Reject JSON booleans, NaN, infinities and out-of-range measurements."""
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def tpv_observation(report):
    """Only valid 2D/3D fixes with a reported epoch can be forwarded safely.

    An epoch is necessary to avoid multiplying partial/replayed GPSD reports.
    We omit unknown error estimates rather than inventing accuracy.
    """
    if report.get("class") != "TPV" or type(report.get("mode")) is not int or report["mode"] not in (2, 3):
        return None
    lat = number(report.get("lat"), -90, 90)
    lon = number(report.get("lon"), -180, 180)
    measured_ms = measured_milliseconds(report.get("time"))
    if lat is None or lon is None or measured_ms is None:
        return None
    observation = {"lat": lat, "lon": lon, "measured_ms": measured_ms, "provider": "gpsd"}
    accuracy = number(report.get("eph"), 0, 100000)
    if accuracy is not None:
        observation["accuracy_m"] = accuracy
    return observation


class Adapter:
    """Keep the receiver protocol separate from GPSD's streaming lifecycle."""

    def __init__(self, *, endpoint, authorization=None, track_id=None, source_id=None, device=None,
                 send=None):
        self.endpoint = endpoint_url(endpoint)
        self.authorization = authorization
        self.track_id = track_id or str(uuid4())
        if not TRACK_ID.fullmatch(self.track_id):
            raise ConfigurationError("invalid track ID")
        if source_id is not None and not SOURCE_ID.fullmatch(source_id):
            raise ConfigurationError("invalid enrolled source ID")
        self.source_id = source_id
        self.device = device
        self.selected_device = device
        self.available_devices = None
        self.last_epoch = None
        self.counts = {"accepted": 0, "rejected": 0, "unavailable": 0, "errors": 0, "duplicate": 0}
        self._http_state = None
        self.send = send or self._send

    def _send(self, observation):
        request = Request(self.endpoint, method="POST",
                          data=json.dumps(observation, separators=(",", ":")).encode("ascii"),
                          headers={"Content-Type": "application/json"})
        if self.authorization:
            request.add_header("Authorization", self.authorization)
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
                response.read(4096)
        except HTTPError as exc:
            status = exc.code
        except (URLError, OSError, TimeoutError) as exc:
            raise ConnectionError(type(exc).__name__) from exc
        if status != 201:
            if status in (400, 401, 403, 404, 409, 422):
                raise ConfigurationError(f"receiver rejected observation (HTTP {status}); check credentials, source enrollment and endpoint")
            raise ConnectionError(f"HTTP {status}")

    def handle(self, report):
        """Handle one GPSD JSON object; no network read or artificial polling."""
        if not isinstance(report, dict):
            return
        kind = report.get("class")
        if kind == "ERROR":
            raise ConfigurationError("GPSD reported a protocol error")
        if kind == "WATCH" and (report.get("enable") is False or report.get("json") is False):
            raise ConfigurationError("GPSD did not enable JSON WATCH")
        if kind == "DEVICES":
            devices = report.get("devices")
            if isinstance(devices, list):
                paths = {entry.get("path") for entry in devices if isinstance(entry, dict) and
                         isinstance(entry.get("path"), str)}
                self.available_devices = paths
                if self.device is None and len(paths) > 1:
                    raise ConfigurationError("multiple GPSD devices; select one with --device")
                if self.device is None and len(paths) == 1:
                    only = next(iter(paths))
                    if self.selected_device is not None and self.selected_device != only:
                        raise ConfigurationError("GPSD device changed; start a new recording run")
                    self.selected_device = only
            return
        if kind != "TPV":
            return
        device = report.get("device")
        if not isinstance(device, str):
            # Untagged TPV can only be attributed when exactly one device was advertised.
            if self.available_devices is None or len(self.available_devices) != 1:
                self.counts["unavailable"] += 1
                return
            device = next(iter(self.available_devices))
        if self.device is not None and device != self.device:
            return
        if self.selected_device is None:
            self.selected_device = device
        elif self.device is None and device != self.selected_device:
            raise ConfigurationError("second GPSD device observed; select one with --device")
        observation = tpv_observation(report)
        if observation is None:
            self.counts["unavailable"] += 1
            return
        epoch = observation["measured_ms"]
        if self.last_epoch is not None and epoch <= self.last_epoch:
            self.counts["duplicate"] += 1
            return
        # Mark before POST: an ambiguous HTTP failure must not be replayed.
        self.last_epoch = epoch
        observation["track_id"] = self.track_id
        if self.source_id is not None:
            observation["source_id"] = self.source_id
        try:
            self.send(observation)
        except ConfigurationError:
            self.counts["rejected"] += 1
            raise
        except ConnectionError as exc:
            self.counts["errors"] += 1
            if self._http_state != "error":
                LOG.warning("Observation not acknowledged (%s); not replaying it", type(exc).__name__)
            self._http_state = "error"
        else:
            self.counts["accepted"] += 1
            if self._http_state == "error":
                LOG.info("Cartochron ingestion recovered")
            self._http_state = "accepted"

    def summary(self):
        return " ".join(f"{name}={count}" for name, count in self.counts.items())

    def description(self):
        return f"run={self.track_id} source={self.source_id or 'unassigned'}"

    def consume(self, connection):
        """Subscribe once and read bounded newline-delimited GPSD reports."""
        consume_reports(connection, self.handle)


def consume_reports(connection, handle):
    """Read GPSD reports through one bounded WATCH subscription."""
    connection.settimeout(15)
    connection.sendall(WATCH)
    with connection.makefile("rb") as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                raise ConnectionError("oversized GPSD report")
            try:
                report = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                LOG.warning("Skipping malformed GPSD report")
                continue
            handle(report)


MAX_UNKNOWN = 16  # Bound diagnostic state even with hostile device names.


class MultiAdapter:
    """One WATCH subscription; independent run and epoch state per GPSD input."""

    def __init__(self, sources, *, endpoint, authorization=None, send=None):
        if not sources:
            raise ConfigurationError("at least one registered GPSD source is required")
        self.adapters = {
            source.device: Adapter(endpoint=endpoint, authorization=authorization,
                                   source_id=source.source_id, device=source.device, send=send)
            for source in sources
        }
        if len(self.adapters) != len(sources):
            raise ConfigurationError("duplicate GPSD device")
        self.unknown = set()
        self.sources = sources

    def description(self):
        return f"registered_sources={len(self.adapters)}"

    def summary(self):
        totals = {key: 0 for key in next(iter(self.adapters.values())).counts}
        for adapter in self.adapters.values():
            for key, count in adapter.counts.items():
                totals[key] += count
        return " ".join(f"{name}={count}" for name, count in totals.items())

    def handle(self, report):
        if not isinstance(report, dict):
            return
        kind = report.get("class")
        if kind in ("ERROR", "WATCH"):
            next(iter(self.adapters.values())).handle(report)
            return
        if kind != "TPV":
            return
        device = report.get("device")
        if not isinstance(device, str) or device not in self.adapters:
            key = device if isinstance(device, str) else None
            if key not in self.unknown and len(self.unknown) < MAX_UNKNOWN:
                LOG.warning("Ignoring unregistered or untagged GPSD input")
                self.unknown.add(key)
            return
        self.adapters[device].handle(report)

    def consume(self, connection):
        consume_reports(connection, self.handle)


def run(adapter, host, port, *, connect=socket.create_connection, sleep=time.sleep):
    """Retry GPSD transport failures; configuration errors stop the process."""
    LOG.info("Started %s", adapter.description())
    if isinstance(adapter, MultiAdapter):
        for source in adapter.sources:
            run_id = adapter.adapters[source.device].track_id
            LOG.info("Registered %s run=%s source=%s", source.name, run_id, source.source_id)
    state = None
    try:
        while True:
            try:
                with connect((host, port), timeout=5) as connection:
                    if state != "connected":
                        LOG.info("GPSD connected")
                    state = "connected"
                    adapter.consume(connection)
                if state != "disconnected":
                    LOG.warning("GPSD disconnected; observation gap")
                state = "disconnected"
            except ConfigurationError:
                raise
            except (OSError, ConnectionError) as exc:
                if state != "disconnected":
                    LOG.warning("GPSD unavailable (%s); observation gap", type(exc).__name__)
                state = "disconnected"
            sleep(RECONNECT_SECONDS)
    finally:
        LOG.info("Stopped %s %s", adapter.description(), adapter.summary())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpsd-host", default="127.0.0.1", help="GPSD host (use SSH forwarding for a VPS)")
    parser.add_argument("--gpsd-port", type=int, default=2947, help="GPSD TCP port")
    parser.add_argument("--device", help="GPSD device path (single-source mode)")
    parser.add_argument("--sources", help="TOML GPSD device -> enrolled source registry (multi-source mode)")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/api/position")
    parser.add_argument("--auth-file", help="private file containing Cartochron user:password")
    parser.add_argument("--source-id", help="existing Cartochron-enrolled source ID")
    parser.add_argument("--track-id", help="recording run ID (random per invocation by default)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[cartochron-gpsd] %(levelname)s %(message)s")
    try:
        if not 1 <= args.gpsd_port <= 65535:
            raise ConfigurationError("GPSD port must be 1..65535")
        authorization = read_auth_file(args.auth_file) if args.auth_file else None
        if args.sources:
            if args.device is not None or args.source_id is not None or args.track_id is not None:
                raise ConfigurationError("--sources cannot be combined with --device, --source-id or --track-id")
            adapter = MultiAdapter(load_sources(args.sources), endpoint=args.endpoint,
                                   authorization=authorization)
        else:
            adapter = Adapter(endpoint=args.endpoint, authorization=authorization,
                              track_id=args.track_id, source_id=args.source_id, device=args.device)
        run(adapter, args.gpsd_host, args.gpsd_port)
    except (ConfigurationError, RegistryError, OSError) as exc:
        parser.exit(1, f"cartochron-gpsd: {exc}\n")
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
