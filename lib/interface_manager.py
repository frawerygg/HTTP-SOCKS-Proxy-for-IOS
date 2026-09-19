"""Stable interface discovery and selection.

Selections are stored by interface name (or ``Auto``), never by an ifaddrs
object whose address can become stale after an iOS network transition.
"""

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import ifaddrs


AUTO = "Auto"
EXCLUDED_PREFIXES = ("lo", "ipsec", "awdl", "llw", "nan", "rd")


def classify_interface(name: str) -> str:
    if name.startswith("bridge"):
        return "Personal Hotspot / bridge"
    if name.startswith("pdp_ip"):
        return "Cellular"
    if name.startswith("utun"):
        return "VPN / tunnel"
    if name.startswith("en"):
        # BSD en* names are implementation details on Apple platforms; an
        # enX device may be Wi-Fi, a local-link path, or another interface.
        # Keep the UI deliberately non-committal instead of treating en2 as
        # a permanently stable Wi-Fi interface.
        return "Local link (en*)"
    return "Other"


def is_usable_ipv4(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
        return (
            value.version == 4
            and not value.is_unspecified
            and not value.is_loopback
            and not value.is_multicast
        )
    except ValueError:
        return False


def is_usable_ipv6(address: str, allow_local: bool = False) -> bool:
    try:
        value = ipaddress.ip_address(address.split("%", 1)[0])
        if value.version != 6 or value.is_unspecified or value.is_loopback:
            return False
        return allow_local or (not value.is_link_local and not value.is_private)
    except ValueError:
        return False


@dataclass
class InterfaceRecord:
    name: str
    kind: str
    ipv4: Optional[str] = None
    ipv6: Optional[str] = None
    available: bool = True
    miss_count: int = 0
    first_missing_at: Optional[float] = None
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def display(self) -> str:
        if self.available and not self.miss_count:
            address = self.ipv4 or self.ipv6 or "present; waiting for IPv4"
            return "%s — %s — %s" % (self.name, self.kind, address)
        return "%s — %s — temporarily unavailable" % (self.name, self.kind)

    @property
    def inbound_state(self) -> str:
        """Current advisory inbound readiness.

        Stable Mode listens on the wildcard address, so an inbound interface
        can remain physically present while its client-reachable IPv4 address
        is temporarily deconfigured during an iOS Wi-Fi/topology transition.
        Keep that distinct from the interface actually disappearing.
        """
        if not self.available or self.miss_count:
            return "ABSENT"
        if self.ipv4:
            return "READY"
        return "PRESENT_NO_IPV4"


@dataclass(frozen=True)
class ResolvedInterface:
    selection: str
    name: str
    kind: str
    ipv4: Optional[str]
    ipv6: Optional[str]


class InterfaceUnavailable(RuntimeError):
    pass


class InterfaceRegistry:
    def __init__(
        self,
        scanner: Optional[Callable[[], Iterable[object]]] = None,
        missing_scans: int = 3,
        missing_grace: float = 20.0,
        cache_ttl: float = 180.0,
        connectivity_probe: Optional[Callable[[str], bool]] = None,
    ):
        self._scanner = scanner or ifaddrs.get_interfaces
        self._missing_scans = max(1, missing_scans)
        self._missing_grace = max(0.0, missing_grace)
        self._cache_ttl = max(self._missing_grace, cache_ttl)
        self._connectivity_probe = connectivity_probe or self._probe_connectivity
        self._records = {}
        self._lock = threading.RLock()
        # Serialize the full multi-scan refresh, not merely the registry write.
        # Otherwise a slow older refresh can commit after a newer one.
        self._refresh_lock = threading.Lock()

    def _snapshot(self):
        found = {}
        for iface in self._scanner():
            address = getattr(iface, "addr", None)
            name = getattr(iface, "name", "")
            if not address or not name or name.startswith(EXCLUDED_PREFIXES):
                continue
            record = found.setdefault(
                name, InterfaceRecord(name=name, kind=classify_interface(name))
            )
            if address.family == socket.AF_INET and is_usable_ipv4(address.address):
                record.ipv4 = address.address
            elif address.family == socket.AF_INET6 and is_usable_ipv6(
                address.address, allow_local=name.startswith("utun")
            ):
                record.ipv6 = address.address
        # Preserve topology-only en* records.  iOS can keep en2 registered
        # (AF_LINK still present / if_nametoindex still valid) while removing
        # its 169.254/16 IPv4 address for tens of seconds during a Wi-Fi
        # transition.  Dropping that record made Stable Mode report en2 as
        # missing even though the interface itself never disappeared.
        return {
            name: rec
            for name, rec in found.items()
            if rec.ipv4 or rec.ipv6 or rec.kind == "Local link (en*)"
        }

    def refresh(self, retry_delays=(0.0, 0.5, 1.0)):
        """Refresh atomically across all retry scans.

        The complete operation is serialized so startup, manual refresh and the
        watchdog cannot publish results out of chronological order.
        """
        with self._refresh_lock:
            merged = {}
            errors = []
            for index, delay in enumerate(retry_delays):
                if index and delay:
                    time.sleep(delay)
                try:
                    merged.update(self._snapshot())
                except Exception as exc:
                    errors.append(exc)

            if not merged and errors and len(errors) == len(retry_delays):
                raise errors[-1]

            now = time.monotonic()
            with self._lock:
                for name, fresh in merged.items():
                    previous = self._records.get(name)
                    if previous:
                        previous.kind = fresh.kind
                        # Assign both families every time.  Explicitly writing
                        # None prevents stale IPv6 data surviving a refresh.
                        previous.ipv4 = fresh.ipv4
                        previous.ipv6 = fresh.ipv6
                        previous.available = True
                        previous.miss_count = 0
                        previous.first_missing_at = None
                        previous.last_seen = now
                    else:
                        fresh.last_seen = now
                        self._records[name] = fresh

                for name, record in list(self._records.items()):
                    if name in merged:
                        continue
                    record.miss_count += 1
                    if record.first_missing_at is None:
                        record.first_missing_at = now
                    missing_for = now - record.first_missing_at
                    record.available = not (
                        record.miss_count >= self._missing_scans
                        and missing_for >= self._missing_grace
                    )
                    if now - record.last_seen > self._cache_ttl:
                        del self._records[name]
                return self.records()

    def records(self):
        with self._lock:
            return [
                InterfaceRecord(**vars(record))
                for record in sorted(self._records.values(), key=self._sort_key)
            ]

    def clear_runtime_cache(self):
        """Forget cached interface observations without touching the OS.

        Used by Stable Mode's one-shot deep soft reset.  The next refresh is
        forced to rebuild its view from a fresh getifaddrs() scan, which more
        closely resembles a cold script start while avoiding private iOS APIs
        or forced address assignment.
        """
        with self._refresh_lock:
            with self._lock:
                self._records.clear()

    def options(self, selected_name=None):
        records = self.records()
        if selected_name and selected_name != AUTO and not any(
            item.name == selected_name for item in records
        ):
            records.append(
                InterfaceRecord(
                    name=selected_name,
                    kind=classify_interface(selected_name),
                    available=False,
                )
            )
        return [(AUTO, AUTO)] + [(item.display, item.name) for item in records]

    def resolve(self, selection: str, purpose: str, current_name=None, prefer_vpn=True):
        with self._lock:
            records = [InterfaceRecord(**vars(item)) for item in self._records.values()]

        if selection != AUTO:
            match = next((item for item in records if item.name == selection), None)
            if not match or not match.available or match.miss_count:
                raise InterfaceUnavailable(
                    "%s (%s) is not currently available" % (
                        selection,
                        classify_interface(selection),
                    )
                )
            if not match.ipv4:
                if match.ipv6:
                    raise InterfaceUnavailable(
                        "%s (%s) is IPv6-only; Stable Mode currently requires "
                        "a usable IPv4 address for this route"
                        % (selection, classify_interface(selection))
                    )
                raise InterfaceUnavailable(
                    "%s (%s) has no usable IP address"
                    % (selection, classify_interface(selection))
                )
            return self._resolved(selection, match)

        candidates = [
            item for item in records
            if item.available and not item.miss_count and item.ipv4
        ]
        if not candidates:
            raise InterfaceUnavailable("No usable IPv4 interfaces are available")

        # Keep a healthy current Auto selection stable instead of oscillating.
        current = next((item for item in candidates if item.name == current_name), None)
        if current and (purpose == "proxy" or self._connectivity_probe(current.ipv4)):
            return self._resolved(AUTO, current)

        candidates.sort(
            key=lambda item: self._score(item, purpose, prefer_vpn), reverse=True
        )
        if purpose == "outbound":
            # Probe at least one candidate of every type before additional
            # tunnels, so several utun devices cannot crowd out cellular.
            first_by_kind = []
            remaining = []
            seen_kinds = set()
            for item in candidates:
                if item.kind in seen_kinds:
                    remaining.append(item)
                else:
                    seen_kinds.add(item.kind)
                    first_by_kind.append(item)
            for item in (first_by_kind + remaining)[:5]:
                if self._connectivity_probe(item.ipv4):
                    return self._resolved(AUTO, item)
        return self._resolved(AUTO, candidates[0])

    @staticmethod
    def _resolved(selection, record):
        return ResolvedInterface(
            selection=selection,
            name=record.name,
            kind=record.kind,
            ipv4=record.ipv4,
            ipv6=record.ipv6,
        )

    @staticmethod
    def _sort_key(record):
        order = {
            "Personal Hotspot / bridge": 0,
            "Local link (en*)": 1,
            "Cellular": 2,
            "VPN / tunnel": 3,
            "Other": 4,
        }
        return (not record.available, order.get(record.kind, 9), record.name)

    @staticmethod
    def _score(record, purpose, prefer_vpn):
        if purpose == "proxy":
            weights = {
                "Personal Hotspot / bridge": 500,
                "Local link (en*)": 400,
                "Other": 200,
                "Cellular": 100,
                "VPN / tunnel": 50,
            }
        else:
            weights = {
                "VPN / tunnel": 500 if prefer_vpn else 300,
                "Cellular": 450,
                "Local link (en*)": 350,
                "Other": 200,
                "Personal Hotspot / bridge": 100,
            }
        return weights.get(record.kind, 0)

    @staticmethod
    def _probe_connectivity(address):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.bind((address, 0))
            sock.connect(("1.1.1.1", 80))
            return True
        except OSError:
            return False
        finally:
            sock.close()
