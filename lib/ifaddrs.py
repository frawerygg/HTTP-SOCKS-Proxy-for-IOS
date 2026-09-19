# adapted from https://stackoverflow.com/a/30495952/1204143
import collections
import socket
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    addressof,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_ubyte,
    c_uint,
    c_ushort,
    c_void_p,
    cast,
    get_errno,
    memmove,
    memset,
    sizeof,
)


def get_sockaddr(sockaddr_p):
    if not sockaddr_p:
        return None
    sa_len, sa_family = cast(sockaddr_p, POINTER(c_ubyte * 2)).contents
    if sa_family == socket.AF_INET:
        sin = copy_zerofill(SockaddrIn(), sockaddr_p, sa_len)
        packed = bytes(sin.sin_addr)
        try:
            address = socket.inet_ntop(socket.AF_INET, packed)
        except (OSError, ValueError) as exc:
            raise OSError("failed to decode IPv4 sockaddr: %s" % exc)
        return SocketAddress(sa_family, address)
    elif sa_family == socket.AF_INET6:
        sin6 = copy_zerofill(SockaddrIn6(), sockaddr_p, sa_len)
        packed = bytes(sin6.sin6_addr)
        try:
            address = socket.inet_ntop(socket.AF_INET6, packed)
        except (OSError, ValueError) as exc:
            raise OSError("failed to decode IPv6 sockaddr: %s" % exc)
        if sin6.sin6_scope_id and address.lower().startswith("fe80:"):
            # Preserve the scope information when iOS reports a link-local
            # IPv6 address.  Callers can strip the suffix when comparing the
            # literal address, but retaining it avoids ambiguous output.
            address = "%s%%%d" % (address, sin6.sin6_scope_id)
        return SocketAddress(sa_family, address)
    else:
        sa_data = cast(sockaddr_p, POINTER(c_char * sa_len)).contents.raw
        return SocketAddress(sa_family, sa_data)


def copy_zerofill(dstobj, srcptr, srclen):
    dstlen = sizeof(dstobj)
    memset(addressof(dstobj), 0, dstlen)
    memmove(addressof(dstobj), srcptr, min(dstlen, srclen))
    return dstobj


class SockaddrIn(Structure):
    _fields_ = [
        ("sin_len", c_ubyte),
        ("sin_family", c_ubyte),
        ("sin_port", c_ushort),
        ("sin_addr", c_ubyte * 4),
    ]


class SockaddrIn6(Structure):
    _fields_ = [
        ("sin6_len", c_ubyte),
        ("sin6_family", c_ubyte),
        ("sin6_port", c_ushort),
        ("sin6_flowinfo", c_uint),
        ("sin6_addr", c_ubyte * 16),
        ("sin6_scope_id", c_uint),
    ]


class Sockaddr(Structure):
    _fields_ = [("sa_len", c_ubyte), ("sa_family", c_ubyte), ("sa_data", c_char * 14)]


class Ifaddrs(Structure):
    pass


Ifaddrs._fields_ = [
    ("ifa_next", POINTER(Ifaddrs)),
    ("ifa_name", c_char_p),
    ("ifa_flags", c_uint),
    ("ifa_addr", POINTER(Sockaddr)),
    ("ifa_netmask", POINTER(Sockaddr)),
    ("ifa_dstaddr", POINTER(Sockaddr)),
    ("ifa_data", c_void_p),
]

try:
    libc = CDLL("libSystem.dylib", use_errno=True)
except OSError:
    from ctypes.util import find_library

    libc = CDLL(find_library("libSystem.dylib"), use_errno=True)
libc.getifaddrs.restype = c_int
libc.getifaddrs.argtypes = [POINTER(POINTER(Ifaddrs))]
libc.freeifaddrs.restype = None
libc.freeifaddrs.argtypes = [POINTER(Ifaddrs)]


def errno():
    return get_errno()


SocketAddress = collections.namedtuple("SocketAddress", "family address")
Interface = collections.namedtuple("Interface", "name flags addr netmask dstaddr")


def get_interfaces():
    ifaddr_p = POINTER(Ifaddrs)()
    ret = libc.getifaddrs(byref(ifaddr_p))
    if ret < 0:
        raise OSError("getifaddrs failed: errno=%d" % errno())

    interfaces = []
    head = ifaddr_p
    try:
        while ifaddr_p:
            ifaddr = ifaddr_p.contents
            interfaces.append(
                Interface(
                    ifaddr.ifa_name.decode(errors="replace"),
                    ifaddr.ifa_flags,
                    get_sockaddr(ifaddr.ifa_addr),
                    get_sockaddr(ifaddr.ifa_netmask),
                    get_sockaddr(ifaddr.ifa_dstaddr),
                )
            )
            ifaddr_p = ifaddr_p.contents.ifa_next
        return interfaces
    finally:
        if head:
            libc.freeifaddrs(head)


if __name__ == "__main__":
    for iface in get_interfaces():
        print(iface)
