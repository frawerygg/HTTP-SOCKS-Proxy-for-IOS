""" General base class for proxies """

from __future__ import annotations

import asyncio
import logging
import random
import socket
from asyncio.staggered import staggered_race
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Sequence, Type

from dns.asyncresolver import Resolver
from dns.inet import af_for_address

from . import status

logger = logging.getLogger("proxy")

SocketAddress = tuple[str, int]
Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter]


@dataclass
class GenericAddress:
    ipv4: SocketAddress | None = None
    ipv6: SocketAddress | None = None


HAPPY_EYEBALLS_DELAY = 0.05  # seconds
CONNECT_TIMEOUT = 75  # seconds


async def forwarder_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stat_fn: Callable[[int], None],
) -> None:
    """Forward one TCP direction without destroying the reverse direction.

    EOF means this sender has half-closed.  Propagate EOF when supported, but
    leave final socket closure to ``tcp_forward`` after both directions finish.
    """
    while True:
        buf = await reader.read(65536)
        if not buf:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
            except (AttributeError, ConnectionError, OSError):
                pass
            return
        stat_fn(len(buf))
        writer.write(buf)
        await writer.drain()


# XXX: should make this a more generic address type enum and convert from socks5
class Socks5AddressType(IntEnum):
    IPV4 = 1
    DOMAIN = 3
    IPV6 = 4


