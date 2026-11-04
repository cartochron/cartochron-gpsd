"""Optional real HTTP integration against a local Cartochron checkout.

Run: make check-integration CARTOCHRON_DIR=/path/to/cartochron
This creates a disposable SQLite database, a fake GPSD listener and a local
Cartochron receiver. No real GPS equipment or location data is used.
"""

from contextlib import closing
import base64
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

from cartochron_gpsd import Adapter, MultiAdapter, WATCH
from source_registry import Source


class CartochronIntegrationTest(unittest.TestCase):
    def test_gpsd_tcp_to_authenticated_receiver_and_sqlite_restart(self):
        cartochron = os.environ.get("CARTOCHRON_DIR")
        if not cartochron:
            self.skipTest("CARTOCHRON_DIR not set (use make check-integration)")
        cartochron = Path(cartochron).resolve()
        if not (cartochron / "map_server.py").is_file():
            self.fail("CARTOCHRON_DIR must point to a receiver checkout")
        sys.path.insert(0, str(cartochron))
        try:
            from map_server import Handler, Server
            from observation_store import ObservationStore
        finally:
            sys.path.remove(str(cartochron))

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observations.sqlite3"
            auth = "Basic " + base64.b64encode(b"operator:secret").decode("ascii")
            receiver = Server(("127.0.0.1", 0), Handler,
                              authorization=auth, database=database)
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            source_id = receiver.store.enroll("Synthetic GPSD fixture")
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                received = []

                def fake_gpsd():
                    client, _address = listener.accept()
                    with client:
                        received.append(client.recv(1024))
                        reports = [
                            {"class": "DEVICES", "devices": [{"path": "/dev/fake-gps"}]},
                            {"class": "WATCH", "enable": True, "json": True},
                            {"class": "TPV", "device": "/dev/fake-gps", "mode": 1},
                            {"class": "TPV", "device": "/dev/fake-gps", "mode": 3,
                             "time": "2026-11-03T13:00:00.250Z", "lat": 47.0,
                             "lon": 28.0, "eph": 5.5},
                            {"class": "TPV", "device": "/dev/fake-gps", "mode": 3,
                             "time": "2026-11-03T13:00:00.250Z", "lat": 47.0001,
                             "lon": 28.0001},
                            {"class": "TPV", "device": "/dev/fake-gps", "mode": 2,
                             "time": "2026-11-03T13:00:01.250Z", "lat": 47.0002,
                             "lon": 28.0002},
                        ]
                        client.sendall(b"".join(json.dumps(item).encode() + b"\r\n"
                                                for item in reports))
                producer = threading.Thread(target=fake_gpsd, daemon=True)
                producer.start()
                adapter = Adapter(endpoint=f"http://127.0.0.1:{receiver.server_port}/api/position",
                                  authorization=auth, source_id=source_id,
                                  track_id="gpsd-integration")
                try:
                    with socket.create_connection(listener.getsockname(), timeout=5) as connection:
                        adapter.consume(connection)
                    producer.join(timeout=5)
                    self.assertFalse(producer.is_alive(), "fake GPSD did not finish")
                    self.assertEqual(received, [WATCH])
                    self.assertEqual(adapter.counts["accepted"], 2)
                    self.assertEqual(adapter.counts["duplicate"], 1)
                    self.assertEqual(adapter.counts["unavailable"], 1)
                    listing = receiver.store.tracks()["tracks"]
                    self.assertEqual(listing[0]["track_id"], "gpsd-integration")
                    self.assertEqual(listing[0]["source_id"], source_id)
                    self.assertIsNone(listing[0]["channel_id"])
                finally:
                    receiver.shutdown()
                    receiver.server_close()
                    thread.join(timeout=5)
            # Read the saved data after the HTTP server and its connection close.
            store = ObservationStore(database)
            try:
                rows = store.history("gpsd-integration")["points"]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["lat"], 47.0)
                self.assertEqual(rows[0]["provider"], "gpsd")
                self.assertEqual(rows[0]["accuracy_m"], 5.5)
                self.assertEqual(rows[0]["measured_time"], "2026-11-03T13:00:00.250+00:00")
                self.assertNotIn("accuracy_m", rows[1])
                self.assertEqual(rows[1]["measured_time"], "2026-11-03T13:00:01.250+00:00")
            finally:
                store.close()


    def test_unassigned_lab_channels_write_separate_runs(self):
        """Blank source IDs are valid: they do not trigger automatic enrollment."""
        cartochron = os.environ.get("CARTOCHRON_DIR")
        if not cartochron:
            self.skipTest("CARTOCHRON_DIR not set")
        sys.path.insert(0, str(Path(cartochron).resolve()))
        try:
            from map_server import Handler, Server
        finally:
            sys.path.remove(str(Path(cartochron).resolve()))
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observations.sqlite3"
            receiver = Server(("127.0.0.1", 0), Handler, database=database)
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            collector = MultiAdapter([
                Source("oneplus", "udp://*:29998", None),
                Source("lg", "udp://*:29999", None)],
                endpoint=f"http://127.0.0.1:{receiver.server_port}/api/position")
            try:
                for path, lat in [("udp://*:29998", 47), ("udp://*:29999", 48)]:
                    collector.handle({"class": "TPV", "device": path, "mode": 3,
                                      "time": "2026-11-03T13:00:00Z", "lat": lat, "lon": 28})
                runs = receiver.store.tracks()["tracks"]
                self.assertEqual(len(runs), 2)
                self.assertTrue(all(r["source_id"] is None for r in runs))
                self.assertEqual({r["channel_id"] for r in runs}, {"oneplus", "lg"})
                self.assertEqual(receiver.store.sources()["sources"], [])
                self.assertEqual({p["track_id"] for p in runs},
                                 {a.track_id for a in collector.adapters.values()})
                self.assertEqual({r["track_id"]: receiver.store.history(r["track_id"])["points"][0]["lat"]
                                  for r in runs},
                                 {a.track_id: lat for a, lat in
                                  zip(collector.adapters.values(), (47, 48))})
            finally:
                receiver.shutdown()
                receiver.server_close()
                thread.join(timeout=5)

    def test_two_gpsd_devices_remain_distinct_in_real_receiver(self):
        """One WATCH connection and two input paths cannot merge source histories."""
        cartochron = os.environ.get("CARTOCHRON_DIR")
        if not cartochron:
            self.skipTest("CARTOCHRON_DIR not set")
        cartochron = Path(cartochron).resolve()
        sys.path.insert(0, str(cartochron))
        try:
            from map_server import Handler, Server
            from observation_store import ObservationStore
        finally:
            sys.path.remove(str(cartochron))

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observations.sqlite3"
            auth = "Basic " + base64.b64encode(b"operator:secret").decode("ascii")
            receiver = Server(("127.0.0.1", 0), Handler,
                              authorization=auth, database=database)
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            first = receiver.store.enroll("Synthetic OnePlus")
            second = receiver.store.enroll("Synthetic LG")
            collector = MultiAdapter([
                Source("oneplus", "udp://*:29998", first),
                Source("lg", "udp://*:29999", second),
            ], endpoint=f"http://127.0.0.1:{receiver.server_port}/api/position",
                authorization=auth)
            left, right = socket.socketpair()
            failures = []
            def stream():
                try:
                    with right:
                        if right.recv(1024) != WATCH:
                            raise AssertionError("no single WATCH subscription")
                        reports = [
                            {"class": "DEVICES", "devices": [
                                {"path": "udp://*:29998"}, {"path": "udp://*:29999"}]},
                            {"class": "TPV", "device": "udp://*:29998", "mode": 3,
                             "time": "2026-11-03T13:00:00Z", "lat": 47, "lon": 28},
                            {"class": "TPV", "device": "udp://*:29999", "mode": 3,
                             "time": "2026-11-03T13:00:00Z", "lat": 48, "lon": 29, "eph": 4},
                            {"class": "TPV", "device": "udp://*:29998", "mode": 3,
                             "time": "2026-11-03T13:00:00Z", "lat": 49, "lon": 30},
                        ]
                        right.sendall(b"".join(json.dumps(r).encode() + b"\r\n" for r in reports))
                except Exception as exc:
                    failures.append(exc)
            sender = threading.Thread(target=stream)
            sender.start()
            try:
                with left:
                    collector.consume(left)
                sender.join(timeout=3)
                self.assertFalse(sender.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(collector.adapters["udp://*:29998"].counts["accepted"], 1)
                self.assertEqual(collector.adapters["udp://*:29999"].counts["accepted"], 1)
                self.assertEqual(collector.adapters["udp://*:29998"].counts["duplicate"], 1)
                runs = receiver.store.tracks()["tracks"]
                self.assertEqual(len(runs), 2)
                self.assertEqual({r["source_id"] for r in runs}, {first, second})
                self.assertEqual({r["channel_id"] for r in runs}, {"oneplus", "lg"})
                self.assertEqual({r["track_id"] for r in runs},
                                 {a.track_id for a in collector.adapters.values()})
            finally:
                receiver.shutdown()
                receiver.server_close()
                thread.join(timeout=5)
            with closing(ObservationStore(database)) as store:
                rows = {r["source_id"]: store.history(r["track_id"])["points"]
                        for r in runs}
                self.assertEqual(rows[first][0]["lat"], 47)
                self.assertEqual(rows[second][0]["lat"], 48)
                self.assertEqual(rows[second][0]["accuracy_m"], 4)


if __name__ == "__main__":
    unittest.main()
