"""Protect GPSD provenance, single-device ownership, and transport semantics."""

import base64
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cartochron_gpsd import (Adapter, ConfigurationError, MAX_LINE, WATCH,
                             endpoint_url, measured_milliseconds, number,
                             read_auth_file, run, tpv_observation)


T0 = "2026-11-03T13:00:00.250Z"
T1 = "2026-11-03T13:00:01.250Z"


def tpv(**changes):
    return {"class": "TPV", "mode": 3, "device": "/dev/ttyUSB0", "time": T0,
            "lat": 47.0, "lon": 28.0, "eph": 7.5, **changes}


class ValidationTest(unittest.TestCase):
    def test_tpv_requires_real_fix_and_reported_epoch(self):
        valid = tpv_observation(tpv())
        self.assertEqual(valid, {"lat": 47.0, "lon": 28.0,
                                 "measured_ms": 1793710800250,
                                 "provider": "gpsd", "accuracy_m": 7.5})
        for bad in (tpv(mode=0), tpv(mode=1), tpv(mode=True), tpv(mode=2, lat=None),
                    tpv(lon=181), tpv(lat=float("nan")), tpv(lat=True),
                    tpv(time=None), tpv(time="2026-11-03T13:00:00"),
                    tpv(time="invalid"), tpv(time="1960-01-01T00:00:00Z"),
                    tpv(**{"class": "SKY"})):
            with self.subTest(bad=bad):
                self.assertIsNone(tpv_observation(bad))

    def test_2d_fix_without_eph_keeps_unknown_accuracy(self):
        observation = tpv_observation(tpv(mode=2, eph=float("inf")))
        self.assertEqual(observation["provider"], "gpsd")
        self.assertNotIn("accuracy_m", observation)
        self.assertEqual(tpv_observation(tpv(eph=100001))["provider"], "gpsd")
        self.assertNotIn("accuracy_m", tpv_observation(tpv(eph=-1)))

    def test_zero_coordinates_are_valid_when_mode_indicates_fix(self):
        self.assertEqual(tpv_observation(tpv(lat=0, lon=0))["lat"], 0)
        self.assertIsNone(number(float("inf"), -90, 90))

    def test_rfc3339_time_is_utc_normalized_and_has_millisecond_precision(self):
        self.assertEqual(measured_milliseconds("2026-11-03T15:00:00.250+02:00"),
                         measured_milliseconds(T0))
        self.assertEqual(measured_milliseconds("1970-01-01T00:00:00.001Z"), 1)
        self.assertIsNone(measured_milliseconds("99999-01-01T00:00:00Z"))
        self.assertIsNone(measured_milliseconds("not a time"))

    def test_endpoint_validation_prevents_remote_cleartext_and_url_credentials(self):
        self.assertEqual(endpoint_url("http://127.0.0.1:8765/api/position"),
                         "http://127.0.0.1:8765/api/position")
        self.assertEqual(endpoint_url("https://gps.example.test/api/position"),
                         "https://gps.example.test/api/position")
        for bad in ("http://gps.example.test/api/position", "http://user:secret@127.0.0.1/api/position",
                    "https://gps.example.test/api/position?token=secret",
                    "https://gps.example.test/other", "ftp://host/api/position",
                    "http://127.0.0.1:99999/api/position", "http://127.0.0.1/api/position#x"):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                endpoint_url(bad)

    def test_private_authentication_file_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth"
            path.write_text("operator:secret\n", encoding="ascii")
            path.chmod(0o600)
            self.assertEqual(read_auth_file(path),
                             "Basic " + base64.b64encode(b"operator:secret").decode())
            path.chmod(0o644)
            with self.assertRaisesRegex(ConfigurationError, "private"):
                read_auth_file(path)
            path.chmod(0o600)
            link = Path(directory) / "alias"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_auth_file(link)
            path.write_text("operator\n", encoding="ascii")
            with self.assertRaisesRegex(ConfigurationError, "user:password"):
                read_auth_file(path)
            path.write_text("operator:secret\nignored:second\n", encoding="ascii")
            with self.assertRaisesRegex(ConfigurationError, "user:password"):
                read_auth_file(path)


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.adapter = Adapter(endpoint="http://127.0.0.1:8765/api/position",
                               track_id="demo-run", send=self.sent.append)

    def test_one_gpsd_epoch_is_one_observation_even_if_coordinates_change(self):
        self.adapter.handle(tpv())
        self.adapter.handle(tpv(lat=48))
        self.adapter.handle(tpv(time=T1, lat=47.001))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual([x["lat"] for x in self.sent], [47.0, 47.001])
        self.assertEqual(self.adapter.counts["duplicate"], 1)

    def test_disconnect_does_not_reset_epoch_watermark(self):
        self.adapter.handle(tpv(time=T1))
        self.adapter.handle({"class": "WATCH", "enable": True, "json": True})
        self.adapter.handle(tpv())
        self.adapter.handle(tpv(time=T1))
        self.assertEqual(len(self.sent), 1)

    def test_no_fix_sky_and_malformed_samples_do_not_become_observations(self):
        self.adapter.handle({"class": "SKY", "satellites": [1]})
        self.adapter.handle(tpv(mode=1))
        self.adapter.handle(tpv(lat=None))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.adapter.counts["unavailable"], 2)

    def test_source_id_is_not_guessed_from_gpsd_device(self):
        self.adapter.handle(tpv())
        self.assertNotIn("source_id", self.sent[0])
        self.assertEqual(self.sent[0]["track_id"], "demo-run")
        source_id = "src_" + "a" * 24
        other = Adapter(endpoint=self.adapter.endpoint, source_id=source_id,
                        send=self.sent.append)
        other.handle(tpv())
        self.assertEqual(self.sent[-1]["source_id"], source_id)
        self.assertNotEqual(other.track_id, self.adapter.track_id)

    def test_multiple_devices_require_explicit_selection(self):
        with self.assertRaisesRegex(ConfigurationError, "multiple GPSD devices"):
            self.adapter.handle({"class": "DEVICES", "devices": [
                {"path": "/dev/ttyUSB0"}, {"path": "/dev/ttyUSB1"}]})
        selected = Adapter(endpoint=self.adapter.endpoint, device="/dev/ttyUSB1",
                           send=self.sent.append)
        selected.handle({"class": "DEVICES", "devices": [
            {"path": "/dev/ttyUSB0"}, {"path": "/dev/ttyUSB1"}]})
        selected.handle(tpv())
        selected.handle(tpv(device="/dev/ttyUSB1"))
        self.assertEqual(len(self.sent), 1)

    def test_late_second_device_fails_closed_not_merged(self):
        self.adapter.handle(tpv())
        with self.assertRaisesRegex(ConfigurationError, "second GPSD device"):
            self.adapter.handle(tpv(device="/dev/ttyUSB1", time=T1))
        self.assertEqual(len(self.sent), 1)

    def test_device_replacement_cannot_silently_reassign_run(self):
        self.adapter.handle({"class": "DEVICES", "devices": [{"path": "/dev/ttyUSB0"}]})
        self.adapter.handle(tpv())
        with self.assertRaisesRegex(ConfigurationError, "device changed"):
            self.adapter.handle({"class": "DEVICES", "devices": [{"path": "/dev/ttyUSB1"}]})
        self.assertEqual(len(self.sent), 1)

    def test_untagged_report_allowed_only_with_one_advertised_device(self):
        untagged = tpv()
        del untagged["device"]
        self.adapter.handle(untagged)
        self.assertFalse(self.sent)
        self.adapter.handle({"class": "DEVICES", "devices": [{"path": "/dev/ttyUSB0"}]})
        self.adapter.handle(untagged)
        self.assertEqual(len(self.sent), 1)

    def test_duplicate_marked_before_failed_submit_to_avoid_ambiguous_replay(self):
        def fail(_observation):
            raise ConnectionError("lost response")
        self.adapter.send = fail
        self.adapter.handle(tpv())
        self.adapter.send = self.sent.append
        self.adapter.handle(tpv())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.adapter.counts["errors"], 1)
        self.assertEqual(self.adapter.counts["duplicate"], 1)
        self.adapter.handle(tpv(time=T1))
        self.assertEqual(len(self.sent), 1)

    def test_repeated_http_errors_log_once_until_recovered(self):
        def unavailable(_observation):
            raise ConnectionError("receiver unavailable")
        self.adapter.send = unavailable
        with self.assertLogs("cartochron-gpsd", level="INFO") as messages:
            self.adapter.handle(tpv())
            self.adapter.handle(tpv(time=T1))
            self.adapter.send = self.sent.append
            self.adapter.handle(tpv(time="2026-11-03T13:00:02.250Z"))
        self.assertEqual(len([m for m in messages.output if "not acknowledged" in m]), 1)
        self.assertEqual(len([m for m in messages.output if "ingestion recovered" in m]), 1)
        self.assertEqual(self.adapter.counts["errors"], 2)
        self.assertEqual(self.adapter.counts["accepted"], 1)

    def test_repeated_gpsd_unavailability_does_not_flood_logs(self):
        attempts = []
        def unavailable(address, timeout):
            attempts.append(address)
            raise ConnectionRefusedError
        def sleep(delay):
            if len(attempts) == 3:
                raise KeyboardInterrupt
        with self.assertLogs("cartochron-gpsd", level="INFO") as messages:
            with self.assertRaises(KeyboardInterrupt):
                run(self.adapter, "127.0.0.1", 2947, connect=unavailable, sleep=sleep)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len([m for m in messages.output if "GPSD unavailable" in m]), 1)
        self.assertEqual(len([m for m in messages.output if "Stopped" in m]), 1)

    def test_unrecognized_device_and_gpsd_error_fail_or_skip_without_leaking(self):
        self.adapter.handle({"class": "VERSION", "release": "3.25"})
        with self.assertRaises(ConfigurationError):
            self.adapter.handle({"class": "WATCH", "enable": False})
        with self.assertRaises(ConfigurationError):
            self.adapter.handle({"class": "ERROR", "message": "private-data"})

    def test_reject_invalid_ids_before_connect(self):
        with self.assertRaisesRegex(ConfigurationError, "track ID"):
            Adapter(endpoint=self.adapter.endpoint, track_id="bad id")
        with self.assertRaisesRegex(ConfigurationError, "source ID"):
            Adapter(endpoint=self.adapter.endpoint, source_id="not-enrolled")

    def test_consume_sends_watch_and_skips_malformed_lines(self):
        class Socket:
            def __init__(self):
                self.content = io.BytesIO(b'{invalid json\r\n' + json.dumps(tpv()).encode() + b'\r\n')
                self.command = None
                self.timeout = None
            def settimeout(self, timeout): self.timeout = timeout
            def sendall(self, data): self.command = data
            def makefile(self, mode):
                assert mode == "rb"
                return closing_stream(self.content)
        class closing_stream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self.stream
            def __exit__(self, *_): return None
        sock = Socket()
        self.adapter.consume(sock)
        self.assertEqual(sock.command, WATCH)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sock.timeout, 15)

    def test_oversized_report_closes_connection_before_forwarding(self):
        class Socket:
            def settimeout(self, _value): pass
            def sendall(self, _value): pass
            def makefile(self, _mode): return io.BytesIO(b'x' * (MAX_LINE + 10) + b'\n')
        with self.assertRaisesRegex(ConnectionError, "oversized"):
            self.adapter.consume(Socket())
        self.assertEqual(self.sent, [])

    def test_run_reconnects_without_forgetting_duplicate_watermark(self):
        class Socket:
            def __init__(self): self.content = io.BytesIO(json.dumps(tpv()).encode() + b'\r\n')
            def __enter__(self): return self
            def __exit__(self, *_): self.content.close()
            def settimeout(self, _): pass
            def sendall(self, cmd): self.last_command = cmd
            def makefile(self, _): return closing(self.content)
        connections = []
        def connect(address, timeout):
            self.assertEqual(address, ("127.0.0.1", 2947))
            self.assertEqual(timeout, 5)
            sock = Socket()
            connections.append(sock)
            return sock
        def sleep(seconds):
            self.assertEqual(seconds, 3)
            if len(connections) >= 2:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run(self.adapter, "127.0.0.1", 2947, connect=connect, sleep=sleep)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.adapter.counts["duplicate"], 1)
        self.assertEqual(connections[0].last_command, WATCH)


