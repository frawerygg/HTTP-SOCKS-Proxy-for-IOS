"""Real-device diagnostics for the iOS ``en2`` interface lifecycle.

This module is deliberately observational.  It never mutates the interface
registry, changes selections, opens external connections, or requests recovery.
The monitor distinguishes interface presence from IPv4 readiness so topology
transitions are visible without mutating network state.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Iterable, Optional

from . import ifaddrs


class En2DiagnosticMonitor:
    def __init__(
        self,
        interval: float = 1.0,
        scanner: Optional[Callable[[], Iterable[object]]] = None,
        state_provider: Optional[Callable[[], dict]] = None,
        log_dir: Optional[str] = None,
    ):
        self.interval = max(0.25, float(interval))
        self.scanner = scanner or ifaddrs.get_interfaces
        self.state_provider = state_provider
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.log_dir = log_dir or os.path.join(project_root, "diagnostics")
        self.path = None
        self._thread = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._last_signature = None
        self._last_seen_ipv4 = None
        self._last_seen_ipv6 = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return self.path
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(self.log_dir, "en2_monitor_%s.log" % stamp)
        self._stop.clear()
        self._last_signature = None
        self._thread = threading.Thread(
            target=self._run, name="en2-diagnostic", daemon=True
        )
        self._thread.start()
        return self.path

    def stop(self, timeout=2.0):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        if not thread or not thread.is_alive():
            self._thread = None
        return not (thread and thread.is_alive())

    def note(self, event, detail=""):
        if not self.path:
            return
        self._append(
            "%s EVENT %-18s %s\n"
            % (self._timestamp(), str(event)[:18], str(detail).replace("\n", " "))
        )

    def sample_once(self):
        entries = []
        topology_names = set()
        address_owners = {}
        error = ""
        scan_started = time.monotonic()
        try:
            scanned = list(self.scanner())
            for iface in scanned:
                name = getattr(iface, "name", "") or ""
                if name:
                    topology_names.add(name)
                addr = getattr(iface, "addr", None)
                family = getattr(addr, "family", None) if addr else None
                address = getattr(addr, "address", None) if addr else None
                if address:
                    address_owners.setdefault(address, set()).add(name or "<unnamed>")
                if name != "en2":
                    continue
                netmask = getattr(getattr(iface, "netmask", None), "address", None)
                dstaddr = getattr(getattr(iface, "dstaddr", None), "address", None)
                entries.append(
                    (getattr(iface, "flags", 0), family, address, netmask, dstaddr)
                )
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
        scan_ms = (time.monotonic() - scan_started) * 1000.0

        ipv4 = [address for _, family, address, _, _ in entries if family == socket.AF_INET and address]
        ipv6 = [address for _, family, address, _, _ in entries if family == socket.AF_INET6 and address]
        en2_state = self._state_label(bool(entries), bool(ipv4))
        if ipv4:
            self._last_seen_ipv4 = ipv4[-1]
        if ipv6:
            self._last_seen_ipv6 = ipv6[-1]

        try:
            if_index = socket.if_nametoindex("en2") if hasattr(socket, "if_nametoindex") else None
        except OSError:
            if_index = 0
        except Exception:
            if_index = None

        flags = sorted({flags for flags, _, _, _, _ in entries})
        flag_names = self._flag_names(flags)
        en2_details = ";".join(
            "family=%s addr=%s netmask=%s dst=%s flags=0x%x"
            % (family, address or "-", netmask or "-", dstaddr or "-", flag)
            for flag, family, address, netmask, dstaddr in entries
        ) or "-"
        last_ipv4_owners = sorted(address_owners.get(self._last_seen_ipv4, set()))
        runtime = {}
        if self.state_provider:
            try:
                runtime = self.state_provider() or {}
            except Exception as exc:
                runtime = {"state_provider_error": "%s: %s" % (type(exc).__name__, exc)}

        signature = (
            bool(entries), tuple(ipv4), tuple(ipv6), tuple(flags), if_index,
            tuple(sorted(topology_names)), tuple(last_ipv4_owners), error,
        )
        changed = signature != self._last_signature
        self._last_signature = signature

        line = (
            "{ts} sample present={present} state={en2_state} changed={changed} if_index={index} "
            "ipv4={ipv4} ipv6={ipv6} flags={flags} flag_names={flag_names} "
            "last_ipv4={last4} last_ipv4_owners={owners} last_ipv6={last6} "
            "scan_ms={scan_ms:.2f} topology_names={names} runtime_state={state} "
            "inbound_recovery={irecovery} no_ipv4_s={noipv4:.1f} legacy_deep_resets={deepresets} "
            "selected_inbound={pin} selected_outbound={pout} listener_bind={bind} "
            "en2_details={details} error={error}\n"
        ).format(
            ts=self._timestamp(),
            present=bool(entries),
            en2_state=en2_state,
            changed=changed,
            index=if_index,
            ipv4=",".join(ipv4) or "-",
            ipv6=",".join(ipv6) or "-",
            flags=",".join("0x%x" % value for value in flags) or "-",
            flag_names=",".join(flag_names) or "-",
            last4=self._last_seen_ipv4 or "-",
            owners=",".join(last_ipv4_owners) or "-",
            last6=self._last_seen_ipv6 or "-",
            scan_ms=scan_ms,
            names=",".join(sorted(topology_names)) or "-",
            details=en2_details.replace(" ", "_"),
            state=runtime.get("state", "-"),
            irecovery=runtime.get("inbound_recovery_state", "-"),
            noipv4=float(runtime.get("inbound_no_ipv4_seconds", 0.0) or 0.0),
            deepresets=runtime.get("inbound_deep_reset_count", 0),
            pin=runtime.get("proxy_selection", "-"),
            pout=runtime.get("outbound_selection", "-"),
            bind=runtime.get("listener_bind", "-"),
            error=error or runtime.get("state_provider_error", "-") or "-",
        )
        self._append(line)
        if changed:
            self._append(
                "%s TRANSITION en2=%s ipv4=%s ipv6=%s if_index=%s topology=%s\n"
                % (
                    self._timestamp(),
                    en2_state,
                    ",".join(ipv4) or "-",
                    ",".join(ipv6) or "-",
                    if_index,
                    ",".join(sorted(topology_names)) or "-",
                )
            )
        return {
            "present": bool(entries),
            "state": en2_state,
            "ipv4": list(ipv4),
            "ipv6": list(ipv6),
            "flags": flags,
            "if_index": if_index,
            "topology_names": sorted(topology_names),
            "error": error,
        }

    @staticmethod
    def _state_label(present, has_ipv4):
        if not present:
            return "EN2_ABSENT"
        if has_ipv4:
            return "EN2_READY"
        return "EN2_PRESENT_NO_IPV4"

    def _run(self):
        self._append(
            "%s START interval=%.3fs observational=true target=en2\n"
            % (self._timestamp(), self.interval)
        )
        next_sample = time.monotonic()
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as exc:
                self._append(
                    "%s MONITOR_ERROR %s: %s\n"
                    % (self._timestamp(), type(exc).__name__, exc)
                )
            next_sample += self.interval
            wait = max(0.0, next_sample - time.monotonic())
            if self._stop.wait(wait):
                break
        self._append("%s STOP\n" % self._timestamp())

    def _append(self, text):
        if not self.path:
            return
        with self._write_lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()

    @staticmethod
    def _flag_names(values):
        combined = 0
        for value in values:
            combined |= value
        names = []
        for label, attr in (
            ("UP", "IFF_UP"),
            ("RUNNING", "IFF_RUNNING"),
            ("LOOPBACK", "IFF_LOOPBACK"),
            ("POINTOPOINT", "IFF_POINTOPOINT"),
            ("BROADCAST", "IFF_BROADCAST"),
            ("MULTICAST", "IFF_MULTICAST"),
        ):
            bit = getattr(socket, attr, 0)
            if bit and combined & bit:
                names.append(label)
        return names

    @staticmethod
    def _timestamp():
        return datetime.now().astimezone().isoformat(timespec="milliseconds")
