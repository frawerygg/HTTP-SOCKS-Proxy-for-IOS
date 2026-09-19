#!python3
# Socks5/HTTP Proxy server for Pythonista by @nneonneo
# Pretty statistics view and IPv6 support added by @philrosenthal

import asyncio
import ipaddress
import logging
import socket
import sys
import threading
import time

from lib.socks5_server import AsyncSocks5Handler
from lib.http_proxy_server import AsyncHTTPProxyHandler
from lib.proxy_server import AsyncProxyServer
from lib.status import StatusMonitor
from lib.keepalive import KeepaliveManager

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
lifecycle_logger = logging.getLogger("lifecycle")
wpad_logger = logging.getLogger("wpad")
logging.getLogger("proxy").setLevel(logging.INFO)
lifecycle_logger.setLevel(logging.INFO)
wpad_logger.setLevel(logging.INFO)

LISTEN_HOST = "0.0.0.0"
SOCKS_PORT = 9876
HTTP_PORT = 9877
WPAD_PORT = 8088

USE_PHONE_VPN = True
CUSTOM_RESOLVERS = []

IS_PYTHONISTA = "Pythonista" in sys.executable


def is_globally_routable(ipv6_address):
    non_routable_networks = [
        "ff00::/8",
        "fe80::/10",
        "fc00::/7",
        "::/8",
        "2001:db8::/32",
        "2001::/32",
        "2002::/16",
        "ff02::/16",
    ]
    for network in non_routable_networks:
        if ipaddress.ip_address(ipv6_address) in ipaddress.ip_network(network):
            return False
    return True


def is_routable_interface(interface_name):
    non_routable_interface_prefixes = [
        "ipsec",
        "awdl",
        "llw",
        "nan",
        "rd",
    ]
    return not any(
        interface_name.startswith(prefix) for prefix in non_routable_interface_prefixes
    )


DEFAULT_RESOLVERS = [
    "1.0.0.1",
    "1.1.1.1",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "2606:4700:4700::1001",
    "2001:4860:4860::8844",
]

try:
    import dns.asyncresolver

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers += CUSTOM_RESOLVERS or DEFAULT_RESOLVERS
except ImportError:
    print("Warning: dnspython not available; falling back to system DNS")
    resolver = None


def resolve_ipv6_for_interface(iface_name, is_vpn=False):
    """Find the best IPv6 address matching a given interface name."""
    from lib import ifaddrs

    all_ifaces = ifaddrs.get_interfaces()
    # First try: match by interface name
    ipv6_list = [
        iface
        for iface in all_ifaces
        if iface.addr
        and iface.addr.family == socket.AF_INET6
        and iface.addr.address
        and iface.name == iface_name
        and is_routable_interface(iface.name)
        and (is_globally_routable(iface.addr.address) if not is_vpn else True)
    ]
    if ipv6_list:
        return ipv6_list[-1].addr.address

    # Do not borrow IPv6 from another interface.  If the selected route has
    # no usable IPv6, IPv6 is disabled while IPv4 continues normally.
    return None


def test_ipv6_connectivity(ipv6_address):
    """Test if an IPv6 address can reach the internet. Returns the address or None."""
    try:
        test_socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        test_socket.settimeout(5)
        test_socket.bind((ipv6_address, 0))
        test_socket.connect(("2606:4700:4700::1111", 80))
        test_socket.close()
        return ipv6_address
    except Exception:
        try:
            test_socket.close()
        except Exception:
            pass
        return None


