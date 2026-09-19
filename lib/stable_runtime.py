"""Reliability-focused runtime used only by Stable Mode."""

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .config import DEFAULT_STABLE_CONFIG
from .en2_diagnostics import En2DiagnosticMonitor
from .http_proxy_server import AsyncHTTPProxyHandler
from .interface_manager import (
    AUTO,
    InterfaceRegistry,
    InterfaceUnavailable,
    ResolvedInterface,
    classify_interface,
)
from .keepalive import KeepaliveManager
from .proxy_server import AsyncProxyServer
from .socks5_server import AsyncSocks5Handler


DEFAULT_RESOLVERS = ("1.0.0.1", "1.1.1.1", "8.8.8.8")


class RecoveryRequired(RuntimeError):
    pass


def _pythonista_is_backgrounded():
    """Best-effort Pythonista foreground/background state.

    The import is deliberately lazy so Stable Mode remains testable and usable
    outside Pythonista.
    """
    try:
        import console
        return bool(console.is_in_background())
    except Exception:
        return False


class _ReusableHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    # On runtimes that support it, do not wait for request threads during close.
    block_on_close = False

    def handle_error(self, request, client_address):
        # Incomplete/abandoned PAC clients are expected on mobile networks; do
        # not emit socketserver tracebacks from daemon request threads.
        logging.debug("WPAD client ended with an error: %s", client_address)


def create_wpad_server(host, port, proxy_host, proxy_port):
    class WPADHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            # A client that connects and never completes a request must not pin
            # a WPAD worker forever.
            try:
                self.connection.settimeout(5.0)
            except Exception:
                pass

        def _client_reachable_proxy_host(self):
            """Advertise the destination address the client used to reach WPAD."""
            try:
                local = self.connection.getsockname()[0]
                address = ipaddress.ip_address(local.split("%", 1)[0])
                if not address.is_unspecified:
                    return local
            except Exception:
                pass
            return proxy_host

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            self.end_headers()

        def do_GET(self):
            advertised_host = self._client_reachable_proxy_host()
            body = (
                'function FindProxyForURL(url, host) {\n'
                '  if (isInNet(host, "192.168.0.0", "255.255.0.0")) return "DIRECT";\n'
                '  if (isInNet(host, "172.16.0.0", "255.240.0.0")) return "DIRECT";\n'
                '  if (isInNet(host, "10.0.0.0", "255.0.0.0")) return "DIRECT";\n'
                '  return "SOCKS5 %s:%d; SOCKS %s:%d";\n'
                '}\n'
            ) % (advertised_host, proxy_port, advertised_host, proxy_port)
            self.send_response(200)
            self.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode("ascii"))
            logging.info(
                "WPAD accepted client source=%s local-destination=%s advertised=%s:%d",
                self.client_address,
                self.connection.getsockname(),
                advertised_host,
                proxy_port,
            )

        def log_message(self, fmt, *args):
            return

    return _ReusableHTTPServer((host, port), WPADHandler)


