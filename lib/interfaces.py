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
