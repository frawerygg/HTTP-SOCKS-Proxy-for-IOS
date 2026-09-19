# Network Interface Selector UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the auto-select + console UI with a Pythonista `ui` module GUI that lets users pick proxy-access and internet-connection interfaces at runtime, with live traffic stats.

**Architecture:** Extract interface discovery into `lib/interfaces.py`. Add `stop()` to `AsyncProxyServer`. Build a Pythonista `ui.View` in `lib/ui_view.py` that drives server lifecycle from the main thread while asyncio runs in a background thread. Keep the existing console fallback for non-Pythonista environments.

**Tech Stack:** Python 3, Pythonista `ui`/`dialogs`/`console` modules, asyncio, threading

**Spec:** `docs/superpowers/specs/2026-03-25-interface-selector-ui-design.md`

---

### Task 1: Extract interface discovery into `lib/interfaces.py`

Move the interface enumeration/classification logic out of `socks5.py` into a reusable module.

**Files:**
- Create: `lib/interfaces.py`
- Modify: `socks5.py:93-245` (remove inline interface logic, import from new module)

- [ ] **Step 1: Create `lib/interfaces.py` with `get_labeled_interfaces()`**

```python
import socket
from lib import ifaddrs


def _interface_type_label(name):
    if name.startswith("bridge"):
        return "Hotspot"
    if name.startswith("en"):
        return "WiFi"
    if name.startswith("utun"):
        return "VPN"
    if name.startswith("pdp_ip"):
        return "Cellular"
    return "Other"


def get_labeled_interfaces():
    """Return list of (display_string, iface) for all routable, non-loopback IPv4 interfaces."""
    non_routable_prefixes = ["ipsec", "awdl", "llw", "nan", "rd"]
    result = []
    for iface in ifaddrs.get_interfaces():
        if not iface.addr:
            continue
        if iface.addr.family != socket.AF_INET:
            continue
        if iface.name.startswith("lo"):
            continue
        if any(iface.name.startswith(p) for p in non_routable_prefixes):
            continue
        label = _interface_type_label(iface.name)
        display = f"{iface.name} - {label} ({iface.addr.address})"
        result.append((display, iface))
    return result


def get_default_selections(labeled):
    """Apply heuristic: bridge > en for proxy, cell/vpn for connect. Returns (proxy_idx, connect_idx)."""
    proxy_idx = None
    connect_idx = None
    # Proxy: prefer bridge (hotspot), then en (WiFi)
    for i, (display, iface) in enumerate(labeled):
        if iface.name.startswith("bridge"):
            proxy_idx = i
            break
    if proxy_idx is None:
        for i, (display, iface) in enumerate(labeled):
            if iface.name.startswith("en"):
                proxy_idx = i
                break
    # Connect: prefer utun (VPN), then pdp_ip (cellular), then any non-proxy interface
    for i, (display, iface) in enumerate(labeled):
        if iface.name.startswith("utun"):
            connect_idx = i
            break
    if connect_idx is None:
        for i, (display, iface) in enumerate(labeled):
            if iface.name.startswith("pdp_ip"):
                connect_idx = i
                break
    if connect_idx is None:
        for i, (display, iface) in enumerate(labeled):
            if not iface.name.startswith("bridge") and not iface.name.startswith("en"):
                connect_idx = i
                break
    return proxy_idx, connect_idx
```

- [ ] **Step 2: Verify module loads**

Run: `python -c "from lib.interfaces import get_labeled_interfaces, get_default_selections; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add lib/interfaces.py
git commit -m "feat: extract interface discovery into lib/interfaces.py"
```

---

### Task 2: Add `stop()` to `AsyncProxyServer`

Store the `asyncio.Server` handle and expose a `stop()` coroutine so the UI can shut down and restart servers.

**Files:**
- Modify: `lib/proxy_server.py:138-145`

- [ ] **Step 1: Store server handle and add `stop()`**

Replace the `run()` method in `AsyncProxyServer` (lines 138-145):

```python
    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self.client_connected,
            host=self.listen_hosts,
            port=self.listen_port,
            reuse_address=True,
        )
        await self._server.serve_forever()

    async def stop(self) -> None:
        if hasattr(self, '_server') and self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
```

- [ ] **Step 2: Verify no import errors**

Run: `python -c "from lib.proxy_server import AsyncProxyServer; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add lib/proxy_server.py
git commit -m "feat: add stop() method to AsyncProxyServer for runtime restart"
```