class StableServerManager:
    """Thread-safe facade around a supervised asyncio runtime."""

    def __init__(self, stats, config=None, registry=None):
        self.stats = stats
        self.config = config or DEFAULT_STABLE_CONFIG
        logging.getLogger().setLevel(self.config.logging_level)
        self.registry = registry or InterfaceRegistry(
            missing_scans=self.config.missing_interface_scans,
            missing_grace=self.config.missing_interface_grace,
            cache_ttl=self.config.interface_cache_ttl,
        )
        self.proxy_selection = AUTO
        self.outbound_selection = AUTO
        self._loop = None
        self._thread = None
        self._stop_async = None
        self._recover_async = None
        self._startup_event = threading.Event()
        self._startup_error = None
        self._stop_requested = threading.Event()
        self._manual_reason = "manual recovery"
        self._state_lock = threading.RLock()
        self._generation_counter = 0
        # Revised 4.2: an addressless en2 is treated as a passive local-link
        # condition, not a proxy failure.  Real-device logs show the link-local
        # IPv4 is restored by the external wired-link lifecycle, while proxy
        # restarts/deep resets do not manufacture that address.
        self._inbound_no_ipv4_since = None
        self._inbound_warned = False
        # Kept in runtime/status output for backward-compatible diagnostics.
        # Revised 4.2 never increments it.
        self._inbound_deep_reset_count = 0
        self._keepalive = KeepaliveManager(
            enabled=self.config.enable_keepalive, event_callback=self._event
        )
        self._en2_diag = None
        if self.config.en2_diagnostics_enabled:
            self._en2_diag = En2DiagnosticMonitor(
                interval=self.config.en2_diagnostics_interval,
                state_provider=self._diagnostic_state,
            )

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def refresh_interfaces(self):
        return self.registry.refresh()

    def interface_options(self, selected=None):
        return self.registry.options(selected)

    def set_proxy_selection(self, selection):
        """Update advisory inbound metadata without restarting listeners."""
        selection = selection or AUTO
        with self._state_lock:
            changed = selection != self.proxy_selection
            self.proxy_selection = selection
            self.stats.update_runtime(proxy_selection=selection)
        if changed and self._en2_diag and self._en2_diag.running:
            self._en2_diag.note("inbound-selection", selection)
        return changed

    def set_outbound_selection(self, selection, recover_if_running=True):
        """Update the actual outbound selection and recover only on a change."""
        selection = selection or AUTO
        with self._state_lock:
            changed = selection != self.outbound_selection
            self.outbound_selection = selection
            self.stats.update_runtime(outbound_selection=selection)
            should_recover = changed and recover_if_running and self.running
        if changed and self._en2_diag and self._en2_diag.running:
            self._en2_diag.note("outbound-selection", selection)
        if should_recover:
            self.recover("outbound interface selection changed")
        return changed

    def start(self, proxy_selection=AUTO, outbound_selection=AUTO, timeout=10.0):
        with self._state_lock:
            if self.running:
                raise RuntimeError("Stable Mode is already running")
            self.proxy_selection = proxy_selection or AUTO
            self.outbound_selection = outbound_selection or AUTO
            self._startup_event.clear()
            self._startup_error = None
            self._stop_requested.clear()
            reset_session = getattr(self.stats, "reset_session", None)
            if reset_session:
                reset_session()
            else:
                reset_traffic = getattr(self.stats, "reset_traffic", None)
                if reset_traffic:
                    reset_traffic()
            self._reset_inbound_ipv4_recovery_tracking()
            self.stats.update_runtime(
                state="Starting", started_at=time.time(), stopped_at=None,
                recovery_level=0,
                proxy_selection=self.proxy_selection,
                outbound_selection=self.outbound_selection,
                inbound_recovery_state="IDLE",
                inbound_no_ipv4_seconds=0.0,
                inbound_deep_reset_count=0,
            )
            if self._en2_diag:
                path = self._en2_diag.start()
                self.stats.update_runtime(en2_diagnostic_log=path or "")
                self._en2_diag.note(
                    "start-request",
                    "inbound=%s outbound=%s"
                    % (self.proxy_selection, self.outbound_selection),
                )
            self._keepalive.start()
            start_supervisor = getattr(self._keepalive, "start_supervisor", None)
            if callable(start_supervisor):
                start_supervisor(self.config.keepalive_supervisor_interval)
            self.stats.set_component(
                "keepalive", "healthy" if self._keepalive.active else "optional/off"
            )
            self._thread = threading.Thread(
                target=self._thread_main, name="stable-proxy", daemon=True
            )
            self._thread.start()

        if not self._startup_event.wait(timeout):
            self.stop()
            raise RuntimeError("Stable Mode startup timed out")
        if self._startup_error:
            error = self._startup_error
            self.stop()
            raise RuntimeError(error)
        return self.get_status()

    def stop(self, timeout=8.0):
        # Latch the request before the asyncio loop/events exist.  Startup and
        # Stop run on different threads, and an early Stop must not be lost.
        self._stop_requested.set()
        if self._en2_diag and self._en2_diag.running:
            self._en2_diag.note("stop-request")
        loop = self._loop
        event = self._stop_async
        if loop and event and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        self._keepalive.stop()
        self.stats.set_component("keepalive", "stopped")
        if thread and thread.is_alive():
            # Retain ownership: start() must continue to see this runtime as
            # alive and must not race a second generation onto the same ports.
            self.stats.update_runtime(state="Error", last_error="shutdown timed out")
            self._event("runtime", "error", "shutdown timed out; runtime thread still alive")
            return False
        self.stats.update_runtime(state="Stopped", stopped_at=time.time())
        with self._state_lock:
            if self._thread is thread:
                self._thread = None
        if self._en2_diag:
            self._en2_diag.note("stop-complete")
            self._en2_diag.stop()
        return True

    def recover(self, reason="manual recovery"):
        if not self.running or not self._loop or not self._recover_async:
            raise RuntimeError("Stable Mode is not running")
        self._manual_reason = reason
        self._loop.call_soon_threadsafe(self._recover_async.set)

    def get_status(self):
        return self.stats.get_snapshot()["runtime"]

    def _diagnostic_state(self):
        runtime = dict(self.get_status())
        runtime["proxy_selection"] = self.proxy_selection
        runtime["outbound_selection"] = self.outbound_selection
        return runtime

    def _reset_inbound_ipv4_recovery_tracking(self):
        self._inbound_no_ipv4_since = None
        self._inbound_warned = False
        self._inbound_deep_reset_count = 0

    def _mark_inbound_ready(self):
        had_issue = self._inbound_no_ipv4_since is not None
        self._inbound_no_ipv4_since = None
        self._inbound_warned = False
        self.stats.update_runtime(
            inbound_recovery_state="IDLE",
            inbound_no_ipv4_seconds=0.0,
            inbound_deep_reset_count=0,
        )
        return had_issue

    def _local_addresses(self):
        return ", ".join(
            "%s=%s" % (item.name, item.ipv4)
            for item in self.registry.records()
            if item.ipv4 and not item.miss_count
        )

    def _advisory_inbound(self, name):
        """Return current metadata for an explicitly selected inbound name.

        Unlike resolve(), this deliberately permits a topology-only interface
        with no IPv4 address.  Wildcard listeners can stay healthy while iOS
        temporarily removes en2's 169.254/16 address.
        """
        record = next((item for item in self.registry.records() if item.name == name), None)
        if record:
            return ResolvedInterface(
                selection=name,
                name=name,
                kind=record.kind,
                ipv4=record.ipv4,
                ipv6=record.ipv6,
            )
        return ResolvedInterface(
            selection=name,
            name=name,
            kind=classify_interface(name),
            ipv4=None,
            ipv6=None,
        )

    def _inbound_state(self, name):
        record = next((item for item in self.registry.records() if item.name == name), None)
        if not record:
            return "ABSENT"
        return record.inbound_state

    @staticmethod
    def _format_proxy_interface(proxy, state):
        if state == "READY" and proxy.ipv4:
            return "%s (%s) %s" % (proxy.name, proxy.kind, proxy.ipv4)
        if state == "PRESENT_NO_IPV4":
            return "%s (%s) present; waiting for local-link IPv4" % (proxy.name, proxy.kind)
        return "%s (%s) not enumerated; wildcard listener active" % (
            proxy.name,
            proxy.kind,
        )

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        except Exception as exc:
            self._startup_error = str(exc)
            self.stats.update_runtime(state="Error", last_error=str(exc))
            self._event("runtime", "error", str(exc))
            self._startup_event.set()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _supervise(self):
        self._stop_async = asyncio.Event()
        self._recover_async = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_async.set()
        delay = self.config.restart_initial_delay
        consecutive = 0
        first_start = True

        while not self._stop_async.is_set():
            generation = None
            healthy_since = None
            try:
                generation = await self._start_generation()
                healthy_since = time.monotonic()
                recovery_count = self.get_status().get("recovery_count", 0)
                self.stats.update_runtime(state="Running", recovery_level=0)
                self._event("runtime", "info", "all listeners healthy")
                if recovery_count:
                    self._event("recovery", "info", "recovery complete")
                if first_start:
                    self._startup_event.set()
                    first_start = False

                reason = await self._wait_for_change(generation)
                if reason is None:
                    break
                raise RecoveryRequired(reason)
            except Exception as exc:
                if first_start:
                    self._startup_error = str(exc)
                    self.stats.update_runtime(state="Error", last_error=str(exc))
                    self._event("startup", "error", str(exc))
                    self._startup_event.set()
                    return

                reason = str(exc)
                consecutive += 1
                recovery_count = self.get_status().get("recovery_count", 0) + 1
                level = self._recovery_level(reason, consecutive)
                self.stats.update_runtime(
                    state="Recovering",
                    recovery_count=recovery_count,
                    recovery_level=level,
                    last_recovery_reason=reason,
                    last_error=reason,
                )
                self._event("recovery", "warning", "recovery started: %s" % reason)
            finally:
                if generation:
                    await self._stop_generation(generation)

            if self._stop_async.is_set():
                break
            if healthy_since and time.monotonic() - healthy_since >= self.config.healthy_backoff_reset:
                delay = self.config.restart_initial_delay
                consecutive = 0
            if consecutive >= self.config.max_consecutive_restarts:
                self.stats.update_runtime(state="Paused")
                self.stats.update_runtime(recovery_level=6)
                self._event(
                    "recovery",
                    "error",
                    "persistent failure; retries paused for %.0f seconds"
                    % self.config.restart_pause,
                )
                await self._wait_or_stop(self.config.restart_pause)
                consecutive = 0
                delay = self.config.restart_initial_delay
            else:
                await self._wait_or_stop(delay)
                delay = min(
                    delay * self.config.restart_backoff,
                    self.config.restart_max_delay,
                )

        self.stats.update_runtime(state="Stopped", stopped_at=time.time())
        if first_start:
            self._startup_error = "Stable Mode startup cancelled"
            self._startup_event.set()

    async def _start_generation(self):
        self.stats.update_runtime(state="Starting")
        self._generation_counter += 1
        generation_id = self._generation_counter
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.registry.refresh)
        previous = self.get_status()
        proxy_current = self._name_from_status(previous.get("proxy_interface", ""))
        outbound_current = self._name_from_status(previous.get("outbound_interface", ""))
        try:
            proxy = await self._resolve_interface(
                self.proxy_selection, "proxy", proxy_current
            )
        except InterfaceUnavailable as exc:
            # The inbound selection is only an endpoint/display hint.  The
            # actual SOCKS/HTTP listeners bind to 0.0.0.0 and can remain
            # reachable on an iOS local-link address even when getifaddrs()
            # stops enumerating its enX record.  Fall back to any visible
            # interface for metadata/WPAD fallback instead of refusing to
            # start a healthy wildcard listener.
            self._event(
                "inbound",
                "warning",
                "%s; continuing with wildcard listener" % exc,
            )
            if self.proxy_selection != AUTO:
                # Keep ownership/metadata tied to the user's explicit inbound
                # selection even when that interface temporarily has no IPv4.
                # A separate fallback is used only for WPAD's emergency host.
                proxy = self._advisory_inbound(self.proxy_selection)
                wpad_proxy = await self._resolve_interface(AUTO, "proxy", proxy_current)
            else:
                proxy = await self._resolve_interface(AUTO, "proxy", proxy_current)
                wpad_proxy = proxy
        else:
            wpad_proxy = proxy
        outbound = await self._resolve_interface(
            self.outbound_selection, "outbound", outbound_current
        )
        if not wpad_proxy.ipv4:
            wpad_proxy = outbound
        inbound_state = self._inbound_state(proxy.name)
        if inbound_state == "READY":
            self._mark_inbound_ready()
            inbound_recovery_state = "IDLE"
            inbound_no_ipv4_seconds = 0.0
        elif inbound_state == "PRESENT_NO_IPV4":
            if self._inbound_no_ipv4_since is None:
                self._inbound_no_ipv4_since = time.monotonic()
            inbound_recovery_state = "WAITING_FOR_LOCAL_LINK"
            inbound_no_ipv4_seconds = max(
                0.0, time.monotonic() - self._inbound_no_ipv4_since
            )
        else:
            inbound_recovery_state = "ABSENT"
            inbound_no_ipv4_seconds = 0.0
        self.stats.update_runtime(
            proxy_interface=self._format_proxy_interface(proxy, inbound_state),
            inbound_state=inbound_state,
            inbound_recovery_state=inbound_recovery_state,
            inbound_no_ipv4_seconds=inbound_no_ipv4_seconds,
            inbound_deep_reset_count=self._inbound_deep_reset_count,
            outbound_interface="%s (%s) %s" % (
                outbound.name, outbound.kind, outbound.ipv4
            ),
            listener_bind="%s (all IPv4 interfaces)" % self.config.listen_host
            if self.config.listen_host in ("", "0.0.0.0")
            else "%s (address constrained)" % self.config.listen_host,
            local_addresses=self._local_addresses(),
        )
        self._event(
            "inbound",
            "info",
            "listener request %s; local addresses: %s"
            % (self.config.listen_host, self.get_status()["local_addresses"] or "none"),
        )
        self._event(
            "outbound",
            "info",
            "selected %s (%s) IPv4=%s IPv6=%s"
            % (outbound.name, outbound.kind, outbound.ipv4, outbound.ipv6 or "none"),
        )
        if inbound_state == "READY":
            self.stats.set_component("proxy interface", "healthy / advisory")
        elif inbound_state == "PRESENT_NO_IPV4":
            self.stats.set_component(
                "proxy interface",
                "present; waiting for local-link IPv4; wildcard listener still active",
            )
        else:
            self.stats.set_component(
                "proxy interface",
                "not enumerated; wildcard listener still active",
            )
        self.stats.set_component("outbound interface", "healthy")
        self.stats.set_component("watchdog", "starting")
        self.stats.set_component("DNS", "pending")
        self.stats.set_component("internet/outbound", "pending")

        resolver_one = self._make_resolver()
        resolver_two = self._make_resolver()
        socks = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts=self.config.listen_host,
            listen_port=self.config.socks_port,
            traffic_stats=self.stats,
            resolver=resolver_one,
            connect_host_ipv4=outbound.ipv4,
            connect_host_ipv6=outbound.ipv6,
            connection_timeout=self.config.connection_timeout,
            handshake_timeout=self.config.handshake_timeout,
            max_client_connections=self.config.max_client_connections,
            http_max_header_bytes=self.config.http_max_header_bytes,
            udp_max_mappings=self.config.udp_max_mappings,
            udp_mapping_ttl=self.config.udp_mapping_ttl,
            udp_max_pending_tasks=self.config.udp_max_pending_tasks,
        )
        http = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts=self.config.listen_host,
            listen_port=self.config.http_port,
            traffic_stats=self.stats,
            resolver=resolver_two,
            connect_host_ipv4=outbound.ipv4,
            connect_host_ipv6=outbound.ipv6,
            connection_timeout=self.config.connection_timeout,
            handshake_timeout=self.config.handshake_timeout,
            max_client_connections=self.config.max_client_connections,
            http_max_header_bytes=self.config.http_max_header_bytes,
            udp_max_mappings=self.config.udp_max_mappings,
            udp_mapping_ttl=self.config.udp_mapping_ttl,
            udp_max_pending_tasks=self.config.udp_max_pending_tasks,
        )
        generation = {
            "id": generation_id,
            "proxy": proxy,
            "proxy_state": inbound_state,
            "outbound": outbound,
            "socks": socks,
            "http": http,
            "wpad": None,
            "wpad_thread": None,
            "tasks": [],
        }
        try:
            try:
                await socks.start()
            except Exception as exc:
                raise RuntimeError(
                    "SOCKS failed to bind :%d — %s" % (self.config.socks_port, exc)
                )
            self.stats.set_component("SOCKS listener", "healthy")
            self._event(
                "SOCKS",
                "info",
                "listening %s on %s"
                % (socks.listen_scope, socks.bound_addresses),
            )
            try:
                await http.start()
            except Exception as exc:
                raise RuntimeError(
                    "HTTP failed to bind :%d — %s" % (self.config.http_port, exc)
                )
            self.stats.set_component("HTTP listener", "healthy")
            self._event(
                "HTTP",
                "info",
                "listening %s on %s"
                % (http.listen_scope, http.bound_addresses),
            )
            try:
                wpad = create_wpad_server(
                    self.config.listen_host,
                    self.config.wpad_port,
                    wpad_proxy.ipv4,
                    self.config.socks_port,
                )
            except Exception as exc:
                raise RuntimeError(
                    "WPAD failed to bind :%d — %s" % (self.config.wpad_port, exc)
                )
            generation["wpad"] = wpad
            wpad_thread = threading.Thread(
                target=wpad.serve_forever, name="stable-wpad", daemon=True
            )
            generation["wpad_thread"] = wpad_thread
            wpad_thread.start()
            self.stats.set_component("WPAD listener", "healthy")
            self._event(
                "WPAD",
                "info",
                "listening %s on %s"
                % (
                    "all interfaces" if self.config.listen_host in ("", "0.0.0.0")
                    else "address constrained",
                    wpad.server_address,
                ),
            )

            generation["tasks"] = [
                asyncio.create_task(socks.run(), name="SOCKS listener"),
                asyncio.create_task(http.run(), name="HTTP listener"),
                asyncio.create_task(self._interface_watchdog(generation), name="watchdog"),
                asyncio.create_task(self._health_monitor(generation), name="health"),
                asyncio.create_task(
                    self._background_runtime_monitor(generation), name="scheduler"
                ),
            ]
            await self._verify_listeners()
            return generation
        except Exception:
            await self._stop_generation(generation)
            raise

    async def _wait_for_change(self, generation):
        stop_task = asyncio.create_task(self._stop_async.wait(), name="stop request")
        recover_task = asyncio.create_task(
            self._recover_async.wait(), name="recovery request"
        )
        watched = list(generation["tasks"]) + [stop_task, recover_task]
        done, pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            if task in (stop_task, recover_task):
                task.cancel()
        if stop_task in done and self._stop_async.is_set():
            return None
        if recover_task in done and self._recover_async.is_set():
            self._recover_async.clear()
            return self._manual_reason
        task = next(iter(done))
        if task.cancelled():
            return "%s stopped unexpectedly" % task.get_name()
        exc = task.exception()
        if exc:
            return "%s: %s" % (task.get_name(), exc)
        return "%s stopped unexpectedly" % task.get_name()

    async def _interface_watchdog(self, generation):
        self.stats.set_component("watchdog", "healthy")
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.config.watchdog_interval)
            if self.config.background_lite_mode and _pythonista_is_backgrounded():
                self.stats.set_component(
                    "watchdog", "background-lite; interface scans paused"
                )
                continue
            self.stats.set_component("watchdog", "healthy")
            await loop.run_in_executor(None, lambda: self.registry.refresh((0.0,)))

            # Inbound listeners are bound to 0.0.0.0, so their health does not
            # depend on getifaddrs continuing to enumerate the interface that
            # originally exposed a client-reachable address.  iOS can stop
            # listing an enX interface while the address remains reachable by
            # connected clients.  Treat the proxy-side interface as advisory
            # metadata only; listener health checks are authoritative.
            active_proxy = generation["proxy"]
            records = {item.name: item for item in self.registry.records()}
            target_name = (
                self.proxy_selection
                if self.proxy_selection != AUTO
                else active_proxy.name
            )
            proxy_record = records.get(target_name)
            previous_state = generation.get("proxy_state", "ABSENT")

            if proxy_record and not proxy_record.miss_count:
                current_state = proxy_record.inbound_state
                if current_state == "READY":
                    try:
                        refreshed_proxy = await self._resolve_interface(
                            self.proxy_selection, "proxy", target_name
                        )
                    except InterfaceUnavailable:
                        refreshed_proxy = None
                    if refreshed_proxy is not None:
                        generation["proxy"] = refreshed_proxy
                        active_proxy = refreshed_proxy
                    self.stats.set_component("proxy interface", "healthy / advisory")
                else:
                    # The interface still exists; only its IPv4 assignment is
                    # missing.  Preserve that exact state instead of collapsing
                    # it into "interface missing" or restarting listeners.
                    active_proxy = self._advisory_inbound(target_name)
                    generation["proxy"] = active_proxy
                    self.stats.set_component(
                        "proxy interface",
                        "present; waiting for local-link IPv4; wildcard listener still active",
                    )
            else:
                current_state = "ABSENT"
                active_proxy = self._advisory_inbound(target_name)
                generation["proxy"] = active_proxy
                self.stats.set_component(
                    "proxy interface",
                    "not enumerated; wildcard listener still active",
                )

            now = time.monotonic()
            recovery_state = "IDLE"
            no_ipv4_seconds = 0.0
            if current_state == "READY":
                self._mark_inbound_ready()
            elif current_state == "PRESENT_NO_IPV4":
                if self._inbound_no_ipv4_since is None:
                    self._inbound_no_ipv4_since = now
                no_ipv4_seconds = max(0.0, now - self._inbound_no_ipv4_since)
                recovery_state = "WAITING_FOR_LOCAL_LINK"
                self.stats.set_component(
                    "proxy interface",
                    "present without local-link IPv4; connect/reconnect the wired link; "
                    "wildcard listeners remain active",
                )
                if (
                    no_ipv4_seconds >= self.config.inbound_ipv4_warn_after
                    and not self._inbound_warned
                ):
                    self._inbound_warned = True
                    self._event(
                        "inbound",
                        "warning",
                        "%s has been present without IPv4 for %.0fs. Proxy resets are "
                        "suppressed because they cannot create the external local link; "
                        "connect/reconnect the wired link and the watchdog will resume "
                        "automatically when IPv4 returns."
                        % (active_proxy.name, no_ipv4_seconds),
                    )
            else:
                self._inbound_no_ipv4_since = None
                self._inbound_warned = False
                recovery_state = "ABSENT"

            generation["proxy_state"] = current_state
            self.stats.update_runtime(
                proxy_interface=self._format_proxy_interface(active_proxy, current_state),
                inbound_state=current_state,
                inbound_recovery_state=recovery_state,
                inbound_no_ipv4_seconds=no_ipv4_seconds,
                inbound_deep_reset_count=self._inbound_deep_reset_count,
                local_addresses=self._local_addresses(),
            )
            if current_state != previous_state:
                if current_state == "READY":
                    self._event(
                        "inbound",
                        "info",
                        "%s local-link IPv4 restored: %s; wildcard listeners remained active"
                        % (active_proxy.name, active_proxy.ipv4),
                    )
                elif current_state == "PRESENT_NO_IPV4":
                    self._event(
                        "inbound",
                        "warning",
                        "%s still present but local-link IPv4 is unavailable; "
                        "waiting for external link while wildcard listeners remain active"
                        % active_proxy.name,
                    )
                else:
                    self._event(
                        "inbound",
                        "warning",
                        "%s not enumerated; wildcard listeners remain active"
                        % active_proxy.name,
                    )

            # Outbound is different: SOCKS/HTTP sockets are explicitly bound
            # to this IPv4 address.  If it disappears or Auto moves elsewhere,
            # the generation really does need to restart with the new route.
            purpose = "outbound"
            active = generation[purpose]
            records = {item.name: item for item in self.registry.records()}
            record = records.get(active.name)
            if record and record.miss_count and record.available:
                self.stats.set_component(
                    "outbound interface",
                    "temporarily unavailable (%d/%d)"
                    % (record.miss_count, self.config.missing_interface_scans),
                )
                continue
            try:
                resolved = await self._resolve_interface(
                    self.outbound_selection, purpose, active.name
                )
            except InterfaceUnavailable as exc:
                raise RecoveryRequired(str(exc))
            if resolved.name != active.name:
                raise RecoveryRequired(
                    "outbound Auto interface changed: %s → %s"
                    % (active.name, resolved.name)
                )
            if resolved.ipv4 != active.ipv4:
                raise RecoveryRequired(
                    "outbound address changed: %s %s → %s"
                    % (active.name, active.ipv4, resolved.ipv4)
                )
            if resolved.ipv6 != active.ipv6:
                raise RecoveryRequired(
                    "outbound IPv6 changed: %s %s → %s"
                    % (active.name, active.ipv6 or "none", resolved.ipv6 or "none")
                )
            self.stats.set_component("outbound interface", "healthy")


    async def _background_runtime_monitor(self, generation):
        """Detect event-loop stalls and repair an interrupted audio keepalive.

        This intentionally does no network I/O and writes no periodic files. A
        warning is emitted only when the asyncio loop wakes substantially later
        than expected, which is the signature of background scheduling stalls.
        """
        interval = max(0.25, float(self.config.scheduler_probe_interval))
        lag_warn = max(0.10, float(self.config.scheduler_lag_warn))
        keepalive_interval = max(1.0, float(self.config.keepalive_recheck_interval))
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        next_keepalive = loop.time()
        lagging = False
        was_backgrounded = None
        self.stats.set_component("scheduler", "healthy")
        self.stats.set_component("background", "foreground")

        while True:
            await asyncio.sleep(interval)
            now = loop.time()
            backgrounded = _pythonista_is_backgrounded()
            if backgrounded != was_backgrounded:
                was_backgrounded = backgrounded
                self.stats.set_component(
                    "background", "background-lite" if backgrounded else "foreground"
                )
                self._event(
                    "background", "info",
                    "Pythonista entered background-lite mode; forwarding stays active"
                    if backgrounded
                    else "Pythonista returned to foreground; full monitoring resumed",
                )
            lag = max(0.0, now - expected)
            expected = now + interval

            if backgrounded and self.config.background_lite_mode:
                self.stats.set_component("scheduler", "background-lite")
                lagging = False
            elif lag >= lag_warn:
                self.stats.set_component("scheduler", "lag %.2fs" % lag)
                if not lagging:
                    lagging = True
                    self._event(
                        "performance",
                        "warning",
                        "asyncio scheduler resumed %.2fs late; likely background/runtime stall"
                        % lag,
                    )
            else:
                self.stats.set_component("scheduler", "healthy")
                if lagging:
                    lagging = False
                    self._event(
                        "performance",
                        "info",
                        "asyncio scheduler returned to normal",
                    )

            if now >= next_keepalive:
                next_keepalive = now + keepalive_interval
                ensure = getattr(self._keepalive, "ensure_active", None)
                if callable(ensure):
                    was_active = self._keepalive.active
                    okay = ensure()
                    self.stats.set_component(
                        "keepalive", "healthy" if okay else "optional/off"
                    )
                    if not was_active and okay:
                        self._event(
                            "keepalive", "info",
                            "background audio keepalive was not playing and was restarted",
                        )
                else:
                    self.stats.set_component(
                        "keepalive",
                        "healthy" if self._keepalive.active else "optional/off",
                    )

    async def _health_monitor(self, generation):
        failures = 0
        external_due = 0.0
        warned = False
        while True:
            await asyncio.sleep(self.config.health_interval)
            if self.config.background_lite_mode and _pythonista_is_backgrounded():
                # The forwarding tasks remain active. Avoid loopback probes, DNS
                # checks, and recovery decisions while iOS is giving Pythonista
                # reduced scheduling time.
                self.stats.set_component("health", "background-lite; probes paused")
                failures = 0
                warned = False
                continue
            self.stats.set_component("health", "active")
            detailed = await self._listener_health_detailed()
            socks = generation.get("socks")
            if (
                not detailed.get("SOCKS listener", (True, ""))[0]
                and socks is not None
                and socks.connection_limit_reached
                and self._listener_structural_health(generation).get(
                    "SOCKS listener", False
                )
            ):
                # The probe is itself an inbound SOCKS client.  At the configured
                # admission limit, rejection confirms saturation rather than a
                # dead listener as long as the task and listening socket are alive.
                detailed["SOCKS listener"] = (True, "connection capacity reached")
            results = {name: value[0] for name, value in detailed.items()}
            for name, (okay, detail) in detailed.items():
                self.stats.set_component(
                    name,
                    (
                        "healthy; connection capacity reached"
                        if okay and detail == "connection capacity reached"
                        else "healthy" if okay else "probe warning: %s" % detail
                    ),
                )

            if not all(results.values()):
                failures += 1
                failed_text = self._format_health_failures(detailed)
                if not warned:
                    warned = True
                    self._event(
                        "health",
                        "warning",
                        "listener self-check warning: %s; confirming before any restart"
                        % failed_text,
                    )

                if failures >= self.config.health_failures_before_recovery:
                    confirmed = await self._confirm_listener_health()
                    confirmed_results = {
                        name: value[0] for name, value in confirmed.items()
                    }
                    if all(confirmed_results.values()):
                        self._event(
                            "health",
                            "info",
                            "listener confirmation recovered; restart suppressed",
                        )
                        failures = 0
                        warned = False
                        for name in confirmed:
                            self.stats.set_component(name, "healthy")
                    else:
                        structural = self._listener_structural_health(generation)
                        persistent = [
                            name for name, okay in confirmed_results.items() if not okay
                        ]
                        structurally_dead = [
                            name for name in persistent if not structural.get(name, False)
                        ]
                        all_three_probe_failed = len(persistent) == len(confirmed_results)
                        all_structurally_alive = all(
                            structural.get(name, False) for name in confirmed_results
                        )

                        if structurally_dead:
                            raise RecoveryRequired(
                                "listener structurally unavailable after confirmation: %s"
                                % ", ".join(structurally_dead)
                            )

                        if all_three_probe_failed and all_structurally_alive:
                            # Real-device logs showed all three loopback probes
                            # timing out together while every listener task/socket
                            # remained alive and en2 stayed READY.  Treat that
                            # signature as scheduler/loopback probe degradation,
                            # not proof that three independent listeners died.
                            self._event(
                                "health",
                                "warning",
                                "all loopback probes still failed, but SOCKS/HTTP/WPAD "
                                "tasks and listening sockets are alive; restart suppressed",
                            )
                            for name in persistent:
                                self.stats.set_component(
                                    name, "probe degraded; listener task/socket alive"
                                )
                            failures = 0
                            warned = False
                        else:
                            raise RecoveryRequired(
                                "confirmed listener health failure: %s"
                                % self._format_health_failures(confirmed)
                            )
            else:
                if warned:
                    self._event(
                        "health", "info", "listener self-check returned to healthy"
                    )
                failures = 0
                warned = False

            now = time.monotonic()
            if self.config.diagnostics_enabled and now >= external_due:
                external_due = now + self.config.external_health_interval
                loop = asyncio.get_running_loop()
                dns_ok = await loop.run_in_executor(None, self._check_dns)
                internet_ok = await loop.run_in_executor(
                    None, lambda: self._check_outbound(generation["outbound"].ipv4)
                )
                self.stats.set_component("DNS", "healthy" if dns_ok else "failed")
                self.stats.set_component(
                    "internet/outbound", "healthy" if internet_ok else "failed"
                )

    async def _verify_listeners(self):
        detailed = await self._listener_health_detailed()
        failed = [name for name, (okay, _) in detailed.items() if not okay]
        if failed:
            raise RuntimeError(
                "listener verification failed: %s"
                % self._format_health_failures(detailed)
            )

    async def _listener_health(self):
        detailed = await self._listener_health_detailed()
        return {name: value[0] for name, value in detailed.items()}

    async def _listener_health_detailed(self):
        socks, http, wpad = await asyncio.gather(
            self._check_socks_detail(),
            self._check_tcp_detail(self.config.http_port),
            self._check_wpad_detail(),
        )
        return {
            "SOCKS listener": socks,
            "HTTP listener": http,
            "WPAD listener": wpad,
        }

    async def _confirm_listener_health(self):
        last = None
        attempts = max(1, int(self.config.health_confirmation_attempts))
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.config.health_confirmation_delay)
            last = await self._listener_health_detailed()
            if all(okay for okay, _ in last.values()):
                return last
        return last or {}

    @staticmethod
    def _format_health_failures(detailed):
        parts = []
        for name, value in detailed.items():
            okay, detail = value
            if not okay:
                parts.append("%s (%s)" % (name, detail))
        return ", ".join(parts) or "none"

    def _listener_structural_health(self, generation):
        task_by_name = {
            task.get_name(): task for task in generation.get("tasks", [])
        }
        socks_task = task_by_name.get("SOCKS listener")
        http_task = task_by_name.get("HTTP listener")
        socks = generation.get("socks")
        http = generation.get("http")
        wpad = generation.get("wpad")
        wpad_thread = generation.get("wpad_thread")
        try:
            wpad_socket_alive = bool(wpad and wpad.socket and wpad.socket.fileno() >= 0)
        except Exception:
            wpad_socket_alive = False
        return {
            "SOCKS listener": bool(
                socks and socks.is_listening and socks_task and not socks_task.done()
            ),
            "HTTP listener": bool(
                http and http.is_listening and http_task and not http_task.done()
            ),
            "WPAD listener": bool(
                wpad_socket_alive and wpad_thread and wpad_thread.is_alive()
            ),
        }

    async def _check_socks(self):
        return (await self._check_socks_detail())[0]

    async def _check_socks_detail(self):
        writer = None
        timeout = self.config.health_probe_timeout
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.config.socks_port), timeout
            )
        except asyncio.TimeoutError:
            return False, "connect timeout"
        except Exception as exc:
            return False, "connect %s" % type(exc).__name__
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            try:
                reply = await asyncio.wait_for(reader.readexactly(2), timeout)
            except asyncio.TimeoutError:
                return False, "handshake timeout"
            except Exception as exc:
                return False, "handshake %s" % type(exc).__name__
            if reply != b"\x05\x00":
                return False, "unexpected handshake reply %r" % (reply,)
            return True, "ok"
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _check_tcp(self, port):
        return (await self._check_tcp_detail(port))[0]

    async def _check_tcp_detail(self, port):
        writer = None
        timeout = self.config.health_probe_timeout
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout
            )
            return True, "ok"
        except asyncio.TimeoutError:
            return False, "connect timeout"
        except Exception as exc:
            return False, "connect %s" % type(exc).__name__
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _check_wpad(self):
        return (await self._check_wpad_detail())[0]

    async def _check_wpad_detail(self):
        writer = None
        timeout = self.config.health_probe_timeout
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.config.wpad_port), timeout
            )
        except asyncio.TimeoutError:
            return False, "connect timeout"
        except Exception as exc:
            return False, "connect %s" % type(exc).__name__
        try:
            writer.write(b"GET /wpad.dat HTTP/1.0\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            try:
                response = bytearray()
                deadline = asyncio.get_running_loop().time() + timeout
                max_header = min(
                    8192, int(getattr(self.config, "http_max_header_bytes", 65536))
                )
                while b"\r\n\r\n" not in response:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    chunk = await asyncio.wait_for(
                        reader.read(min(1024, max_header + 1 - len(response))),
                        remaining,
                    )
                    if not chunk:
                        return False, "incomplete response header"
                    response += chunk
                    if len(response) > max_header:
                        return False, "response header too large"
            except asyncio.TimeoutError:
                return False, "response timeout"
            except Exception as exc:
                return False, "response %s" % type(exc).__name__
            if b"200" not in response:
                return False, "HTTP status not 200"
            if b"proxy-autoconfig" not in response.lower():
                return False, "PAC content-type missing"
            return True, "ok"
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _stop_generation(self, generation):
        tasks = generation.get("tasks", [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for name in ("socks", "http"):
            server = generation.get(name)
            if server:
                try:
                    await server.stop()
                except Exception as exc:
                    self._event(name, "warning", "cleanup: %s" % exc)
        wpad = generation.get("wpad")
        thread = generation.get("wpad_thread")
        if wpad:
            loop = asyncio.get_running_loop()

            def stop_wpad():
                try:
                    wpad.shutdown()
                finally:
                    wpad.server_close()

            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, stop_wpad), timeout=4.0
                )
            except asyncio.TimeoutError:
                self._event("WPAD", "error", "shutdown timed out; forcing socket close")
                # shutdown() can wait for serve_forever() ownership.  Close the
                # listening socket independently so a stale generation cannot
                # retain the port while the blocking shutdown worker unwinds.
                try:
                    await asyncio.wait_for(
                        loop.run_in_executor(None, wpad.server_close), timeout=1.0
                    )
                except Exception as close_exc:
                    self._event("WPAD", "error", "forced close failed: %s" % close_exc)
            except Exception as exc:
                self._event("WPAD", "warning", "shutdown cleanup: %s" % exc)
        if thread and thread is not threading.current_thread():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: thread.join(3.0))
            if thread.is_alive():
                self._event(
                    "WPAD", "error",
                    "generation %s WPAD thread still alive after shutdown"
                    % generation.get("id", "?"),
                )
        for component in (
            "SOCKS listener", "HTTP listener", "WPAD listener", "watchdog", "scheduler"
        ):
            self.stats.set_component(component, "stopped")

    async def _wait_or_stop(self, delay):
        stop_task = asyncio.create_task(self._stop_async.wait())
        recover_task = asyncio.create_task(self._recover_async.wait())
        done, pending = await asyncio.wait(
            (stop_task, recover_task), timeout=delay,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if recover_task in done and self._recover_async.is_set():
            self._recover_async.clear()

    async def _resolve_interface(self, selection, purpose, current_name=None):
        # Auto selection performs connectivity probes; keep those blocking
        # socket operations off the forwarding event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.registry.resolve(
                selection, purpose, current_name=current_name,
                prefer_vpn=self.config.prefer_vpn,
            ),
        )

    def _make_resolver(self):
        try:
            import dns.asyncresolver

            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = list(DEFAULT_RESOLVERS)
            return resolver
        except ImportError:
            return None

    @staticmethod
    def _check_dns():
        try:
            socket.getaddrinfo("example.com", 80, socket.AF_INET, socket.SOCK_STREAM)
            return True
        except OSError:
            return False

    @staticmethod
    def _check_outbound(address):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(3.0)
            sock.bind((address, 0))
            sock.connect(("1.1.1.1", 80))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    @staticmethod
    def _name_from_status(value):
        return value.split(" ", 1)[0] if value else None

    def _event(self, component, severity, message):
        self.stats.add_event(component, severity, message)
        if (
            self._en2_diag
            and self._en2_diag.running
            and component in {
                "runtime", "startup", "recovery", "inbound", "outbound",
                "SOCKS", "HTTP", "WPAD", "performance", "keepalive"
            }
        ):
            self._en2_diag.note("%s/%s" % (component, severity), message)
        level = getattr(logging, severity.upper(), logging.INFO)
        logging.log(
            level,
            "%s: %s",
            component,
            message,
            extra={"structured_event": True},
        )

    @staticmethod
    def _recovery_level(reason, consecutive):
        text = reason.lower()
        if consecutive > 1:
            return 5
        if "interface" in text or "address changed" in text:
            return 3
        if "listener" in text or "stopped unexpectedly" in text:
            return 4
        if "health check" in text:
            return 2
        return 4
