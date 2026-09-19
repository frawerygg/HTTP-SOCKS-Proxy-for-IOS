import asyncio
import logging
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from ctypes import POINTER, byref, cast, sizeof
from dataclasses import replace
from unittest import mock

from lib import ifaddrs
from lib.config import DEFAULT_STABLE_CONFIG
from lib.en2_diagnostics import En2DiagnosticMonitor
from lib.http_proxy_server import AsyncHTTPProxyHandler
from lib.interface_manager import AUTO, InterfaceRegistry
from lib.proxy_server import AsyncProxyServer
from lib.socks5_server import AsyncSocks5Handler, UdpForwarder
from lib.stable_runtime import StableServerManager, create_wpad_server
from lib.status import StatusMonitor
from socks5 import resolve_ipv6_for_interface


Address = namedtuple("Address", "family address")
Interface = namedtuple("Interface", "name flags addr netmask dstaddr")


def iface(name, address, family=socket.AF_INET, flags=0):
    return Interface(name, flags, Address(family, address), None, None)


def topology_iface(name, flags=0):
    # Any non-IP family is enough to model the AF_LINK/topology-only record
    # seen on iOS while en2 temporarily has no IPv4/IPv6 assignment.
    return Interface(name, flags, Address(999, "link"), None, None)


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


def socks_greeting(host, port):
    sock = socket.create_connection((host, port), timeout=2)
    try:
        sock.sendall(b"\x05\x01\x00")
        return sock.recv(2)
    finally:
        sock.close()


class SnapshotScanner:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            index = min(self.index, len(self.snapshots) - 1)
            return list(self.snapshots[index])

    def advance(self):
        with self.lock:
            self.index += 1