---

### Task 3: Add `get_snapshot()` to `StatusMonitor` for thread-safe stats reading

The UI thread needs to read stats without races. Add a snapshot method that returns a frozen dict.

**Files:**
- Modify: `lib/status.py:72-107`

- [ ] **Step 1: Add thread-safe `get_snapshot()` and a `threading.Lock` to `StatusMonitor`**

First, add `import threading` at the top of `lib/status.py`.

Then add `self._lock = threading.Lock()` in `StatusMonitor.__init__` after `self.num_errors = 0`.

Wrap the mutating methods `add_inbound`, `add_outbound`, `add_connection`, `remove_connection` with `with self._lock:`.

Add after the `emit()` method (after line 106):

```python
    def get_snapshot(self) -> dict:
        """Return a frozen snapshot of current stats for the UI to read.
        Thread-safe: uses a lock to prevent torn reads from ThroughputTracker."""
        with self._lock:
            inbound_average, inbound_total = self.inbound.update()
            outbound_average, outbound_total = self.outbound.update()
            return {
                "inbound_speed": inbound_average,
                "inbound_total": inbound_total,
                "outbound_speed": outbound_average,
                "outbound_total": outbound_total,
                "connections": self.num_connections,
                "errors": self.num_errors,
                "messages": list(self.messages),
            }
```

- [ ] **Step 2: Verify no import errors**

Run: `python -c "from lib.status import StatusMonitor; s = StatusMonitor(''); print(s.get_snapshot())"`
Expected: dict with all keys present

- [ ] **Step 3: Commit**

```bash
git add lib/status.py
git commit -m "feat: add get_snapshot() to StatusMonitor for thread-safe UI reads"
```

---

### Task 4: Create the Pythonista UI view (`lib/ui_view.py`)

Build the main `ui.View` subclass with interface selection, stats display, and server control.

**Files:**
- Create: `lib/ui_view.py`

- [ ] **Step 1: Create `lib/ui_view.py`**

