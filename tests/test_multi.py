"""Independent sources must never share run identity or duplicate suppression."""

import json
import socket
import threading
import unittest
from unittest.mock import patch

from cartochron_gpsd import ConfigurationError, MultiAdapter, WATCH
from source_registry import Source

A = 'src_' + 'a' * 24
B = 'src_' + 'b' * 24
T0 = '2026-11-03T13:00:00.250Z'
T1 = '2026-11-03T13:00:01.250Z'


def tpv(device, epoch=T0, lat=47):
    return {'class': 'TPV', 'device': device, 'mode': 3, 'lat': lat,
            'lon': 28, 'time': epoch, 'eph': 5}


class MultiTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.adapter = MultiAdapter([
            Source('a', 'udp://*:29998', A), Source('b', 'udp://*:29999', B)],
            endpoint='http://127.0.0.1:8765/api/position', send=self.sent.append)

    def test_isolated_runs_and_epochs(self):
        self.adapter.handle(tpv('udp://*:29998'))
        self.adapter.handle(tpv('udp://*:29999', lat=48))
        self.adapter.handle(tpv('udp://*:29998', lat=49))  # same epoch
        self.adapter.handle(tpv('udp://*:29999', T1, lat=48.5))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual([x['source_id'] for x in self.sent], [A, B, B])
        self.assertEqual([x['lat'] for x in self.sent], [47, 48, 48.5])
        runs = [x['track_id'] for x in self.sent]
        self.assertNotEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])
        self.assertEqual(self.adapter.adapters['udp://*:29998'].counts['duplicate'], 1)
        self.assertEqual(self.adapter.summary().split(' '),
                         ['accepted=3', 'rejected=0', 'unavailable=0', 'errors=0', 'duplicate=1'])

    def test_unassigned_sources_remain_distinct_runs_without_source_ids(self):
        reports = []
        adapter = MultiAdapter([
            Source('oneplus', 'udp://*:29998', None),
            Source('lg', 'udp://*:29999', None)],
            endpoint='http://127.0.0.1:8765/api/position', send=reports.append)
        adapter.handle(tpv('udp://*:29998'))
        adapter.handle(tpv('udp://*:29999'))
        self.assertEqual(len(reports), 2)
        self.assertTrue(all('source_id' not in report for report in reports))
        self.assertNotEqual(reports[0]['track_id'], reports[1]['track_id'])
        self.assertRegex(reports[0]['track_id'], r'^oneplus-[0-9a-f]{12}$')
        self.assertRegex(reports[1]['track_id'], r'^lg-[0-9a-f]{12}$')
        adapter.handle(tpv('udp://*:29998'))  # Duplicate source epoch.
        self.assertEqual(len(reports), 2)

    def test_unknown_and_untagged_reports_cannot_be_attributed(self):
        with self.assertLogs('cartochron-gpsd', level='WARNING') as logs:
            self.adapter.handle(tpv('udp://*:30000'))
            self.adapter.handle(tpv('udp://*:30000', T1))
            self.adapter.handle(tpv(None))
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(self.sent, [])
        self.adapter.handle(tpv('udp://*:29998'))
        self.assertEqual(self.sent[0]['source_id'], A)

    def test_reconnect_preserves_watermarks_but_restart_creates_new_runs(self):
        self.adapter.handle(tpv('udp://*:29998', T1))
        previous = self.sent[0]['track_id']
        self.adapter.handle({'class': 'DEVICES', 'devices': []})
        self.adapter.handle(tpv('udp://*:29998', T0))
        self.assertEqual(len(self.sent), 1)
        replacement = MultiAdapter([Source('a', 'udp://*:29998', A)],
                                   endpoint='http://127.0.0.1:8765/api/position',
                                   send=self.sent.append)
        replacement.handle(tpv('udp://*:29998', T1))
        self.assertEqual(self.sent[-1]['source_id'], A)
        self.assertNotEqual(self.sent[-1]['track_id'], previous)

    def test_unregistered_gpsd_events_dont_break_other_sources(self):
        self.adapter.handle({'class': 'DEVICES', 'devices': [
            {'path': 'udp://*:29998'}, {'path': 'udp://*:30000'}]})
        self.adapter.handle(tpv('udp://*:29998'))
        self.assertEqual(len(self.sent), 1)
        with self.assertRaisesRegex(ConfigurationError, 'JSON WATCH'):
            self.adapter.handle({'class': 'WATCH', 'enable': True, 'json': False})

    def test_one_watch_stream_delivers_two_devices(self):
        left, right = socket.socketpair()
        reports = [
            {'class': 'DEVICES', 'devices': [{'path': 'udp://*:29998'},
                                            {'path': 'udp://*:29999'}]},
            tpv('udp://*:29998'), tpv('udp://*:29999'),
        ]
        def sender():
            with right:
                self.assertEqual(right.recv(256), WATCH)
                right.sendall(b''.join(json.dumps(x).encode() + b'\r\n' for x in reports))
        thread = threading.Thread(target=sender)
        thread.start()
        with left:
            self.adapter.consume(left)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([x['source_id'] for x in self.sent], [A, B])

    def test_one_failed_submission_does_not_replay_and_others_continue(self):
        def send(report):
            if report['source_id'] == A:
                raise ConnectionError('unknown server outcome')
            self.sent.append(report)
        adapter = MultiAdapter([Source('a', 'udp://*:29998', A),
                                Source('b', 'udp://*:29999', B)],
                               endpoint='http://127.0.0.1:8765/api/position', send=send)
        with self.assertLogs('cartochron-gpsd', level='WARNING'):
            adapter.handle(tpv('udp://*:29998'))
        adapter.handle(tpv('udp://*:29998'))
        adapter.handle(tpv('udp://*:29999'))
        self.assertEqual([x['source_id'] for x in self.sent], [B])
        self.assertEqual(adapter.adapters['udp://*:29998'].counts['errors'], 1)

    def test_hundred_independent_sources_no_process_per_source(self):
        sources = [Source(f'device_{i}', f'udp://*:{30000+i}', f'src_{i:024x}')
                   for i in range(100)]
        received = []
        adapter = MultiAdapter(sources, endpoint='http://127.0.0.1:8765/api/position',
                               send=received.append)
        for source in sources:
            adapter.handle(tpv(source.device))
        self.assertEqual(len(received), 100)
        self.assertEqual(len({x['track_id'] for x in received}), 100)
        self.assertEqual(len({x['source_id'] for x in received}), 100)


if __name__ == '__main__':
    unittest.main()