class Revised3RegressionTests(unittest.TestCase):
    def make_runtime(self, scanner=None):
        address = local_address()
        scanner = scanner or SnapshotScanner(
            [[iface("en2", address), iface("pdp_ip0", address), iface("en0", address)]]
        )
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.08,
            health_interval=0.15,
            diagnostics_enabled=False,
            enable_keepalive=False,
            en2_diagnostics_enabled=False,
            restart_initial_delay=0.03,
            restart_max_delay=0.05,
        )
        return StableServerManager(StatusMonitor(""), config=config, registry=registry)

    def test_ifaddrs_ipv4_and_ipv6_decode(self):
        sin = ifaddrs.SockaddrIn()
        sin.sin_len = sizeof(ifaddrs.SockaddrIn)
        sin.sin_family = socket.AF_INET
        packed4 = socket.inet_pton(socket.AF_INET, "169.254.211.57")
        for i, value in enumerate(packed4):
            sin.sin_addr[i] = value
        result4 = ifaddrs.get_sockaddr(
            cast(byref(sin), POINTER(ifaddrs.Sockaddr))
        )
        self.assertEqual(result4.address, "169.254.211.57")

        sin6 = ifaddrs.SockaddrIn6()
        sin6.sin6_len = sizeof(ifaddrs.SockaddrIn6)
        sin6.sin6_family = socket.AF_INET6
        sin6.sin6_scope_id = 7
        packed6 = socket.inet_pton(socket.AF_INET6, "fe80::1234")
        for i, value in enumerate(packed6):
            sin6.sin6_addr[i] = value
        result6 = ifaddrs.get_sockaddr(
            cast(byref(sin6), POINTER(ifaddrs.Sockaddr))
        )
        self.assertEqual(result6.address, "fe80::1234%7")

    def test_refresh_is_serialized_and_newer_result_wins(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def scanner():
            call = len(calls)
            calls.append(call)
            if call == 0:
                first_started.set()
                release_first.wait(2)
                return [iface("en2", "169.254.1.1")]
            return [iface("en2", "169.254.1.2")]

        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        t1 = threading.Thread(target=lambda: registry.refresh((0,)))
        t2 = threading.Thread(target=lambda: registry.refresh((0,)))
        t1.start()
        self.assertTrue(first_started.wait(1))
        t2.start()
        time.sleep(0.05)
        # Complete the older operation; the newer operation must run after it.
        release_first.set()
        t1.join(2)
        t2.join(2)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(registry.resolve("en2", "proxy").ipv4, "169.254.1.2")

    def test_ipv6_is_cleared_not_retained_stale(self):
        scanner = SnapshotScanner([
            [
                iface("pdp_ip0", "100.64.0.2"),
                iface("pdp_ip0", "2606:4700::1234", socket.AF_INET6),
            ],
            [iface("pdp_ip0", "100.64.0.2")],
        ])
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        registry.refresh((0,))
        self.assertEqual(registry.resolve("pdp_ip0", "outbound").ipv6, "2606:4700::1234")
        scanner.advance()
        registry.refresh((0,))
        self.assertIsNone(registry.resolve("pdp_ip0", "outbound").ipv6)

    def test_en2_topology_record_survives_without_ipv4(self):
        scanner = SnapshotScanner([
            [iface("en2", "169.254.211.57")],
            [topology_iface("en2")],
        ])
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        registry.refresh((0,))
        self.assertEqual(registry.resolve("en2", "proxy").ipv4, "169.254.211.57")

        scanner.advance()
        registry.refresh((0,))
        record = next(item for item in registry.records() if item.name == "en2")
        self.assertEqual(record.inbound_state, "PRESENT_NO_IPV4")
        self.assertEqual(record.miss_count, 0)
        self.assertIsNone(record.ipv4)
        self.assertIn("waiting for IPv4", record.display)

    def test_en2_ipv4_loss_and_restore_never_restarts_wildcard_listeners(self):
        outbound = local_address()
        scanner = SnapshotScanner([
            [iface("en2", "169.254.211.57"), iface("pdp_ip0", outbound)],
            [topology_iface("en2"), iface("pdp_ip0", outbound)],
            [
                topology_iface("en2"),
                iface("en2", "fe80::1234", socket.AF_INET6),
                iface("pdp_ip0", outbound),
            ],
            [iface("en2", "169.254.211.57"), iface("pdp_ip0", outbound)],
        ])
        manager = self.make_runtime(scanner=scanner)

        def wait_for_state(expected, timeout=2.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = manager.get_status()
                if status.get("inbound_state") == expected:
                    return status
                time.sleep(0.03)
            self.fail("timed out waiting for inbound state %s" % expected)

        try:
            manager.start("en2", "pdp_ip0")
            before = manager.get_status().get("recovery_count", 0)

            scanner.advance()
            waiting = wait_for_state("PRESENT_NO_IPV4")
            self.assertEqual(waiting["state"], "Running")
            self.assertEqual(waiting.get("recovery_count", 0), before)
            self.assertIn("waiting for local-link IPv4", waiting["proxy_interface"])
            self.assertIn(
                "wildcard listeners remain active",
                waiting["components"]["proxy interface"],
            )
            self.assertEqual(
                socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )

            # IPv6 can return before IPv4.  This must remain the same advisory
            # state rather than being mistaken for a usable proxy-side route.
            scanner.advance()
            time.sleep(0.2)
            self.assertEqual(manager.get_status().get("inbound_state"), "PRESENT_NO_IPV4")
            self.assertEqual(manager.get_status().get("recovery_count", 0), before)

            scanner.advance()
            ready = wait_for_state("READY")
            self.assertEqual(ready.get("recovery_count", 0), before)
            self.assertIn("169.254.211.57", ready["proxy_interface"])
            self.assertEqual(
                socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
        finally:
            manager.stop()

    def test_en2_stuck_no_ipv4_waits_for_external_local_link_without_restart(self):
        outbound = local_address()
        scanner = SnapshotScanner([[topology_iface("en2"), iface("pdp_ip0", outbound)]])
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.03,
            health_interval=0.2,
            external_health_interval=60,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
            restart_initial_delay=0.02,
            restart_max_delay=0.03,
            inbound_ipv4_warn_after=0.05,
        )
        manager = StableServerManager(StatusMonitor(""), config=config, registry=registry)
        try:
            manager.start("en2", "pdp_ip0")
            time.sleep(0.45)
            status = manager.get_status()
            self.assertEqual(status.get("inbound_recovery_state"), "WAITING_FOR_LOCAL_LINK")
            self.assertEqual(status.get("inbound_deep_reset_count"), 0)
            self.assertEqual(status.get("recovery_count", 0), 0)
            self.assertEqual(status.get("inbound_state"), "PRESENT_NO_IPV4")
            self.assertIn("local-link", status.get("proxy_interface", ""))
            self.assertEqual(
                socks_greeting("127.0.0.1", manager.config.socks_port),
                b"\x05\x00",
            )
        finally:
            manager.stop()

    def test_en2_external_local_link_restore_resumes_without_proxy_restart(self):
        outbound = local_address()
        scanner = SnapshotScanner([
            [topology_iface("en2"), iface("pdp_ip0", outbound)],
            [iface("en2", "169.254.211.57"), iface("pdp_ip0", outbound)],
        ])
        registry = InterfaceRegistry(scanner=scanner, connectivity_probe=lambda value: True)
        config = replace(
            DEFAULT_STABLE_CONFIG,
            listen_host="127.0.0.1",
            socks_port=free_port(),
            http_port=free_port(),
            wpad_port=free_port(),
            watchdog_interval=0.03,
            health_interval=0.2,
            external_health_interval=60,
            diagnostics_enabled=False,
            en2_diagnostics_enabled=False,
            enable_keepalive=False,
            restart_initial_delay=0.02,
            restart_max_delay=0.03,
            inbound_ipv4_warn_after=0.04,
        )
        manager = StableServerManager(StatusMonitor(""), config=config, registry=registry)
        try:
            manager.start("en2", "pdp_ip0")
            time.sleep(0.18)
            self.assertEqual(
                manager.get_status().get("inbound_recovery_state"),
                "WAITING_FOR_LOCAL_LINK",
            )
            self.assertEqual(manager.get_status().get("recovery_count", 0), 0)

            # Model the real-device event: the external wired local link returns
            # and iOS assigns the same 169.254/16 IPv4 to en2.
            scanner.advance()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                status = manager.get_status()
                if status.get("inbound_state") == "READY":
                    break
                time.sleep(0.02)
            status = manager.get_status()
            self.assertEqual(status.get("inbound_state"), "READY")
            self.assertEqual(status.get("inbound_recovery_state"), "IDLE")
            self.assertEqual(status.get("inbound_deep_reset_count"), 0)
            self.assertEqual(status.get("recovery_count", 0), 0)
            self.assertIn("169.254.211.57", status.get("proxy_interface", ""))
        finally:
            manager.stop()

    def test_manager_selection_state_cannot_diverge(self):
        manager = self.make_runtime()
        try:
            manager.start("en2", "pdp_ip0")
            before = manager.get_status().get("recovery_count", 0)
            self.assertFalse(manager.set_outbound_selection("pdp_ip0"))
            time.sleep(0.15)
            self.assertEqual(manager.get_status().get("recovery_count", 0), before)

            # Inbound is advisory; changing it updates manager state without recovery.
            self.assertTrue(manager.set_proxy_selection(AUTO))
            self.assertEqual(manager.proxy_selection, AUTO)
            time.sleep(0.15)
            self.assertEqual(manager.get_status().get("recovery_count", 0), before)

            # A real outbound change updates runtime ownership and requests recovery.
            self.assertTrue(manager.set_outbound_selection(AUTO))
            self.assertEqual(manager.outbound_selection, AUTO)
            deadline = time.time() + 2
            while time.time() < deadline:
                if manager.get_status().get("recovery_count", 0) > before:
                    break
                time.sleep(0.03)
            self.assertGreater(manager.get_status().get("recovery_count", 0), before)
        finally:
            manager.stop()

    def test_stop_retains_live_runtime_thread_ownership_on_timeout(self):
        manager = self.make_runtime()
        sleeper = threading.Thread(target=lambda: time.sleep(0.25), daemon=True)
        sleeper.start()
        manager._thread = sleeper
        result = manager.stop(timeout=0.01)
        self.assertFalse(result)
        self.assertIs(manager._thread, sleeper)
        self.assertEqual(manager.get_status()["state"], "Error")
        sleeper.join(1)
        self.assertTrue(manager.stop(timeout=0.1))
        self.assertIsNone(manager._thread)

    def test_stop_requested_before_async_events_cancels_startup(self):
        entered = threading.Event()
        release = threading.Event()

        class DelayedManager(StableServerManager):
            def _thread_main(self):
                entered.set()
                release.wait(2)
                super()._thread_main()

        manager = self.make_runtime()
        delayed = DelayedManager(
            manager.stats, config=manager.config, registry=manager.registry
        )
        errors = []

        def start():
            try:
                delayed.start("en2", "pdp_ip0", timeout=2)
            except RuntimeError as exc:
                errors.append(str(exc))

        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(delayed.stop(timeout=0.01))
        release.set()
        starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(errors, ["Stable Mode startup cancelled"])
        self.assertFalse(delayed.running)
        self.assertTrue(delayed.stop(timeout=0.2))

    def test_runtime_metrics_reset_and_structured_error_counts_once(self):
        manager = self.make_runtime()
        root = logging.getLogger()
        root.addHandler(manager.stats)
        try:
            manager._event("test", "error", "one failure")
        finally:
            root.removeHandler(manager.stats)
        snapshot = manager.stats.get_snapshot()
        self.assertEqual(snapshot["errors"], 1)
        self.assertEqual(snapshot["runtime"]["last_error"], "one failure")

        manager.stats.update_runtime(
            recovery_count=3,
            last_recovery_reason="old recovery",
            last_error="old failure",
        )
        manager.stats.reset_session()
        snapshot = manager.stats.get_snapshot()
        self.assertEqual(snapshot["errors"], 0)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["runtime"]["recovery_count"], 0)
        self.assertEqual(snapshot["runtime"]["last_recovery_reason"], "")
        self.assertEqual(snapshot["runtime"]["last_error"], "")

    def test_stopped_runtime_records_fixed_uptime_endpoint(self):
        manager = self.make_runtime()
        try:
            manager.start("en2", "pdp_ip0")
            self.assertTrue(manager.stop())
            runtime = manager.get_status()
            self.assertEqual(runtime["state"], "Stopped")
            self.assertIsNotNone(runtime["stopped_at"])
            stopped_at = runtime["stopped_at"]
            time.sleep(0.03)
            self.assertEqual(manager.get_status()["stopped_at"], stopped_at)
            self.assertGreaterEqual(stopped_at, runtime["started_at"])
        finally:
            manager.stop()

    def test_wpad_shutdown_not_blocked_by_incomplete_client(self):
        port = free_port()
        server = create_wpad_server("127.0.0.1", port, "127.0.0.1", 9876)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        try:
            client.sendall(b"GET /wpad.dat HTTP/1.1\r\nHost: test")
            started = time.monotonic()
            server.shutdown()
            elapsed = time.monotonic() - started
            client.close()
            server.server_close()
            thread.join(1.5)
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 1.5)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def test_en2_monitor_logs_only_en2_details_plus_transition_context(self):
        scanner = SnapshotScanner([
            [iface("en2", "169.254.211.57", flags=3), iface("pdp_ip0", "100.64.0.2")],
            [iface("pdp_ip0", "100.64.0.2")],
        ])
        with tempfile.TemporaryDirectory() as tempdir:
            monitor = En2DiagnosticMonitor(
                interval=60,
                scanner=scanner,
                state_provider=lambda: {
                    "state": "Running",
                    "proxy_selection": "en2",
                    "outbound_selection": "pdp_ip0",
                    "listener_bind": "0.0.0.0",
                },
                log_dir=tempdir,
            )
            monitor.start()
            # The background thread samples once immediately.
            time.sleep(0.05)
            scanner.advance()
            monitor.sample_once()
            path = monitor.path
            monitor.stop()
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("TRANSITION en2=EN2_READY", text)
            self.assertIn("TRANSITION en2=EN2_ABSENT", text)
            self.assertIn("last_ipv4=169.254.211.57", text)
            self.assertIn("runtime_state=Running", text)

    def test_legacy_ipv6_never_borrows_other_interface(self):
        interfaces = [
            iface("pdp_ip0", "100.64.0.2"),
            iface("en0", "2606:4700::9999", socket.AF_INET6),
        ]
        with mock.patch("lib.ifaddrs.get_interfaces", return_value=interfaces):
            self.assertIsNone(resolve_ipv6_for_interface("pdp_ip0", is_vpn=False))


class AsyncProxyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_half_close_allows_response_after_client_eof(self):
        async def target(reader, writer):
            data = await reader.read()
            await asyncio.sleep(0.05)
            writer.write(b"reply:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        stats = StatusMonitor("")
        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            traffic_stats=stats,
            handshake_timeout=2,
        )
        await proxy.start()
        proxy_port = proxy.bound_addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await reader.readexactly(2), b"\x05\x00")
            request = (
                b"\x05\x01\x00\x01"
                + socket.inet_pton(socket.AF_INET, "127.0.0.1")
                + target_port.to_bytes(2, "big")
            )
            writer.write(request)
            await writer.drain()
            reply = await reader.readexactly(10)
            self.assertEqual(reply[1], 0)
            writer.write(b"hello")
            await writer.drain()
            writer.write_eof()
            response = await asyncio.wait_for(reader.read(), 2)
            self.assertEqual(response, b"reply:hello")
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            snap = stats.get_snapshot()
            self.assertEqual(snap["connections"], 0)
            self.assertGreaterEqual(snap["outbound_total"], 5)
            self.assertGreaterEqual(snap["inbound_total"], len(b"reply:hello"))
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_upstream_rst_releases_client_handler(self):
        async def target(reader, writer):
            sock = writer.get_extra_info("socket")
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            writer.transport.abort()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        stats = StatusMonitor("")
        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            traffic_stats=stats,
            handshake_timeout=2,
        )
        await proxy.start()
        reader = writer = None
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_addresses[0][1]
            )
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await reader.readexactly(2), b"\x05\x00")
            writer.write(
                b"\x05\x01\x00\x01"
                + socket.inet_pton(socket.AF_INET, "127.0.0.1")
                + target_port.to_bytes(2, "big")
            )
            await writer.drain()
            self.assertEqual((await reader.readexactly(10))[1], 0)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            deadline = asyncio.get_running_loop().time() + 1
            while proxy._client_tasks and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(proxy._client_tasks)
            self.assertEqual(stats.get_snapshot()["connections"], 0)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_client_rst_releases_client_handler(self):
        target_closed = asyncio.Event()

        async def target(reader, writer):
            try:
                await reader.read()
            finally:
                target_closed.set()
                writer.close()
                await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        stats = StatusMonitor("")
        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            traffic_stats=stats,
            handshake_timeout=2,
        )
        await proxy.start()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.bound_addresses[0][1]
        )
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await reader.readexactly(2), b"\x05\x00")
            writer.write(
                b"\x05\x01\x00\x01"
                + socket.inet_pton(socket.AF_INET, "127.0.0.1")
                + target_port.to_bytes(2, "big")
            )
            await writer.drain()
            self.assertEqual((await reader.readexactly(10))[1], 0)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            writer.transport.abort()
            await asyncio.wait_for(target_closed.wait(), 2)
            deadline = asyncio.get_running_loop().time() + 1
            while proxy._client_tasks and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(proxy._client_tasks)
            self.assertEqual(stats.get_snapshot()["connections"], 0)
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_stop_while_tcp_forwarding_closes_handler(self):
        target_closed = asyncio.Event()

        async def target(reader, writer):
            try:
                await reader.read()
            finally:
                target_closed.set()
                writer.close()
                await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            handshake_timeout=2,
        )
        await proxy.start()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.bound_addresses[0][1]
        )
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b"\x05\x00")
        writer.write(
            b"\x05\x01\x00\x01"
            + socket.inet_pton(socket.AF_INET, "127.0.0.1")
            + target_port.to_bytes(2, "big")
        )
        await writer.drain()
        self.assertEqual((await reader.readexactly(10))[1], 0)
        await proxy.stop()
        self.assertFalse(proxy._client_tasks)
        await asyncio.wait_for(target_closed.wait(), 2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        target_server.close()
        await target_server.wait_closed()

    async def test_http_strips_proxy_headers_and_normalizes_empty_path(self):
        received = asyncio.get_running_loop().create_future()

        async def target(reader, writer):
            data = await reader.readuntil(b"\r\n\r\n")
            if not received.done():
                received.set_result(data)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        stats = StatusMonitor("")
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            traffic_stats=stats,
            handshake_timeout=2,
            http_max_header_bytes=8192,
        )
        await proxy.start()
        proxy_port = proxy.bound_addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            request = (
                "GET http://127.0.0.1:%d HTTP/1.1\r\n"
                "Host: 127.0.0.1:%d\r\n"
                "Proxy-Authorization: Basic secret\r\n"
                "Proxy-Connection: keep-alive\r\n"
                "Connection: Foo, keep-alive\r\n"
                "Foo: should-disappear\r\n"
                "X-Test: yes\r\n\r\n"
            ) % (target_port, target_port)
            writer.write(request.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            self.assertIn(b"200 OK", response)
            upstream = await asyncio.wait_for(received, 2)
            lower = upstream.lower()
            self.assertTrue(upstream.startswith(b"GET / HTTP/1.1\r\n"))
            self.assertNotIn(b"proxy-authorization", lower)
            self.assertNotIn(b"proxy-connection", lower)
            self.assertNotIn(b"foo:", lower)
            self.assertEqual(lower.count(b"connection:"), 1)
            self.assertIn(b"connection: close", lower)
            self.assertIn(b"x-test: yes", lower)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            snap = stats.get_snapshot()
            self.assertEqual(snap["connections"], 0)
            self.assertGreater(snap["outbound_total"], 0)
            self.assertGreater(snap["inbound_total"], 0)
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_header_size_limit(self):
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            handshake_timeout=2,
            http_max_header_bytes=8192,
        )
        await proxy.start()
        port = proxy.bound_addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET http://example.com/ HTTP/1.1\r\nX-Large: "
                + b"a" * 9000
                + b"\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            self.assertIn(b"431 Request Header Fields Too Large", response)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    async def test_absolute_https_is_rejected_but_connect_still_tunnels(self):
        accepted = 0

        async def target(reader, writer):
            nonlocal accepted
            accepted += 1
            data = await reader.read(4)
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            handshake_timeout=2,
        )
        await proxy.start()
        proxy_port = proxy.bound_addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(
                (
                    "GET https://127.0.0.1:%d/ HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\n\r\n"
                ).encode()
                % (target_port, target_port)
            )
            await writer.drain()
            self.assertIn(b"400", await asyncio.wait_for(reader.read(), 2))
            writer.close()
            await writer.wait_closed()
            self.assertEqual(accepted, 0)

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(
                (
                    "CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\n\r\n"
                ).encode()
                % (target_port, target_port)
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            self.assertIn(b"200 Connection established", response)
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"ping")
            writer.close()
            await writer.wait_closed()
            self.assertEqual(accepted, 1)
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_rejects_ambiguous_framing_and_mismatched_host(self):
        accepted = 0

        async def target(reader, writer):
            nonlocal accepted
            accepted += 1
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            handshake_timeout=2,
        )
        await proxy.start()
        proxy_port = proxy.bound_addresses[0][1]
        target = "http://127.0.0.1:%d/" % target_port
        invalid_headers = [
            "Transfer-Encoding: chunked\r\nContent-Length: 1\r\n",
            "Content-Length: 1\r\nContent-Length: 2\r\n",
            "Transfer-Encoding: chunked, gzip\r\n",
            "Transfer-Encoding: chunked,,\r\n",
            "Connection: Content-Length\r\nContent-Length: 1\r\n",
            "Host: example.invalid\r\n",
        ]
        try:
            for extra in invalid_headers:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy_port
                )
                host = "" if extra.lower().startswith("host:") else (
                    "Host: 127.0.0.1:%d\r\n" % target_port
                )
                writer.write(
                    ("POST %s HTTP/1.1\r\n%s%s\r\n" % (target, host, extra)).encode()
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                self.assertIn(b"400", response, extra)
                writer.close()
                await writer.wait_closed()
            self.assertEqual(accepted, 0)
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_normalizes_host_and_equal_content_lengths(self):
        received = asyncio.get_running_loop().create_future()

        async def target(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            body = await reader.readexactly(4)
            received.set_result((headers, body))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            handshake_timeout=2,
        )
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_addresses[0][1]
            )
            writer.write(
                (
                    "POST http://127.0.0.1:%d/upload HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\n"
                    "Content-Length: 4\r\nContent-Length: 4\r\n\r\ntest"
                ).encode()
                % (target_port, target_port)
            )
            await writer.drain()
            self.assertIn(b"200 OK", await asyncio.wait_for(reader.read(), 2))
            headers, body = await asyncio.wait_for(received, 2)
            lower = headers.lower()
            self.assertEqual(body, b"test")
            self.assertEqual(lower.count(b"host:"), 1)
            self.assertIn(("host: 127.0.0.1:%d" % target_port).encode(), lower)
            self.assertEqual(lower.count(b"content-length:"), 1)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_chunked_body_preserves_extensions_and_trailers(self):
        received = asyncio.get_running_loop().create_future()

        async def target(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            wire = bytearray()
            while True:
                line = await reader.readline()
                wire += line
                size = int(line.split(b";", 1)[0], 16)
                if size:
                    wire += await reader.readexactly(size + 2)
                else:
                    while True:
                        trailer = await reader.readline()
                        wire += trailer
                        if trailer == b"\r\n":
                            break
                    break
            received.set_result(bytes(wire))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await proxy.start()
        body = b"1;test=yes\r\na\r\n2\r\nbc\r\n1\r\nd\r\n0\r\nX-End: yes\r\n\r\n"
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_addresses[0][1]
            )
            writer.write(
                (
                    "POST http://127.0.0.1:%d/ HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n"
                ).encode()
                % (target_port, target_port)
                + body
            )
            await writer.drain()
            self.assertIn(b"200 OK", await asyncio.wait_for(reader.read(), 2))
            self.assertEqual(await asyncio.wait_for(received, 2), body)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_multi_megabyte_chunk_streams_before_completion(self):
        first_piece = asyncio.Event()
        received_size = asyncio.get_running_loop().create_future()

        async def target(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            size_line = await reader.readline()
            size = int(size_line.strip(), 16)
            first = await reader.readexactly(65536)
            first_piece.set()
            rest = await reader.readexactly(size - len(first))
            self.assertEqual(await reader.readexactly(2), b"\r\n")
            self.assertEqual(await reader.readline(), b"0\r\n")
            self.assertEqual(await reader.readline(), b"\r\n")
            received_size.set_result(len(first) + len(rest))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await proxy.start()
        size = 2 * 1024 * 1024
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_addresses[0][1]
            )
            writer.write(
                (
                    "POST http://127.0.0.1:%d/ HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n"
                    "%x\r\n"
                ).encode()
                % (target_port, target_port, size)
            )
            writer.write(b"a" * 65536)
            await writer.drain()
            await asyncio.wait_for(first_piece.wait(), 2)
            writer.write(b"b" * (size - 65536) + b"\r\n0\r\n\r\n")
            await writer.drain()
            self.assertIn(b"200 OK", await asyncio.wait_for(reader.read(), 5))
            self.assertEqual(await asyncio.wait_for(received_size, 2), size)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_huge_partial_chunk_and_stop_stalled_body_cleanup(self):
        first_piece = asyncio.Event()
        upstream_closed = asyncio.Event()

        async def target(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await reader.readline()
            await reader.readexactly(65536)
            first_piece.set()
            await reader.read()
            upstream_closed.set()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await proxy.start()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.bound_addresses[0][1]
        )
        writer.write(
            (
                "POST http://127.0.0.1:%d/ HTTP/1.1\r\n"
                "Host: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n"
                "40000000\r\n"
            ).encode()
            % (target_port, target_port)
        )
        writer.write(b"x" * 65536)
        await writer.drain()
        await asyncio.wait_for(first_piece.wait(), 2)
        await proxy.stop()
        await asyncio.wait_for(upstream_closed.wait(), 2)
        self.assertFalse(proxy._client_tasks)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        target_server.close()
        await target_server.wait_closed()

    async def test_http_rejects_malformed_chunk_terminator(self):
        trailing = asyncio.get_running_loop().create_future()

        async def target(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            self.assertEqual(await reader.readline(), b"4\r\n")
            self.assertEqual(await reader.readexactly(4), b"test")
            trailing.set_result(await reader.read())
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        proxy = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_addresses[0][1]
            )
            writer.write(
                (
                    "POST http://127.0.0.1:%d/ HTTP/1.1\r\n"
                    "Host: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n"
                    "4\r\ntestXX"
                ).encode()
                % (target_port, target_port)
            )
            await writer.drain()
            self.assertIn(b"400", await asyncio.wait_for(reader.read(), 2))
            self.assertEqual(await asyncio.wait_for(trailing, 2), b"")
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            target_server.close()
            await target_server.wait_closed()

    async def test_http_upstream_is_closed_on_pre_forward_exceptions(self):
        class FailingOrdinaryHandler(AsyncHTTPProxyHandler):
            def _forward_headers(self):
                raise RuntimeError("injected header construction failure")

        class FailingConnectHandler(AsyncHTTPProxyHandler):
            def send_response(self, *args, **kwargs):
                raise RuntimeError("injected CONNECT response failure")

        async def run_case(handler_class, request_template):
            upstream_closed = asyncio.Event()

            async def target(reader, writer):
                await reader.read()
                upstream_closed.set()
                writer.close()
                await writer.wait_closed()

            target_server = await asyncio.start_server(target, "127.0.0.1", 0)
            target_port = target_server.sockets[0].getsockname()[1]
            proxy = AsyncProxyServer(
                handler_class, listen_hosts="127.0.0.1", listen_port=0
            )
            await proxy.start()
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy.bound_addresses[0][1]
                )
                writer.write(request_template(target_port).encode())
                await writer.drain()
                await asyncio.wait_for(upstream_closed.wait(), 2)
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                writer.close()
                await writer.wait_closed()
            finally:
                await proxy.stop()
                target_server.close()
                await target_server.wait_closed()

        await run_case(
            FailingOrdinaryHandler,
            lambda port: (
                "GET http://127.0.0.1:%d/ HTTP/1.1\r\n"
                "Host: 127.0.0.1:%d\r\n\r\n" % (port, port)
            ),
        )
        await run_case(
            FailingConnectHandler,
            lambda port: (
                "CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                "Host: 127.0.0.1:%d\r\n\r\n" % (port, port)
            ),
        )

    async def test_wpad_health_accepts_fragmented_response_header(self):
        async def wpad(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.0 200 OK\r\n")
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(
                b"Content-Type: application/x-ns-proxy-autoconfig\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(wpad, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        manager = StableServerManager(
            StatusMonitor(""),
            config=replace(
                DEFAULT_STABLE_CONFIG,
                wpad_port=port,
                health_probe_timeout=0.5,
                diagnostics_enabled=False,
                enable_keepalive=False,
            ),
        )
        try:
            self.assertEqual(await manager._check_wpad_detail(), (True, "ok"))
        finally:
            server.close()
            await server.wait_closed()

    async def test_dns_resolution_has_application_total_deadline(self):
        class StalledResolver:
            nameservers = ["1.1.1.1"]

            def __init__(self):
                self.cancelled = 0

            async def resolve(self, *args, **kwargs):
                try:
                    await asyncio.Future()
                finally:
                    self.cancelled += 1

        resolver = StalledResolver()
        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            resolver=resolver,
            connection_timeout=0.05,
        )
        started = asyncio.get_running_loop().time()
        with self.assertRaisesRegex(asyncio.TimeoutError, "DNS resolution timed out"):
            await proxy.resolve_address(
                3, ("deadline-test.invalid", 80)
            )
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.assertEqual(resolver.cancelled, 2)

    async def test_proxy_connection_limit_rejects_excess_clients(self):
        class DummyResolver:
            nameservers = ["1.1.1.1"]

        proxy = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            resolver=DummyResolver(),
            connect_host_ipv4="127.0.0.1",
            handshake_timeout=5,
            max_client_connections=1,
        )
        await proxy.start()
        port = proxy.bound_addresses[0][1]
        first_reader = first_writer = second_writer = None
        try:
            first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
            self.assertEqual(
                await asyncio.wait_for(second_reader.read(1), 1.0), b""
            )
            # The original client remains owned by the proxy until it closes.
            self.assertEqual(len(proxy._client_tasks), 1)
        finally:
            if second_writer is not None:
                second_writer.close()
                try:
                    await second_writer.wait_closed()
                except Exception:
                    pass
            if first_writer is not None:
                first_writer.close()
                try:
                    await first_writer.wait_closed()
                except Exception:
                    pass
            await proxy.stop()

    async def test_udp_partial_start_failure_closes_created_transport(self):
        class FakeTransport:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class DummyResolver:
            nameservers = ["1.1.1.1"]

        server = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            resolver=DummyResolver(),
            connect_host_ipv4="127.0.0.1",
        )
        transport = FakeTransport()
        loop = asyncio.get_running_loop()
        calls = 0

        async def fake_create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return transport, object()
            raise OSError("simulated endpoint failure")

        forwarder = UdpForwarder(
            "test", server, "127.0.0.1", expected_client=("127.0.0.1", 5000)
        )
        with mock.patch.object(loop, "create_datagram_endpoint", new=fake_create):
            with self.assertRaises(OSError):
                await forwarder.start()
        self.assertTrue(transport.closed)
        self.assertIsNone(forwarder.client_conn)

    async def test_udp_partial_start_cancellation_closes_created_transport(self):
        class FakeTransport:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class DummyResolver:
            nameservers = ["1.1.1.1"]

        server = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts="127.0.0.1",
            listen_port=0,
            resolver=DummyResolver(),
            connect_host_ipv4="127.0.0.1",
        )
        transport = FakeTransport()
        second_endpoint_started = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        async def fake_create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return transport, object()
            second_endpoint_started.set()
            await asyncio.Future()

        forwarder = UdpForwarder(
            "test", server, "127.0.0.1", expected_client=("127.0.0.1", 5000)
        )
        with mock.patch.object(loop, "create_datagram_endpoint", new=fake_create):
            task = asyncio.create_task(forwarder.start())
            await asyncio.wait_for(second_endpoint_started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(transport.closed)
        self.assertIsNone(forwarder.client_conn)

    async def test_udp_rejects_wrong_association_sender(self):
        class DummyServer:
            udp_max_mappings = 8
            udp_mapping_ttl = 10
            udp_max_pending_tasks = 8

        forwarder = UdpForwarder(
            "test", DummyServer(), "127.0.0.1", expected_client=("10.0.0.5", 0)
        )
        self.assertFalse(forwarder._client_allowed(("10.0.0.6", 1234)))
        self.assertTrue(forwarder._client_allowed(("10.0.0.5", 1234)))
        self.assertFalse(forwarder._client_allowed(("10.0.0.5", 5678)))


if __name__ == "__main__":
    unittest.main()