def create_wpad_server(hhost, hport, phost, pport):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HTTPHandler(BaseHTTPRequestHandler):
        def _client_reachable_proxy_host(s):
            try:
                local = s.connection.getsockname()[0]
                address = ipaddress.ip_address(local.split("%", 1)[0])
                if not address.is_unspecified:
                    return local
            except Exception:
                pass
            return phost

        def do_HEAD(s):
            s.send_response(200)
            s.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            s.end_headers()

        def do_GET(s):
            advertised_host = s._client_reachable_proxy_host()
            s.send_response(200)
            s.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            s.end_headers()
            s.wfile.write(
                (
                    """
function FindProxyForURL(url, host)
{
   if (isInNet(host, "192.168.0.0", "255.255.0.0")) {
      return "DIRECT";
   } else if (isInNet(host, "172.16.0.0", "255.240.0.0")) {
      return "DIRECT";
   } else if (isInNet(host, "10.0.0.0", "255.0.0.0")) {
      return "DIRECT";
   } else {
      return "SOCKS5 %s:%d; SOCKS %s:%d";
   }
}
"""
                    % (advertised_host, pport, advertised_host, pport)
                )
                .lstrip()
                .encode()
            )
            wpad_logger.info(
                "WPAD accepted client source=%s local-destination=%s advertised=%s:%d",
                s.client_address,
                s.connection.getsockname(),
                advertised_host,
                pport,
            )

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((hhost, hport), HTTPHandler)
    return server


# --- Server lifecycle management ---


