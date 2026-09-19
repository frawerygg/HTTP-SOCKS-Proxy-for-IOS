import asyncio
import socket
import threading
import time
import unittest
from collections import namedtuple
from dataclasses import replace

from lib.config import DEFAULT_STABLE_CONFIG
from lib.interface_manager import AUTO, InterfaceRegistry
from lib.stable_runtime import StableServerManager, create_wpad_server
from lib.status import StatusMonitor
from socks5 import create_wpad_server as create_legacy_wpad_server


Address = namedtuple("Address", "family address")
Interface = namedtuple("Interface", "name flags addr netmask dstaddr")


def iface(name, address):
    return Interface(name, 0, Address(socket.AF_INET, address), None, None)


def local_address():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SnapshotScanner:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0

    def __call__(self):
        return self.snapshots[min(self.index, len(self.snapshots) - 1)]

    def advance(self):
        self.index += 1


class StableModeTests(unittest.TestCase):
    def make_runtime(self, manager_class=StableServerManager, listen_host="127.0.0.1"):
        address = local_address()
        scanner = SnapshotScanner([[iface("en0", address), iface("pdp_ip0", address)]])
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host=listen_host,
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.15,
            health_interval=0.2,
            external_health_interval=60,
            restart_initial_delay=0.05,
            restart_max_delay=0.1,
            healthy_backoff_reset=0.2,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
        )
        return manager_class(StatusMonitor(""), config=config, registry=registry)

    @staticmethod
    def socks_greeting(host, port):
        sock = socket.create_connection((host, port), timeout=2)
        try:
            sock.sendall(b"\x05\x01\x00")
            return sock.recv(2)
        finally:
            sock.close()

    def test_interface_grace_and_address_update(self):
        scanner = SnapshotScanner([
            [iface("en0", "192.168.1.2"), iface("pdp_ip0", "100.1.1.1")],
            [iface("pdp_ip0", "100.1.1.1")],
            [iface("en0", "192.168.1.3"), iface("pdp_ip0", "100.1.1.1")],
        ])
        registry = InterfaceRegistry(
            scanner=scanner,
            missing_scans=3,
            missing_grace=0,
            connectivity_probe=lambda value: True,
        )
        registry.refresh((0,))
        selected = registry.resolve("en0", "proxy")
        self.assertEqual(selected.ipv4, "192.168.1.2")
        scanner.advance()
        registry.refresh((0,))
        en0 = next(item for item in registry.records() if item.name == "en0")
        self.assertTrue(en0.available)
        self.assertIn("temporarily unavailable", en0.display)
        scanner.advance()
        registry.refresh((0,))
        selected = registry.resolve("en0", "proxy")
        self.assertEqual(selected.ipv4, "192.168.1.3")

    def test_auto_can_change_en0_to_bridge100(self):
        scanner = SnapshotScanner([
            [iface("en0", "192.168.1.2")],
            [iface("bridge100", "172.20.10.1")],
        ])
        registry = InterfaceRegistry(
            scanner=scanner, missing_scans=1, missing_grace=0,
            connectivity_probe=lambda value: True,
        )
        registry.refresh((0,))
        self.assertEqual(registry.resolve(AUTO, "proxy").name, "en0")
        scanner.advance()
        registry.refresh((0,))
        self.assertEqual(registry.resolve(AUTO, "proxy").name, "bridge100")

    def test_start_stop_start_three_cycles_releases_ports(self):
        manager = self.make_runtime()
        try:
            for _ in range(3):
                status = manager.start(AUTO, AUTO)
                self.assertEqual(status["state"], "Running")
                manager.stop()
                self.assertFalse(manager.running)
        finally:
            manager.stop()

    def test_broad_listener_accepts_each_reachable_local_address(self):
        manager = self.make_runtime(listen_host="0.0.0.0")
        try:
            status = manager.start(AUTO, AUTO)
            self.assertIn("all IPv4 interfaces", status["listener_bind"])
            self.assertEqual(
                self.socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
            address = local_address()
            self.assertEqual(
                self.socks_greeting(address, manager.config.socks_port),
                b"\x05\x00",
            )
            self.assertIn("pdp_ip0", status["outbound_interface"])
        finally:
            manager.stop()

    def test_occupied_port_is_reported_and_never_running(self):
        manager = self.make_runtime()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", manager.config.http_port))
        blocker.listen()
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP failed to bind"):
                manager.start(AUTO, AUTO)
            self.assertNotEqual(manager.get_status()["state"], "Running")
        finally:
            blocker.close()
            manager.stop()

    def test_supervisor_detects_cancelled_socks_task(self):
        class CrashManager(StableServerManager):
            async def _start_generation(self):
                generation = await super()._start_generation()
                if not getattr(self, "crashed", False):
                    self.crashed = True
                    asyncio.get_running_loop().call_later(
                        0.05, generation["tasks"][0].cancel
                    )
                return generation

        manager = self.make_runtime(CrashManager)
        try:
            manager.start(AUTO, AUTO)
            deadline = time.time() + 4
            while time.time() < deadline:
                if manager.get_status()["recovery_count"]:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(manager.get_status()["recovery_count"], 1)
            self.assertIn("SOCKS listener", manager.get_status()["last_recovery_reason"])
        finally:
            manager.stop()

    def test_wpad_rebind_three_cycles(self):
        port = free_port()
        for _ in range(3):
            server = create_wpad_server("127.0.0.1", port, "127.0.0.1", 9876)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_wpad_advertises_address_used_by_client(self):
        for factory in (create_wpad_server, create_legacy_wpad_server):
            port = free_port()
            server = factory("0.0.0.0", port, "169.254.211.57", 9876)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for address in ("127.0.0.1", local_address()):
                    sock = socket.create_connection((address, port), timeout=2)
                    try:
                        sock.sendall(b"GET /wpad.dat HTTP/1.0\r\nHost: test\r\n\r\n")
                        response = b""
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            response += chunk
                    finally:
                        sock.close()
                    self.assertIn(("SOCKS5 %s:9876" % address).encode(), response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)



    def test_unavailable_selected_inbound_hint_does_not_block_startup(self):
        address = local_address()
        scanner = SnapshotScanner([[iface("pdp_ip0", address)]])
        registry = InterfaceRegistry(
            scanner=scanner,
            connectivity_probe=lambda value: True,
        )
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.1,
            health_interval=0.2,
            external_health_interval=60,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
        )
        manager = StableServerManager(StatusMonitor(""), config=config, registry=registry)
        try:
            status = manager.start("en2", "pdp_ip0")
            self.assertEqual(status["state"], "Running")
            self.assertEqual(
                self.socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
        finally:
            manager.stop()

    def test_missing_inbound_enx_does_not_restart_wildcard_listeners(self):
        address = local_address()
        scanner = SnapshotScanner([
            [iface("en2", address), iface("pdp_ip0", address)],
            [iface("pdp_ip0", address)],
        ])
        registry = InterfaceRegistry(
            scanner=scanner,
            missing_scans=1,
            missing_grace=0,
            connectivity_probe=lambda value: True,
        )
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.05,
            health_interval=0.1,
            external_health_interval=60,
            restart_initial_delay=0.05,
            restart_max_delay=0.1,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
        )
        manager = StableServerManager(StatusMonitor(""), config=config, registry=registry)
        try:
            manager.start("en2", "pdp_ip0")
            scanner.advance()
            time.sleep(0.35)
            status = manager.get_status()
            self.assertEqual(status["state"], "Running")
            self.assertEqual(status.get("recovery_count", 0), 0)
            self.assertIn(
                "wildcard listener still active",
                status["components"]["proxy interface"],
            )
            self.assertEqual(
                self.socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
        finally:
            manager.stop()

    def test_missing_outbound_still_triggers_recovery(self):
        address = local_address()
        scanner = SnapshotScanner([
            [iface("en2", address), iface("pdp_ip0", address)],
            [iface("en2", address)],
        ])
        registry = InterfaceRegistry(
            scanner=scanner,
            missing_scans=1,
            missing_grace=0,
            connectivity_probe=lambda value: True,
        )
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.05,
            health_interval=0.1,
            external_health_interval=60,
            restart_initial_delay=0.05,
            restart_max_delay=0.1,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
        )
        manager = StableServerManager(StatusMonitor(""), config=config, registry=registry)
        try:
            manager.start("en2", "pdp_ip0")
            scanner.advance()
            deadline = time.time() + 1.5
            while time.time() < deadline and manager.get_status().get("recovery_count", 0) == 0:
                time.sleep(0.05)
            self.assertGreaterEqual(manager.get_status().get("recovery_count", 0), 1)
        finally:
            manager.stop()

    def test_simultaneous_probe_failures_do_not_restart_live_listeners(self):
        class ProbeGlitchManager(StableServerManager):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.health_calls = 0

            async def _listener_health_detailed(self):
                self.health_calls += 1
                # First call is startup verification.  Then emulate repeated
                # simultaneous loopback timeouts like the real-device log.
                if 2 <= self.health_calls <= 8:
                    return {
                        "SOCKS listener": (False, "connect timeout"),
                        "HTTP listener": (False, "connect timeout"),
                        "WPAD listener": (False, "connect timeout"),
                    }
                return await super()._listener_health_detailed()

        manager = self.make_runtime(ProbeGlitchManager)
        manager.config = replace(
            manager.config,
            health_interval=0.03,
            health_failures_before_recovery=2,
            health_confirmation_attempts=3,
            health_confirmation_delay=0.01,
        )
        try:
            manager.start(AUTO, AUTO)
            time.sleep(0.35)
            status = manager.get_status()
            self.assertEqual(status.get("state"), "Running")
            self.assertEqual(status.get("recovery_count", 0), 0)
            self.assertEqual(
                self.socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
        finally:
            manager.stop()

    def test_confirmed_single_listener_probe_failure_still_recovers(self):
        class SingleFailureManager(StableServerManager):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.health_calls = 0

            async def _listener_health_detailed(self):
                self.health_calls += 1
                if 2 <= self.health_calls <= 7:
                    return {
                        "SOCKS listener": (False, "handshake timeout"),
                        "HTTP listener": (True, "ok"),
                        "WPAD listener": (True, "ok"),
                    }
                return await super()._listener_health_detailed()

        manager = self.make_runtime(SingleFailureManager)
        manager.config = replace(
            manager.config,
            health_interval=0.03,
            health_failures_before_recovery=2,
            health_confirmation_attempts=2,
            health_confirmation_delay=0.01,
            restart_initial_delay=0.02,
            restart_max_delay=0.03,
        )
        try:
            manager.start(AUTO, AUTO)
            deadline = time.time() + 1.5
            while time.time() < deadline:
                if manager.get_status().get("recovery_count", 0) >= 1:
                    break
                time.sleep(0.02)
            self.assertGreaterEqual(manager.get_status().get("recovery_count", 0), 1)
        finally:
            manager.stop()

    def test_full_socks_capacity_does_not_trigger_false_recovery(self):
        manager = self.make_runtime()
        manager.config = replace(
            manager.config,
            max_client_connections=1,
            health_interval=0.03,
            health_failures_before_recovery=1,
            health_confirmation_attempts=1,
        )
        client = None
        try:
            manager.start(AUTO, AUTO)
            client = socket.create_connection(
                ("127.0.0.1", manager.config.socks_port), timeout=1
            )
            time.sleep(0.25)
            status = manager.get_status()
            self.assertEqual(status.get("state"), "Running")
            self.assertEqual(status.get("recovery_count", 0), 0)
            self.assertIn(
                "capacity reached",
                status.get("components", {}).get("SOCKS listener", ""),
            )
        finally:
            if client is not None:
                client.close()
            manager.stop()

    def test_keepalive_start_stop_start_lifecycle(self):
        class FakeKeepalive:
            def __init__(self):
                self.active = False
                self.starts = 0

            def start(self):
                self.starts += 1
                self.active = True
                return True

            def stop(self):
                self.active = False

        manager = self.make_runtime()
        fake = FakeKeepalive()
        manager._keepalive = fake
        try:
            manager.start(AUTO, AUTO)
            manager.stop()
            manager.start(AUTO, AUTO)
            self.assertTrue(fake.active)
            self.assertEqual(fake.starts, 2)
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
