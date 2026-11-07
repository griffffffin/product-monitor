"""The shared HTTP session: IPv4 only.

The Pi has no IPv6 route. With both address families allowed, the c-ares
resolver (aiodns, which aiohttp uses by itself on the Pi) can answer with only
the IPv6 addresses when the A answer is late; every connect then fails with
"Network is unreachable", and the connector's DNS cache serves that answer for
its whole TTL (seen in production: ~6 one-minute XiaomiPlanet ticks per such
failure, all searches of a site failing together).
"""

import asyncio
import socket

import product_monitor as pm


def make_monitor(tmp_path):
    config = pm.MonitorConfig.from_dict({"searches": []})
    return pm.MultiMarketplaceMonitor(config, str(tmp_path))


class TestSession:
    def test_connector_asks_for_ipv4_only(self, tmp_path):
        async def scenario():
            m = make_monitor(tmp_path)
            await m.create_session()
            try:
                return m.session.connector.family
            finally:
                await m.session.close()

        assert asyncio.run(scenario()) == socket.AF_INET

    def test_the_resolver_is_asked_for_ipv4_only(self, tmp_path):
        """Not just a connector attribute: the family reaches the resolver."""
        asked = []

        class RecordingResolver:
            async def resolve(self, host, port=0, family=socket.AF_INET):
                asked.append(family)
                raise OSError("resolution refused by the test")

            async def close(self):
                pass

        async def scenario():
            m = make_monitor(tmp_path)
            await m.create_session()
            try:
                m.session.connector._resolver = RecordingResolver()
                try:
                    async with m.session.get("http://ipv4-only.invalid/"):
                        pass
                except Exception:
                    pass
            finally:
                await m.session.close()

        asyncio.run(scenario())
        assert asked and set(asked) == {socket.AF_INET}

    def test_requests_still_work_over_ipv4(self, tmp_path):
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        async def scenario():
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            m = make_monitor(tmp_path)
            await m.create_session()
            try:
                async with m.session.get(f"http://localhost:{port}/") as response:
                    return response.status, await response.text()
            finally:
                await m.session.close()
                server.close()
                await server.wait_closed()

        assert asyncio.run(scenario()) == (200, "ok")
