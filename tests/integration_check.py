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

from cartochron_gpsd import Adapter, WATCH


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


if __name__ == "__main__":
    unittest.main()
