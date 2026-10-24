"""
SCopy for Decky Loader: starts and stops the SCopy web page from the Quick Access menu
on a Steam Deck or Steam Machine. The web page itself is scopy.py, copied into py_modules/.
"""

import asyncio
import os
import sys

import decky

# Settings, remembered computers and Steam's folders all live under the user's home.
os.environ["HOME"] = decky.DECKY_USER_HOME
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules"))

from scopy import IDLE_MINUTES, ShareServer, read_config  # noqa: E402


class Plugin:
    server = None

    async def _main(self):
        folder, port, steamos_folders = read_config()
        self.server = ShareServer(folder, port, steamos_folders=steamos_folders)

    async def _unload(self):
        if self.server:
            self.server.stop()

    async def _uninstall(self):
        pass

    async def status(self):
        server = self.server
        files = await asyncio.to_thread(server.files) if server.active else []
        return {
            "active": server.active,
            "urls": list(server.urls) if server.active else [],
            "pin": server.pin if server.active else "",
            "files": len(files),
            "devices": server.device_count(),
            "idle_minutes": IDLE_MINUTES,
        }

    async def start(self):
        if not self.server.active:
            try:
                self.server.start()
            except OSError as e:
                return {"error": f"Can't share on port {self.server.port}: {e.strerror or e}"}
        return await self.status()

    async def stop(self):
        self.server.stop()
        return await self.status()

    async def forget_all(self):
        self.server.forget_all_devices()
        return await self.status()
