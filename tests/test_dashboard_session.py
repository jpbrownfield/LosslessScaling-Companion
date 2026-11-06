import asyncio
import json
from pathlib import Path
import re
import tempfile
import unittest
import urllib.request
from unittest.mock import AsyncMock

from websockets.legacy.client import connect
from websockets.exceptions import ConnectionClosed

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.server import CompanionWebSocketServer


class DashboardSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_rejects_old_credentials_and_serves_a_new_authorized_session(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            manager.config.port = 0

            async def start():
                server = CompanionWebSocketServer(manager, AppState())
                server._addon_update_loop = AsyncMock()
                server._detect_presentmon_on_startup = AsyncMock()
                await server.start()
                return server

            first = await start()
            port = first.server.sockets[0].getsockname()[1]
            origin = f'http://127.0.0.1:{port}'

            async def page_token():
                def read():
                    with urllib.request.urlopen(origin + '/dashboard') as response:
                        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                        return re.search(r'<meta name="companion-session" content="([^"]+)"', response.read().decode()).group(1)
                return await asyncio.to_thread(read)

            try:
                old_token = await page_token()
                async with connect(f'{origin.replace("http://", "ws://")}/ws?token={old_token}', origin=origin) as socket:
                    self.assertEqual(json.loads(await socket.recv())['type'], 'INITIAL_STATE')
            finally:
                await first.stop()

            manager.config.port = port
            second = await start()
            try:
                async with connect(f'{origin.replace("http://", "ws://")}/ws?token={old_token}', origin=origin) as socket:
                    with self.assertRaises(ConnectionClosed):
                        await socket.recv()
                    self.assertEqual(socket.close_code, 4001)
                self.assertEqual(second.state.connected_clients, 0)
                new_token = await page_token()
                self.assertNotEqual(old_token, new_token)
                async with connect(f'{origin.replace("http://", "ws://")}/ws?token={new_token}', origin=origin) as socket:
                    self.assertEqual(json.loads(await socket.recv())['type'], 'INITIAL_STATE')
                    await socket.send(json.dumps({'type': 'SET_EDITOR_ACTIVITY', 'editing': True}))
                    await socket.send(json.dumps({'type': 'PING'}))
                    self.assertEqual(json.loads(await socket.recv())['type'], 'PONG')
                    self.assertEqual(len(second._editing_clients), 1)
                async with connect(f'{origin.replace("http://", "ws://")}/ws?token={new_token}', origin='https://untrusted.example') as socket:
                    with self.assertRaises(ConnectionClosed):
                        await socket.recv()
                    self.assertEqual(socket.close_code, 1008)
            finally:
                await second.stop()
