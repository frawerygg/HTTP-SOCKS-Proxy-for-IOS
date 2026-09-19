import asyncio
import io
import logging
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib import parse

from . import status
from .proxy_server import (
    AsyncProxyHandler,
    AsyncProxyServer,
    SocketAddress,
    Socks5AddressType,
    forwarder_loop,
)

logger = logging.getLogger("http")

_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEX_CHUNK_SIZE = re.compile(br"^[0-9A-Fa-f]+$")


class HTTPBodyError(ValueError):
    pass


class AsyncHTTPProxyHandler(AsyncProxyHandler, BaseHTTPRequestHandler):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: "AsyncProxyServer",
    ):
        AsyncProxyHandler.__init__(self, reader, writer, server)
        # Skip BaseHTTPRequestHandler.__init__; it assumes blocking rfile/wfile.
        self.wfile = writer

    async def _readline(self, limit):
        line = await asyncio.wait_for(
            self.reader.readline(), timeout=self.server.handshake_timeout
        )
        if len(line) > limit:
            raise ValueError("HTTP line exceeds configured limit")
        return line

    async def handle(self):
        try:
            self.raw_requestline = await self._readline(8192)
            if not self.raw_requestline:
                return

            max_headers = int(getattr(self.server, "http_max_header_bytes", 65536))
            headers = bytearray()
            while True:
                line = await self._readline(min(8192, max_headers + 1))
                headers += line
                if len(headers) > max_headers:
                    self.writer.write(
                        b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                    await self.writer.drain()
                    return
                if line in (b"\r\n", b"\n", b""):
                    break
            self.rfile = io.BytesIO(headers)

            if not self.parse_request():
                return

            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED, "Unsupported method (%r)" % self.command
                )
                return

            await getattr(self, mname)()
            await self.writer.drain()
        except HTTPBodyError as exc:
            logger.warning("%s: HTTP request body rejected: %s", self.log_tag, exc)
            if not self.writer.is_closing():
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                try:
                    await self.writer.drain()
                except ConnectionError:
                    pass
        except ValueError as exc:
            logger.warning("%s: HTTP request rejected: %s", self.log_tag, exc)
            if not self.writer.is_closing():
                self.writer.write(
                    b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
                try:
                    await self.writer.drain()
                except ConnectionError:
                    pass
        except asyncio.TimeoutError as exc:
            logger.warning("%s: HTTP handshake timed out: %s", self.log_tag, exc)
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error("%s: %s: %s", self.log_tag, type(e).__name__, e)

    def log_error(self, format, *args):
        logger.error("%s: " + format, self.log_tag, *args)

    def log_message(self, format, *args):
        logger.info("%s: " + format, self.log_tag, *args)

    @staticmethod
    def _authority_address(authority, default_port):
        parsed = parse.urlsplit("//" + authority)
        if not parsed.hostname:
            raise ValueError("missing host")
        return parsed.hostname, parsed.port or default_port

    async def do_CONNECT(self):
        try:
            address = self._authority_address(self.path, 443)
        except (TypeError, ValueError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, "bad CONNECT target: %s" % exc)
            return

        try:
            connection = await self.server.tcp_connect(
                Socks5AddressType.DOMAIN, address
            )
        except Exception as e:
            self.send_error(
                HTTPStatus.BAD_GATEWAY,
                "Unable to connect to host %s: %s" % (address, e),
            )
            return

        s_writer = connection[1]
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            await self.writer.drain()
            await self.tcp_forward(connection)
        finally:
            if not s_writer.is_closing():
                s_writer.close()
            try:
                await s_writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass

    def _validated_framing(self):
        connection_tokens = set()
        for value in self.headers.get_all("Connection", []):
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
        if connection_tokens.intersection({"content-length", "transfer-encoding"}):
            raise ValueError("Connection nominates an HTTP framing field")

        transfer_values = self.headers.get_all("Transfer-Encoding", [])
        length_values = self.headers.get_all("Content-Length", [])
        if transfer_values and length_values:
            raise ValueError("both Transfer-Encoding and Content-Length are present")

        if transfer_values:
            codings = []
            for value in transfer_values:
                for item in value.split(","):
                    item = item.strip()
                    if not item:
                        raise ValueError("empty transfer coding")
                    name, separator, parameters = item.partition(";")
                    name = name.strip().lower()
                    if not _HTTP_TOKEN.fullmatch(name):
                        raise ValueError("malformed transfer coding")
                    if name == "chunked" and (separator or parameters):
                        raise ValueError("chunked transfer coding cannot have parameters")
                    codings.append((name, item))
            names = [name for name, _ in codings]
            if not names or names[-1] != "chunked" or names.count("chunked") != 1:
                raise ValueError("final transfer coding must be chunked")
            return "chunked", None, ", ".join(item for _, item in codings)

        if length_values:
            lengths = []
            for value in length_values:
                for item in value.split(","):
                    item = item.strip()
                    if not item or not item.isdigit():
                        raise ValueError("malformed Content-Length")
                    lengths.append(int(item, 10))
            if not lengths or any(length != lengths[0] for length in lengths[1:]):
                raise ValueError("conflicting Content-Length values")
            return "length", lengths[0], str(lengths[0])

        return "none", 0, None

    def _forward_headers(self):
        # RFC hop-by-hop headers plus every header named by Connection must not
        # be blindly forwarded to the next hop. Framing and Host are emitted
        # separately from the validated request-target decision.
        connection_tokens = set()
        for value in self.headers.get_all("Connection", []):
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
        blocked = {
            "connection",
            "proxy-connection",
            "proxy-authorization",
            "proxy-authenticate",
            "keep-alive",
            "te",
            "trailer",
            "upgrade",
            "host",
            "content-length",
            "transfer-encoding",
        }
        blocked.update(connection_tokens)
        return [
            (key, value)
            for key, value in self.headers.items()
            if key.lower() not in blocked
        ]

    async def _forward_request_body(self, s_writer, framing, content_length):
        if framing == "chunked":
            while True:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("client closed during chunked request")
                if len(line) > 8192:
                    raise ValueError("chunk header too large")
                if not line.endswith(b"\r\n"):
                    raise HTTPBodyError("malformed chunk header terminator")
                size_text = line.split(b";", 1)[0].strip()
                if not _HEX_CHUNK_SIZE.fullmatch(size_text):
                    raise HTTPBodyError("malformed chunk size")
                size = int(size_text, 16)
                s_writer.write(line)
                self.server.traffic_stats.add_outbound(len(line))
                if size:
                    remaining = size
                    while remaining:
                        data = await self.reader.readexactly(min(65536, remaining))
                        remaining -= len(data)
                        s_writer.write(data)
                        self.server.traffic_stats.add_outbound(len(data))
                        await s_writer.drain()
                    terminator = await self.reader.readexactly(2)
                    if terminator != b"\r\n":
                        raise HTTPBodyError("malformed chunk data terminator")
                    s_writer.write(terminator)
                    self.server.traffic_stats.add_outbound(len(terminator))
                else:
                    # Forward optional trailer fields through the terminating
                    # blank line.  Their aggregate is bounded like headers.
                    trailer_total = 0
                    while True:
                        trailer = await self.reader.readline()
                        if not trailer:
                            raise ConnectionError("client closed during chunk trailers")
                        trailer_total += len(trailer)
                        if trailer_total > self.server.http_max_header_bytes:
                            raise ValueError("chunk trailers too large")
                        s_writer.write(trailer)
                        self.server.traffic_stats.add_outbound(len(trailer))
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                await s_writer.drain()
        elif framing == "length":
            remaining = content_length
            while remaining:
                chunk = await self.reader.read(min(65536, remaining))
                if not chunk:
                    raise ConnectionError("client closed during request body")
                remaining -= len(chunk)
                self.server.traffic_stats.add_outbound(len(chunk))
                s_writer.write(chunk)
                await s_writer.drain()

    async def do_verb(self):
        parsed = parse.urlsplit(self.path)
        if parsed.scheme == "http":
            default_port = 80
        elif parsed.scheme == "https":
            self.send_error(
                HTTPStatus.BAD_REQUEST,
                "absolute HTTPS requests require CONNECT",
            )
            return
        else:
            self.send_error(HTTPStatus.BAD_REQUEST, "bad scheme %s" % parsed.scheme)
            return

        if parsed.fragment or not parsed.hostname or parsed.username is not None:
            self.send_error(HTTPStatus.BAD_REQUEST, "bad url %s" % self.path)
            return

        try:
            address: SocketAddress = (parsed.hostname, parsed.port or default_port)
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, "bad url port: %s" % exc)
            return

        host_values = self.headers.get_all("Host", [])
        try:
            for host_value in host_values:
                if self._authority_address(host_value.strip(), default_port) != address:
                    raise ValueError("Host does not match absolute request target")
            framing, content_length, framing_value = self._validated_framing()
        except (TypeError, ValueError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid HTTP request: %s" % exc)
            return

        try:
            connection = await self.server.tcp_connect(
                Socks5AddressType.DOMAIN, address
            )
        except Exception as e:
            self.send_error(
                HTTPStatus.BAD_GATEWAY,
                "Unable to connect to host %s: %s" % (address, e),
            )
            return

        s_reader, s_writer = connection
        try:
            self.log_request()
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            request = bytearray(
                ("%s %s %s\r\n" % (self.command, path, self.request_version)).encode(
                    "utf8"
                )
            )
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = "[" + host + "]"
            if address[1] != default_port:
                host += ":%d" % address[1]
            request += ("Host: %s\r\n" % host).encode("utf8")
            for key, value in self._forward_headers():
                request += ("%s: %s\r\n" % (key, value)).encode("utf8")
            if framing == "chunked":
                request += ("Transfer-Encoding: %s\r\n" % framing_value).encode("utf8")
            elif framing == "length":
                request += ("Content-Length: %s\r\n" % framing_value).encode("ascii")
            request += b"Connection: close\r\n\r\n"
            s_writer.write(request)
            await s_writer.drain()
            self.server.traffic_stats.add_outbound(len(request))

            await self._forward_request_body(s_writer, framing, content_length)
            try:
                if s_writer.can_write_eof():
                    s_writer.write_eof()
                    await s_writer.drain()
            except (AttributeError, ConnectionError, OSError):
                pass
            await forwarder_loop(
                s_reader, self.writer, self.server.traffic_stats.add_inbound
            )
        finally:
            if not s_writer.is_closing():
                s_writer.close()
            try:
                await s_writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError, OSError):
                pass

    do_GET = do_verb
    do_HEAD = do_verb
    do_POST = do_verb
    do_PUT = do_verb
    do_DELETE = do_verb
    do_OPTIONS = do_verb


if __name__ == "__main__":
    stats = status.StatusMonitor("HTTP Server", interval=1)
    logging.getLogger().addHandler(stats)

    async def main() -> None:
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_port=9877, traffic_stats=stats
        )
        asyncio.create_task(server.run())
        await stats.render_forever()

    asyncio.run(main())