```python
import sys
import threading
import asyncio
import dialogs

from lib.interfaces import get_labeled_interfaces, get_default_selections
from lib.status import StatusMonitor

IS_PYTHONISTA = "Pythonista" in sys.executable

if IS_PYTHONISTA:
    import ui
    import console
    from objc_util import on_main_thread


def _make_label(text, frame, font_size=14, alignment=ui.ALIGN_LEFT if IS_PYTHONISTA else 0):
    lbl = ui.Label()
    lbl.text = text
    lbl.frame = frame
    lbl.font = ("<system>", font_size)
    lbl.alignment = alignment
    lbl.number_of_lines = 0
    return lbl


def _make_button(title, frame, action):
    btn = ui.Button(type="system")
    btn.title = title
    btn.frame = frame
    btn.action = action
    return btn


class ProxyUIView(ui.View):
    def __init__(self, stats, start_server_cb, stop_server_cb, socks_port=9876, http_port=9877, wpad_port=8088):
        super().__init__()
        self.name = "SOCKS5 Proxy"
        self.background_color = "white"
        self.stats = stats
        self._start_server_cb = start_server_cb
        self._stop_server_cb = stop_server_cb
        self._socks_port = socks_port
        self._http_port = http_port
        self._wpad_port = wpad_port
        self._running = False

        # Current selections
        self._proxy_iface = None
        self._connect_iface = None

        # Detect interfaces and pick defaults
        self._refresh_and_set_defaults()

        # Keep screen on
        on_main_thread(console.set_idle_timer_disabled)(True)

        self._build_ui()
        self.update_interval = 1.0

    def _refresh_and_set_defaults(self):
        labeled = get_labeled_interfaces()
        proxy_idx, connect_idx = get_default_selections(labeled)
        if labeled:
            if proxy_idx is not None:
                self._proxy_iface = labeled[proxy_idx]
            else:
                self._proxy_iface = labeled[0]
            if connect_idx is not None:
                self._connect_iface = labeled[connect_idx]
            elif len(labeled) > 1:
                self._connect_iface = labeled[1]
            else:
                self._connect_iface = labeled[0]

    def _build_ui(self):
        w = self.width
        y = 10
        pad = 10

        # --- Proxy access row ---
        self._proxy_label = _make_label("", (pad, y, w - 100, 36))
        self._update_proxy_label()
        self.add_subview(self._proxy_label)

        self._proxy_btn = _make_button("변경", (w - 80, y, 70, 36), self._change_proxy)
        self.add_subview(self._proxy_btn)
        y += 44

        # --- Connect row ---
        self._connect_label = _make_label("", (pad, y, w - 100, 36))
        self._update_connect_label()
        self.add_subview(self._connect_label)

        self._connect_btn = _make_button("변경", (w - 80, y, 70, 36), self._change_connect)
        self.add_subview(self._connect_btn)
        y += 52

        # --- Separator ---
        sep = ui.View(frame=(pad, y, w - 2 * pad, 1))
        sep.background_color = "#cccccc"
        self.add_subview(sep)
        y += 10

        # --- Status label ---
        self._status_label = _make_label("Status: Stopped", (pad, y, w - 2 * pad, 24), font_size=16)
        self.add_subview(self._status_label)
        y += 30

        # --- Stats area ---
        self._stats_text = ui.TextView()
        self._stats_text.frame = (pad, y, w - 2 * pad, 200)
        self._stats_text.editable = False
        self._stats_text.font = ("<system>", 13)
        self.add_subview(self._stats_text)
        y += 210

        # --- Log area ---
        self._log_label = _make_label("", (pad, y, w - 2 * pad, 100), font_size=11)
        self.add_subview(self._log_label)
        y += 110

        # --- Start/Restart button ---
        self._action_btn = _make_button("Start", (pad, y, w - 2 * pad, 44), self._toggle_server)
        self._action_btn.background_color = "#007AFF"
        self._action_btn.tint_color = "white"
        self._action_btn.corner_radius = 8
        self.add_subview(self._action_btn)

    def _update_proxy_label(self):
        display = self._proxy_iface[0] if self._proxy_iface else "None"
        self._proxy_label.text = f"Proxy: {display}"

    def _update_connect_label(self):
        display = self._connect_iface[0] if self._connect_iface else "None"
        self._connect_label.text = f"Connect: {display}"

    def _change_proxy(self, sender):
        labeled = get_labeled_interfaces()
        items = [d for d, _ in labeled]
        choice = dialogs.list_dialog("Proxy 접근 인터페이스", items)
        if choice is not None:
            idx = items.index(choice)
            self._proxy_iface = labeled[idx]
            self._update_proxy_label()
            if self._running:
                self._restart_server()

    def _change_connect(self, sender):
        labeled = get_labeled_interfaces()
        items = [d for d, _ in labeled]
        choice = dialogs.list_dialog("인터넷 연결 인터페이스", items)
        if choice is not None:
            idx = items.index(choice)
            self._connect_iface = labeled[idx]
            self._update_connect_label()
            if self._running:
                self._restart_server()

    def _toggle_server(self, sender):
        if self._running:
            self._stop_server_cb()
            self._running = False
            self._status_label.text = "Status: Stopped"
            self._action_btn.title = "Start"
        else:
            if self._proxy_iface is None or self._connect_iface is None:
                dialogs.alert("Error", "Select both interfaces first.")
                return
            proxy_addr = self._proxy_iface[1].addr.address
            connect_addr = self._connect_iface[1].addr.address
            connect_name = self._connect_iface[1].name
            self._start_server_cb(proxy_addr, connect_addr, connect_name)
            self._running = True
            self._status_label.text = "Status: Running"
            self._action_btn.title = "Stop"

    def _restart_server(self):
        self._stop_server_cb()
        proxy_addr = self._proxy_iface[1].addr.address
        connect_addr = self._connect_iface[1].addr.address
        connect_name = self._connect_iface[1].name
        self._start_server_cb(proxy_addr, connect_addr, connect_name)
        self._status_label.text = "Status: Running (restarted)"

    def update(self):
        """Called by Pythonista UI at update_interval frequency."""
        if not self._running:
            return
        snap = self.stats.get_snapshot()
        megabit = 1024 * 1024 / 8
        megabyte = 1024 * 1024

        proxy_addr = self._proxy_iface[1].addr.address if self._proxy_iface else "?"
        lines = [
            f"In:    {snap['inbound_speed'] / megabit:.2f} Mbps",
            f"Out:   {snap['outbound_speed'] / megabit:.2f} Mbps",
            f"",
            f"Connections: {snap['connections']}",
            f"Total In:    {snap['inbound_total'] / megabyte:.2f} MB",
            f"Total Out:   {snap['outbound_total'] / megabyte:.2f} MB",
            f"Total:       {(snap['inbound_total'] + snap['outbound_total']) / megabyte:.2f} MB",
            f"",
            f"PAC URL: http://{proxy_addr}:{self._wpad_port}/wpad.dat",
            f"SOCKS:   {proxy_addr}:{self._socks_port}",
            f"HTTP:    {proxy_addr}:{self._http_port}",
        ]
        self._stats_text.text = "\n".join(lines)

        if snap["messages"]:
            self._log_label.text = "Log:\n" + "\n".join(snap["messages"][-5:])
        if snap["errors"]:
            self._log_label.text += f"\nErrors: {snap['errors']}"

    @property
    def proxy_address(self):
        return self._proxy_iface[1].addr.address if self._proxy_iface else None

    @property
    def connect_address(self):
        return self._connect_iface[1].addr.address if self._connect_iface else None

    @property
    def connect_iface_name(self):
        return self._connect_iface[1].name if self._connect_iface else None
```