class AsyncProxyHandler:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: "AsyncProxyServer",
    ):
        self.reader = reader
        self.writer = writer
        self.server = server

        peer_addr = writer.get_extra_info("peername")
        if peer_addr is None:
            self.log_tag = "<unknown>"
        elif len(peer_addr) == 2:
            # IPv4
            self.log_tag = "%s:%s" % peer_addr
        elif len(peer_addr) == 4:
            # IPv6
            self.log_tag = "[%s]:%s" % peer_addr[:2]
        else:
            self.log_tag = "[%s]" % (peer_addr,)

    async def tcp_forward(self, connection: Connection) -> None:
        s_reader, s_writer = connection
        relay_tasks = [
            asyncio.create_task(
                forwarder_loop(
                    s_reader, self.writer, self.server.traffic_stats.add_inbound
                )
            ),
            asyncio.create_task(
                forwarder_loop(
                    self.reader, s_writer, self.server.traffic_stats.add_outbound
                )
            ),
        ]
        try:
            done, pending = await asyncio.wait(
                relay_tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            failure = next(
                (
                    task.exception()
                    for task in done
                    if not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure

            # FIRST_EXCEPTION waits for both tasks when both directions end by
            # clean EOF.  This preserves TCP half-close and delayed responses.
            await asyncio.gather(*relay_tasks)
        finally:
            for task in relay_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*relay_tasks, return_exceptions=True)
            for writer in (s_writer, self.writer):
                if not writer.is_closing():
                    writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, asyncio.CancelledError, OSError):
                    pass

    async def initial_readexactly(self, size: int) -> bytes:
        return await asyncio.wait_for(
            self.reader.readexactly(size), timeout=self.server.handshake_timeout
        )

    async def handle(self) -> None:
        pass


class AsyncProxyServer:
    def __init__(
        self,
        handler_class: Type[AsyncProxyHandler],
        listen_hosts: str | Sequence[str] = ("::", "0.0.0.0"),
        listen_port: int = 9876,
        traffic_stats: status.TrafficStats | None = None,
        resolver: Resolver | None = None,
        connect_host_ipv4: str | None = None,
        connect_host_ipv6: str | None = None,
        connection_timeout: float = CONNECT_TIMEOUT,
        handshake_timeout: float = 10.0,
        max_client_connections: int = 64,
        http_max_header_bytes: int = 65536,
        udp_max_mappings: int = 256,
        udp_mapping_ttl: float = 60.0,
        udp_max_pending_tasks: int = 128,
    ):
        self.handler_class = handler_class
        self.listen_hosts = listen_hosts
        self.listen_port = listen_port
        self.traffic_stats = traffic_stats or status.SimpleTrafficStats()
        self.resolver = resolver or Resolver()
        self.connect_host_ipv4 = connect_host_ipv4
        self.connect_host_ipv6 = connect_host_ipv6
        self.connection_timeout = connection_timeout
        self.handshake_timeout = max(1.0, float(handshake_timeout))
        self.max_client_connections = max(1, int(max_client_connections))
        self.http_max_header_bytes = max(8192, int(http_max_header_bytes))
        self.udp_max_mappings = max(1, int(udp_max_mappings))
        self.udp_mapping_ttl = max(5.0, float(udp_mapping_ttl))
        self.udp_max_pending_tasks = max(1, int(udp_max_pending_tasks))
        self._server = None
        self._client_tasks: set[asyncio.Task] = set()
        self.resolver_source: str | None = None
        if self.connect_host_ipv4 is not None or self.connect_host_ipv6 is not None:
            resolver_afs = [af_for_address(ns) for ns in self.resolver.nameservers]
            if (
                any(af == socket.AF_INET for af in resolver_afs)
                and self.connect_host_ipv4 is not None
            ):
                self.resolver_source = self.connect_host_ipv4
                self.resolver.nameservers = [
                    ns
                    for ns in self.resolver.nameservers
                    if af_for_address(ns) == socket.AF_INET
                ]
            elif (
                any(af == socket.AF_INET6 for af in resolver_afs)
                and self.connect_host_ipv6 is not None
            ):
                self.resolver_source = self.connect_host_ipv6
                self.resolver.nameservers = [
                    ns
                    for ns in self.resolver.nameservers
                    if af_for_address(ns) == socket.AF_INET6
                ]
            else:
                raise Exception("Resolver does not have any suitable nameservers!")

    async def start(self) -> None:
        """Bind the listener and return only after startup succeeds."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self.client_connected,
            host=self.listen_hosts,
            port=self.listen_port,
            reuse_address=True,
        )
        logger.info(
            "%s listener started: requested=%r port=%d scope=%s bound=%s outbound IPv4=%s IPv6=%s",
            self.handler_class.__name__,
            self.listen_hosts,
            self.listen_port,
            self.listen_scope,
            self.bound_addresses,
            self.connect_host_ipv4 or "system default",
            self.connect_host_ipv6 or "disabled/system default",
        )

    async def run(self) -> None:
        await self.start()
        await self._server.serve_forever()

    @property
    def is_listening(self) -> bool:
        return bool(self._server is not None and self._server.sockets)

    @property
    def connection_limit_reached(self) -> bool:
        return len(self._client_tasks) >= self.max_client_connections

    @property
    def bound_addresses(self):
        if self._server is None or not self._server.sockets:
            return []
        return [sock.getsockname() for sock in self._server.sockets]

    @property
    def listen_scope(self) -> str:
        hosts = self.listen_hosts
        if isinstance(hosts, str):
            hosts = (hosts,)
        if any(host in ("", "0.0.0.0", "::", None) for host in hosts):
            return "all interfaces"
        return "interface/address constrained"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            # Cancel all in-flight client handlers so they don't
            # try to use transports on a dying event loop.
            for task in list(self._client_tasks):
                task.cancel()
            if self._client_tasks:
                done, pending = await asyncio.wait(self._client_tasks, timeout=3)
                if pending:
                    for task in pending:
                        task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
            self._server = None

    async def client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if len(self._client_tasks) >= self.max_client_connections:
            logger.warning(
                "%s rejected client: connection limit %d reached",
                self.handler_class.__name__, self.max_client_connections,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass
            return
        if task is not None:
            self._client_tasks.add(task)
        handler = self.handler_class(reader, writer, server=self)
        peer = writer.get_extra_info("peername")
        local = writer.get_extra_info("sockname")
        log = logger.debug if peer and peer[0] in ("127.0.0.1", "::1") else logger.info
        log(
            "%s accepted client source=%s local-destination=%s",
            self.handler_class.__name__, peer, local,
        )
        self.traffic_stats.add_connection()
        try:
            await handler.handle()
        finally:
            self.traffic_stats.remove_connection()
            if not writer.is_closing():
                writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass
            if task is not None:
                self._client_tasks.discard(task)

    async def ipv4_connect(self, address: SocketAddress) -> Connection:
        local_addr = (
            (self.connect_host_ipv4, 0) if self.connect_host_ipv4 is not None else None
        )
        return await asyncio.wait_for(
            asyncio.open_connection(address[0], address[1], local_addr=local_addr),
            timeout=self.connection_timeout,
        )

    async def ipv6_connect(self, address: SocketAddress) -> Connection:
        local_addr = (
            (self.connect_host_ipv6, 0) if self.connect_host_ipv6 is not None else None
        )
        return await asyncio.wait_for(
            asyncio.open_connection(address[0], address[1], local_addr=local_addr),
            timeout=self.connection_timeout,
        )

    async def tcp_connect(
        self, address_type: int, address: SocketAddress
    ) -> Connection:
        resolved = await self.resolve_address(address_type, address)

        if resolved.ipv4 is not None and resolved.ipv6 is not None:
            ipv6_addr = resolved.ipv6
            ipv4_addr = resolved.ipv4
            # happy eyeballs
            result, result_index, exceptions = await staggered_race(
                [
                    lambda: self.ipv6_connect(ipv6_addr),
                    lambda: self.ipv4_connect(ipv4_addr),
                ],
                delay=HAPPY_EYEBALLS_DELAY,
            )
            if not result:
                raise exceptions[0]
            return result
        elif resolved.ipv4 is not None:
            return await self.ipv4_connect(resolved.ipv4)
        elif resolved.ipv6 is not None:
            return await self.ipv6_connect(resolved.ipv6)
        else:
            raise Exception("Host %s could not be resolved" % (address,))

    async def dummy_resolve(self):
        raise Exception("address family not supported")

    async def _resolve_domain(self, address: SocketAddress) -> GenericAddress:
        domain, port = address

        result = GenericAddress()
        try:
            socket.inet_pton(socket.AF_INET, domain)
            result.ipv4 = address
            return result
        except Exception:
            pass

        try:
            socket.inet_pton(socket.AF_INET6, domain)
            result.ipv6 = address
            return result
        except Exception:
            pass

        if self.connect_host_ipv4 is None and self.connect_host_ipv6 is not None:
            ipv4_resolver = self.dummy_resolve()
        else:
            ipv4_resolver = self.resolver.resolve(
                domain, "A", source=self.resolver_source
            )

        if self.connect_host_ipv4 is not None and self.connect_host_ipv6 is None:
            ipv6_resolver = self.dummy_resolve()
        else:
            ipv6_resolver = self.resolver.resolve(
                domain, "AAAA", source=self.resolver_source
            )

        try:
            ipv4, ipv6 = await asyncio.wait_for(
                asyncio.gather(
                    ipv4_resolver,
                    ipv6_resolver,
                    return_exceptions=True,
                ),
                timeout=self.connection_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise asyncio.TimeoutError(
                "DNS resolution timed out after %.1fs" % self.connection_timeout
            ) from exc
        if not isinstance(ipv4, BaseException) and ipv4:
            result.ipv4 = (random.choice(ipv4).address, port)
        if not isinstance(ipv6, BaseException) and ipv6:
            result.ipv6 = (random.choice(ipv6).address, port)
        return result

    async def resolve_address(
        self, address_type: int, address: SocketAddress
    ) -> GenericAddress:
        if address_type == Socks5AddressType.IPV4:
            result = GenericAddress(ipv4=address)
        elif address_type == Socks5AddressType.DOMAIN:
            result = await self._resolve_domain(address)
        elif address_type == Socks5AddressType.IPV6:
            result = GenericAddress(ipv6=address)

        if self.connect_host_ipv4 is None and self.connect_host_ipv6 is not None:
            result.ipv4 = None
        elif self.connect_host_ipv4 is not None and self.connect_host_ipv6 is None:
            result.ipv6 = None

        return result
