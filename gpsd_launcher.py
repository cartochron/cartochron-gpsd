#!/usr/bin/env python3
"""Start one GPSD input per registered UDP channel, without a shell wrapper."""

import argparse
import os
import re

from source_registry import RegistryError, load_sources

UDP_DEVICE = re.compile(r"udp://\*:(\d{1,5})\Z")
PORT_RANGE = re.compile(r"(\d{1,5})-(\d{1,5})\Z")


def device_arguments(sources, port_range):
    """Check every GPSD input fits Docker's explicitly published UDP range."""
    match = PORT_RANGE.fullmatch(port_range)
    if match is None:
        raise RegistryError("GPSD_UDP_PORT_RANGE must be FIRST-LAST")
    first, last = map(int, match.groups())
    if not 1 <= first <= last <= 65535 or last - first >= 256:
        raise RegistryError("invalid or excessive published UDP port range")
    paths = []
    for source in sources:
        match = UDP_DEVICE.fullmatch(source.device)
        if match is None or not first <= int(match[1]) <= last:
            raise RegistryError("registered GPSD device must be udp://*:PORT in the published range")
        paths.append(source.device)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, help="shared TOML source registry")
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.sources)
        paths = device_arguments(sources, os.environ.get("GPSD_UDP_PORT_RANGE", "29998-30017"))
    except RegistryError as exc:
        parser.error(str(exc))
    os.execvp("gpsd", ["gpsd", "-N", "-n", "-G", "-F", "/run/gpsd.sock", *paths])


if __name__ == "__main__":
    main()
