"""Source registry is an explicit authority map, not device fingerprinting."""

import tempfile
from pathlib import Path
import unittest

from gpsd_launcher import device_arguments
from source_registry import RegistryError, Source, load_sources

A = 'src_' + 'a' * 24
B = 'src_' + 'b' * 24


class RegistryTest(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sources.toml'
            path.write_text(content, encoding='utf-8')
            return load_sources(path)

    def test_mapping_is_explicit_and_stable(self):
        sources = self.load(f'''[sources.oneplus]
device = "udp://*:29998"
source_id = "{A}"
[sources.lg]
device = "udp://*:29999"
source_id = "{B}"
''')
        self.assertEqual([(s.name, s.device, s.source_id) for s in sources], [
            ('oneplus', 'udp://*:29998', A), ('lg', 'udp://*:29999', B)])

    def test_missing_or_guessed_identity_fails(self):
        for content in ('', '[sources.oneplus]\ndevice="udp://*:29998"\n',
                        '[sources.oneplus]\ndevice="udp://*:29998"\nsource_id="oneplus"\n',
                        '[sources.oneplus]\ndevice="udp://*:29998"\nsource_id="src_123"\n',
                        '[sources.oneplus]\ndevice="udp://*:29998"\nsource_id="' + A + '"\nlabel="extraneous"\n'):
            with self.subTest(content=content), self.assertRaises(RegistryError):
                self.load(content)

    def test_duplicate_port_or_source_id_is_rejected(self):
        for device, sid in [('udp://*:29998', B), ('udp://*:29999', A)]:
            with self.subTest(device=device, sid=sid), self.assertRaisesRegex(RegistryError, 'duplicate'):
                self.load(f'''[sources.a]
device="udp://*:29998"
source_id="{A}"
[sources.b]
device="{device}"
source_id="{sid}"
''')

    def test_bad_registry_rejected_without_side_effects(self):
        for content in ('[not_sources]\nname="x"\n',
                        '[sources.bad name]\n',
                        '[sources.a]\ndevice="udp://*:29998"\nsource_id=',
                        '[sources.a]\ndevice="udp://*:29998\\n"\nsource_id="' + A + '"\n'):
            with self.subTest(content=content), self.assertRaises(RegistryError):
                self.load(content)

    def test_restrict_udp_inputs_to_published_ports(self):
        sources = [Source('oneplus', 'udp://*:29998', A),
                   Source('lg', 'udp://*:29999', B)]
        self.assertEqual(device_arguments(sources, '29998-30017'),
                         ['udp://*:29998', 'udp://*:29999'])
        for value in ('29998', '30017-29998', '0-9999', '1-65536',
                      '29998-30300', 'hello'):
            with self.subTest(value=value), self.assertRaises(RegistryError):
                device_arguments(sources, value)
        for source in (Source('x', 'udp://*:29997', A),
                       Source('x', '/dev/ttyUSB0', A),
                       Source('x', 'udp://127.0.0.1:29998', A)):
            with self.subTest(source=source), self.assertRaises(RegistryError):
                device_arguments([source], '29998-30017')

    def test_accepts_up_to_256_sources_without_duplicate_devices(self):
        content = ''.join(f'[sources.device_{i}]\ndevice="udp://*:{30000+i}"\n'
                          f'source_id="src_{i:024x}"\n' for i in range(256))
        self.assertEqual(len(self.load(content)), 256)
        with self.assertRaises(RegistryError):
            self.load(content + f'[sources.extra]\ndevice="udp://*:50000"\nsource_id="{A}"\n')


if __name__ == '__main__':
    unittest.main()
