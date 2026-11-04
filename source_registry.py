"""Explicit GPSD input -> enrolled Cartochron source mappings.

A GPSD device path identifies an acquisition channel, not a physical sender.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

SOURCE_ID = re.compile(r"src_[0-9a-f]{24}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MAX_SOURCES = 256


class RegistryError(ValueError):
    """An invalid registry must prevent the collector from starting."""


@dataclass(frozen=True)
class Source:
    name: str
    device: str
    source_id: str


def load_sources(filename):
    """Read a bounded, unambiguous registry; never invent source IDs."""
    try:
        with Path(filename).open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read source registry: {type(exc).__name__}") from exc
    if set(document) != {"sources"} or not isinstance(document["sources"], dict):
        raise RegistryError("registry must contain only [sources.NAME] tables")
    entries = document["sources"]
    if not 1 <= len(entries) <= MAX_SOURCES:
        raise RegistryError(f"registry must contain 1..{MAX_SOURCES} sources")
    result, devices, identities = [], set(), set()
    for name, entry in entries.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(entry, dict):
            raise RegistryError("invalid source name or mapping")
        if set(entry) != {"device", "source_id"}:
            raise RegistryError(f"{name}: expected device and source_id only")
        device, source_id = entry["device"], entry["source_id"]
        if (not isinstance(device, str) or not 1 <= len(device) <= 255 or
                any(ord(ch) < 33 or ord(ch) > 126 for ch in device)):
            raise RegistryError(f"{name}: invalid GPSD device path")
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise RegistryError(f"{name}: source_id must be an enrolled src_ identifier")
        if device in devices or source_id in identities:
            raise RegistryError(f"{name}: duplicate GPSD device or source_id")
        devices.add(device)
        identities.add(source_id)
        result.append(Source(name, device, source_id))
    return result