class HttpTransportTest(unittest.TestCase):
    def test_authorization_and_exact_201_acknowledgment(self):
        sent = []
        class Response:
            def __init__(self, status): self.status = status
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return b'{}'
        def urlopen(request, timeout):
            sent.append((request, timeout))
            return Response(201)
        adapter = Adapter(endpoint="https://example.test/api/position", authorization="Basic dGVzdA==")
        with patch("cartochron_gpsd.urlopen", urlopen):
            adapter.handle(tpv())
        request, timeout = sent[0]
        self.assertEqual(timeout, 5)
        self.assertEqual(request.get_header("Authorization"), "Basic dGVzdA==")
        self.assertEqual(json.loads(request.data)["provider"], "gpsd")
        self.assertEqual(adapter.counts["accepted"], 1)

    def test_http_401_terminates_instead_of_repeating_forever(self):
        def urlopen(req, timeout):
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        adapter = Adapter(endpoint="https://example.test/api/position")
        with patch("cartochron_gpsd.urlopen", urlopen), self.assertRaisesRegex(ConfigurationError, "HTTP 401"):
            adapter.handle(tpv())
        self.assertEqual(adapter.counts["rejected"], 1)
        self.assertEqual(adapter.counts["accepted"], 0)

    def test_transient_http_failure_is_not_retried(self):
        def urlopen(req, timeout):
            raise HTTPError(req.full_url, 503, "Unavailable", {}, None)
        adapter = Adapter(endpoint="https://example.test/api/position")
        with patch("cartochron_gpsd.urlopen", urlopen):
            adapter.handle(tpv())
        self.assertEqual(adapter.counts["errors"], 1)
        self.assertEqual(adapter.counts["accepted"], 0)


if __name__ == "__main__":
    unittest.main()