class ServerManager:
    """Manages SOCKS, HTTP, and WPAD server lifecycle."""

    def __init__(self, stats):
        self.stats = stats
        self._loop = None
        self._thread = None
        self._socks_server = None
        self._http_server = None
        self._wpad_server = None
        self._wpad_thread = None
        self._socks_task = None
        self._http_task = None
        self._startup_event = threading.Event()
        self._startup_error = None

    def start(self, proxy_host, connect_host_ipv4, connect_iface_name):
        self._startup_event.clear()
        self._startup_error = None
        is_vpn = connect_iface_name.startswith("utun")
        ipv6_addr = resolve_ipv6_for_interface(connect_iface_name, is_vpn=is_vpn)
        connect_host_ipv6 = test_ipv6_connectivity(ipv6_addr) if ipv6_addr else None

        # WPAD server
        try:
            self._wpad_server = create_wpad_server(
                LISTEN_HOST, WPAD_PORT, proxy_host, SOCKS_PORT
            )
        except Exception as exc:
            raise RuntimeError("WPAD failed to bind :%d — %s" % (WPAD_PORT, exc))
        self._wpad_thread = threading.Thread(target=self._run_wpad, daemon=True)
        self._wpad_thread.start()

        # asyncio event loop in background thread
        self._loop = asyncio.new_event_loop()

        self._socks_server = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts=LISTEN_HOST,
            listen_port=SOCKS_PORT,
            traffic_stats=self.stats,
            resolver=resolver,
            connect_host_ipv4=connect_host_ipv4,
            connect_host_ipv6=connect_host_ipv6,
        )
        self._http_server = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts=LISTEN_HOST,
            listen_port=HTTP_PORT,
            traffic_stats=self.stats,
            resolver=resolver,
            connect_host_ipv4=connect_host_ipv4,
            connect_host_ipv6=connect_host_ipv6,
        )

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._startup_event.wait(8):
            self.stop()
            raise RuntimeError("Legacy Mode startup timed out")
        if self._startup_error:
            error = self._startup_error
            self.stop()
            raise RuntimeError(error)
        try:
            from lib import ifaddrs

            local_addresses = sorted({
                "%s=%s" % (item.name, item.addr.address)
                for item in ifaddrs.get_interfaces()
                if item.addr and item.addr.family == socket.AF_INET
                and not item.name.startswith("lo")
            })
        except Exception as exc:
            local_addresses = ["unavailable: %s" % exc]
        lifecycle_logger.info(
            "Inbound listeners requested=%s scope=all IPv4 interfaces; "
            "SOCKS=:%d HTTP=:%d WPAD=:%d; local addresses: %s",
            LISTEN_HOST, SOCKS_PORT, HTTP_PORT, WPAD_PORT,
            ", ".join(local_addresses),
        )
        lifecycle_logger.info(
            "Proxy advertisement=%s; outbound interface=%s IPv4=%s IPv6=%s",
            proxy_host, connect_iface_name, connect_host_ipv4,
            connect_host_ipv6 or "none",
        )

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)

        async def _serve():
            try:
                await self._socks_server.start()
            except Exception as exc:
                self._startup_error = "SOCKS failed to bind :%d — %s" % (
                    SOCKS_PORT, exc
                )
                self._startup_event.set()
                return
            try:
                await self._http_server.start()
            except Exception as exc:
                self._startup_error = "HTTP failed to bind :%d — %s" % (
                    HTTP_PORT, exc
                )
                await self._socks_server.stop()
                self._startup_event.set()
                return
            self._socks_task = asyncio.create_task(self._socks_server.run())
            self._http_task = asyncio.create_task(self._http_server.run())
            self._startup_event.set()
            await asyncio.gather(self._socks_task, self._http_task)

        # Schedule _serve() to run once the loop starts, then run the loop
        self._loop.call_soon(lambda: asyncio.ensure_future(_serve()))
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    def _run_wpad(self):
        try:
            self._wpad_server.serve_forever()
        except Exception:
            pass

    def stop(self):
        if self._loop and self._loop.is_running():

            async def _stop_all():
                if self._socks_server:
                    await self._socks_server.stop()
                if self._http_server:
                    await self._http_server.stop()
                for task in [self._socks_task, self._http_task]:
                    if task and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                # Let the event loop flush pending callbacks before shutdown
                await asyncio.sleep(0)

            future = asyncio.run_coroutine_threadsafe(_stop_all(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                logging.warning("Server stop timed out; forcing shutdown")
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

        if self._wpad_server:
            self._wpad_server.shutdown()
            self._wpad_server.server_close()
            if self._wpad_thread:
                self._wpad_thread.join(timeout=3)
            self._wpad_server = None
            self._wpad_thread = None

        self._socks_server = None
        self._http_server = None
        self._loop = None
        self._thread = None


# --- Background audio (prevents iOS from suspending Pythonista) ---
# Uses AVAudioSession directly via objc_util because the simpler
# sound.Player approach doesn't set the right audio session category
# on iOS 17+, causing Pythonista to be suspended anyway.

_legacy_keepalive = KeepaliveManager(enabled=True)


def _start_background_audio():
    """Enable a locally generated, looping silent audio keepalive."""
    return _legacy_keepalive.start()


def _stop_background_audio():
    _legacy_keepalive.stop()


# --- Entry points ---


def run_with_ui(mode=None):
    """Pythonista GUI mode."""
    if mode is None:
        import dialogs

        choice = dialogs.list_dialog(
            "Proxy Mode", ["Stable Mode (recommended)", "Legacy Mode"]
        )
        if choice is None:
            return
        mode = "stable" if choice.startswith("Stable") else "legacy"

    if mode == "stable":
        from lib.stable_runtime import StableServerManager
        from lib.stable_ui_view import StableProxyUIView

        stats = StatusMonitor("")
        stats.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(stats)
        manager = StableServerManager(stats)
        view = StableProxyUIView(manager, stats)
        view.present("fullscreen", hide_title_bar=True)
        return

    from lib.ui_view import ProxyUIView

    stats = StatusMonitor("")
    logging.getLogger().addHandler(stats)
    manager = ServerManager(stats)

    _start_background_audio()

    def start_server(proxy_addr, connect_addr, connect_name):
        # Stop/Start must re-enable the optional background keepalive.
        _start_background_audio()
        try:
            manager.start(proxy_addr, connect_addr, connect_name)
        except Exception:
            _stop_background_audio()
            raise

    def stop_server():
        _stop_background_audio()
        manager.stop()

    view = ProxyUIView(
        stats,
        start_server,
        stop_server,
        socks_port=SOCKS_PORT,
        http_port=HTTP_PORT,
        wpad_port=WPAD_PORT,
    )
    view.present("fullscreen", hide_title_bar=True)


def run_console():
    """Original console mode for non-Pythonista environments."""
    from collections import defaultdict

    from lib import ifaddrs

    # Keep screen on in Pythonista console mode
    try:
        import console
        from objc_util import on_main_thread

        on_main_thread(console.set_idle_timer_disabled)(True)
    except ImportError:
        pass

    PROXY_HOST = "172.20.10.1"
    CONNECT_HOST_IPV4 = "0.0.0.0"
    CONNECT_HOST_IPV6 = None
    initial_output = ""

    try:
        interfaces = ifaddrs.get_interfaces()
        iftypes = defaultdict(list)

        for iface in interfaces:
            if not iface.addr:
                continue
            if iface.name.startswith("lo"):
                continue
            if iface.name.startswith("en"):
                iftypes["en"].append(iface)
            elif iface.name.startswith("bridge"):
                iftypes["bridge"].append(iface)
            elif iface.name.startswith("utun"):
                iftypes["vpn"].append(iface)
            else:
                iftypes["cell"].append(iface)

        if iftypes["vpn"] and USE_PHONE_VPN:
            initial_output += "VPN use enabled (change with USE_PHONE_VPN)\n"
            new_ifaces = list(iftypes["vpn"]) + list(iftypes["cell"])
            iftypes["cell"] = new_ifaces

        if iftypes["bridge"]:
            iface = next(
                (i for i in iftypes["bridge"] if i.addr.family == socket.AF_INET),
                None,
            )
            if iface:
                initial_output += (
                    "Assuming proxy will be accessed over hotspot (%s) at %s\n"
                    % (iface.name, iface.addr.address)
                )
                PROXY_HOST = iface.addr.address
        elif iftypes["en"]:
            iface = next(
                (i for i in iftypes["en"] if i.addr.family == socket.AF_INET), None
            )
            if iface:
                initial_output += (
                    "Assuming proxy will be accessed over WiFi (%s) at %s\n"
                    % (iface.name, iface.addr.address)
                )
                PROXY_HOST = iface.addr.address
        else:
            initial_output += (
                "Warning: could not get WiFi address; assuming %s\n" % PROXY_HOST
            )

        if iftypes["cell"]:
            iface_ipv4 = next(
                (
                    i
                    for i in iftypes["cell"]
                    if i.addr.family == socket.AF_INET
                    and is_routable_interface(i.name)
                ),
                None,
            )
            if iface_ipv4:
                initial_output += (
                    "Will connect to IPv4 servers over interface %s at %s\n"
                    % (iface_ipv4.name, iface_ipv4.addr.address)
                )
                CONNECT_HOST_IPV4 = iface_ipv4.addr.address
                is_vpn = iface_ipv4.name.startswith("utun")
                ipv6_addr = resolve_ipv6_for_interface(
                    iface_ipv4.name, is_vpn=is_vpn
                )
                if ipv6_addr:
                    CONNECT_HOST_IPV6 = test_ipv6_connectivity(ipv6_addr)
                    if CONNECT_HOST_IPV6:
                        initial_output += (
                            "Will connect to IPv6 servers at %s\n" % CONNECT_HOST_IPV6
                        )
    except Exception as e:
        logging.error("Address detection failed: %s: %s", type(e).__name__, e)
        import traceback

        traceback.print_exc()

    wpad_server = create_wpad_server(LISTEN_HOST, WPAD_PORT, PROXY_HOST, SOCKS_PORT)

    initial_output += "PAC URL: http://{}:{}/wpad.dat\n".format(PROXY_HOST, WPAD_PORT)
    initial_output += "SOCKS Address: {}:{}\n".format(
        PROXY_HOST or LISTEN_HOST, SOCKS_PORT
    )
    initial_output += "HTTP Proxy Address: {}:{}\n".format(
        PROXY_HOST or LISTEN_HOST, HTTP_PORT
    )
    stats = StatusMonitor(initial_output)
    logging.getLogger().addHandler(stats)

    thread = threading.Thread(
        target=lambda: wpad_server.serve_forever(), daemon=True
    )
    thread.start()

    async def main():
        socks = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts=LISTEN_HOST,
            listen_port=SOCKS_PORT,
            traffic_stats=stats,
            resolver=resolver,
            connect_host_ipv4=CONNECT_HOST_IPV4,
            connect_host_ipv6=CONNECT_HOST_IPV6,
        )
        asyncio.create_task(socks.run())

        http = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts=LISTEN_HOST,
            listen_port=HTTP_PORT,
            traffic_stats=stats,
            resolver=resolver,
            connect_host_ipv4=CONNECT_HOST_IPV4,
            connect_host_ipv6=CONNECT_HOST_IPV6,
        )
        asyncio.create_task(http.run())

        await stats.render_forever()

    try:
        _start_background_audio()
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
        _stop_background_audio()
        wpad_server.shutdown()
        wpad_server.server_close()


if __name__ == "__main__":
    if IS_PYTHONISTA:
        run_with_ui()
    else:
        run_console()