- [ ] **Step 2: Commit**

```bash
git add lib/ui_view.py
git commit -m "feat: add Pythonista UI view for interface selection and stats"
```

---

### Task 5: Rewrite `socks5.py` main to use UI + server lifecycle

Rework the main entry point to: show the UI on Pythonista, run asyncio in a background thread, support stop/restart. Keep console fallback for non-Pythonista.

**Files:**
- Modify: `socks5.py` (major rewrite of lines 93-344)

- [ ] **Step 1: Rewrite `socks5.py`**

The full rewritten file:

```python
#!python3
# Socks5/HTTP Proxy server for Pythonista by @nneonneo
# Pretty statistics view and IPv6 support added by @philrosenthal

import asyncio
import ipaddress
import logging
import socket
import sys
import threading

from lib.socks5_server import AsyncSocks5Handler
from lib.http_proxy_server import AsyncHTTPProxyHandler
from lib.proxy_server import AsyncProxyServer
from lib.status import StatusMonitor

logging.basicConfig(level=logging.ERROR)

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

    # Fallback: any routable IPv6
    if not is_vpn:
        ipv6_list = [
            iface
            for iface in all_ifaces
            if iface.addr
            and iface.addr.family == socket.AF_INET6
            and iface.addr.address
            and is_globally_routable(iface.addr.address)
            and is_routable_interface(iface.name)
        ]
        if ipv6_list:
            return ipv6_list[-1].addr.address
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
        def do_HEAD(s):
            s.send_response(200)
            s.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            s.end_headers()

        def do_GET(s):
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
                    % (phost, pport, phost, pport)
                )
                .lstrip()
                .encode()
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

    def start(self, proxy_host, connect_host_ipv4, connect_iface_name):
        is_vpn = connect_iface_name.startswith("utun")
        ipv6_addr = resolve_ipv6_for_interface(connect_iface_name, is_vpn=is_vpn)
        connect_host_ipv6 = test_ipv6_connectivity(ipv6_addr) if ipv6_addr else None

        # WPAD server
        self._wpad_server = create_wpad_server(LISTEN_HOST, WPAD_PORT, proxy_host, SOCKS_PORT)
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

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)

        async def _serve():
            self._socks_task = asyncio.create_task(self._socks_server.run())
            self._http_task = asyncio.create_task(self._http_server.run())
            # Keep the loop alive until tasks are cancelled
            await asyncio.gather(self._socks_task, self._http_task, return_exceptions=True)

        self._serve_task = self._loop.create_task(_serve())
        self._loop.run_forever()

    def _run_wpad(self):
        try:
            self._wpad_server.serve_forever()
        except Exception:
            pass

    def stop(self):
        if self._loop and self._loop.is_running():
            async def _stop_all():
                # Stop servers gracefully
                if self._socks_server:
                    await self._socks_server.stop()
                if self._http_server:
                    await self._http_server.stop()
                # Cancel the tasks to avoid zombie coroutines
                for task in [self._socks_task, self._http_task]:
                    if task and not task.done():
                        task.cancel()

            future = asyncio.run_coroutine_threadsafe(_stop_all(), self._loop)
            future.result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

        if self._wpad_server:
            self._wpad_server.shutdown()
            self._wpad_server = None

        self._socks_server = None
        self._http_server = None
        self._loop = None
        self._thread = None


# --- Entry points ---

def run_with_ui():
    """Pythonista GUI mode."""
    from lib.ui_view import ProxyUIView

    stats = StatusMonitor("")
    logging.getLogger().addHandler(stats)
    manager = ServerManager(stats)

    def start_server(proxy_addr, connect_addr, connect_name):
        manager.start(proxy_addr, connect_addr, connect_name)

    def stop_server():
        manager.stop()

    view = ProxyUIView(stats, start_server, stop_server, socks_port=SOCKS_PORT, http_port=HTTP_PORT, wpad_port=WPAD_PORT)
    view.present("fullscreen")


def run_console():
    """Original console mode for non-Pythonista environments."""
    from collections import defaultdict
    from lib import ifaddrs

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
                (i for i in iftypes["bridge"] if i.addr.family == socket.AF_INET), None
            )
            if iface:
                initial_output += "Assuming proxy will be accessed over hotspot (%s) at %s\n" % (iface.name, iface.addr.address)
                PROXY_HOST = iface.addr.address
        elif iftypes["en"]:
            iface = next(
                (i for i in iftypes["en"] if i.addr.family == socket.AF_INET), None
            )
            if iface:
                initial_output += "Assuming proxy will be accessed over WiFi (%s) at %s\n" % (iface.name, iface.addr.address)
                PROXY_HOST = iface.addr.address
        else:
            initial_output += "Warning: could not get WiFi address; assuming %s\n" % PROXY_HOST

        if iftypes["cell"]:
            iface_ipv4 = next(
                (i for i in iftypes["cell"] if i.addr.family == socket.AF_INET and is_routable_interface(i.name)),
                None,
            )
            if iface_ipv4:
                initial_output += "Will connect to IPv4 servers over interface %s at %s\n" % (iface_ipv4.name, iface_ipv4.addr.address)
                CONNECT_HOST_IPV4 = iface_ipv4.addr.address
                is_vpn = iface_ipv4.name.startswith("utun")
                ipv6_addr = resolve_ipv6_for_interface(iface_ipv4.name, is_vpn=is_vpn)
                if ipv6_addr:
                    CONNECT_HOST_IPV6 = test_ipv6_connectivity(ipv6_addr)
                    if CONNECT_HOST_IPV6:
                        initial_output += "Will connect to IPv6 servers at %s\n" % CONNECT_HOST_IPV6
    except Exception as e:
        logging.error("Address detection failed: %s: %s", type(e).__name__, e)
        import traceback
        traceback.print_exc()

    wpad_server = create_wpad_server(LISTEN_HOST, WPAD_PORT, PROXY_HOST, SOCKS_PORT)

    initial_output += "PAC URL: http://{}:{}/wpad.dat\n".format(PROXY_HOST, WPAD_PORT)
    initial_output += "SOCKS Address: {}:{}\n".format(PROXY_HOST or LISTEN_HOST, SOCKS_PORT)
    initial_output += "HTTP Proxy Address: {}:{}\n".format(PROXY_HOST or LISTEN_HOST, HTTP_PORT)
    stats = StatusMonitor(initial_output)
    logging.getLogger().addHandler(stats)

    thread = threading.Thread(target=lambda: wpad_server.serve_forever(), daemon=True)
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
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
        wpad_server.shutdown()


if __name__ == "__main__":
    if IS_PYTHONISTA:
        run_with_ui()
    else:
        run_console()
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import py_compile; py_compile.compile('socks5.py', doraise=True); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add socks5.py
git commit -m "feat: add GUI mode with ServerManager, keep console fallback"
```

---

### Task 6: End-to-end verification

Verify the whole thing loads and the non-Pythonista console path still works.

**Files:** (none modified)

- [ ] **Step 1: Verify all imports resolve**

Run: `python -c "from lib.interfaces import get_labeled_interfaces; from lib.ui_view import ProxyUIView; from lib.proxy_server import AsyncProxyServer; print('All imports OK')"`

Note: This will fail if Pythonista `ui` module is not available. On non-Pythonista, verify:
Run: `python -c "from lib.interfaces import get_labeled_interfaces; from lib.proxy_server import AsyncProxyServer; print('Core imports OK')"`
Expected: `Core imports OK`

- [ ] **Step 2: Verify console mode starts without errors**

Run: `timeout 3 python socks5.py || true`
Expected: Server starts and shows interface detection output (or times out after 3s which is fine)

- [ ] **Step 3: Final commit if any adjustments needed**

```bash
git add -A
git commit -m "fix: adjustments from end-to-end verification"
```
