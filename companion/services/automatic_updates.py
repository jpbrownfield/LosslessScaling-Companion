"""Idle-only, serialized updates using the existing verified release installers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

logger = logging.getLogger("LSCompanion.AutoUpdates")
AUTOMATIC_ADDONS = frozenset({"ls-addon-manager", "reshade", "special-k"})


def newer_addon_version(current: str, latest: str) -> bool:
    """Unknown/prerelease formats stay manual rather than risk a downgrade."""
    def version(value):
        text = str(value or "").strip().removeprefix("v")
        return tuple(int(part) for part in text.split(".")) if re.fullmatch(r"\d+(?:\.\d+)+", text) else None
    before, after = version(current), version(latest)
    if before is None or after is None:
        return False
    width = max(len(before), len(after))
    return after + (0,) * (width - len(after)) > before + (0,) * (width - len(before))


class AutomaticUpdates:
    RETRY_SECONDS = 60 * 60

    def __init__(self, server):
        self.server = server
        self.retry_after = {}

    async def run_pending(self) -> None:
        server = self.server
        if (not server.config.automatically_install_updates or server.state.simulation_mode
                or server.profile_manager.repair_session or server.state.update_in_progress
                or server.component_install_lock.locked()
                or server._active_mutations or server._editing_clients
                or (server.diagnostics_task and not server.diagnostics_task.done())
                or (server.benchmark_task and not server.benchmark_task.done())):
            return
        now = time.monotonic()
        addons = [item for item in (server.addon_update_status or {}).get("updates", [])
                  if item.get("provider") in AUTOMATIC_ADDONS
                  and newer_addon_version(item.get("currentVersion"), item.get("latestVersion"))]
        companion = server.companion_update_status or {}
        jobs = [(item["provider"], str(item["latestVersion"])) for item in addons]
        if (companion.get("updateAvailable") and companion.get("supported")
                and server.companion_updater.automatic_retry_allowed(str(companion['latestVersion']))):
            jobs.append(("companion", str(companion["latestVersion"])))
        jobs = [job for job in jobs if self.retry_after.get(job, 0) <= now]
        if not jobs or not await server._run_background_thread(server.automation.begin_update):
            return
        if (server._active_mutations or server._editing_clients or server.component_install_lock.locked()
                or (server.diagnostics_task and not server.diagnostics_task.done())
                or (server.benchmark_task and not server.benchmark_task.done())):
            await server._run_background_thread(server.automation.end_update)
            return

        class UpdateClient:
            error = None

            async def send(self, encoded):
                message = json.loads(encoded)
                if message.get("type") == "ERROR":
                    self.error = message.get("message", "Automatic update failed")
                # Component progress is already broadcast by the shared operation wrapper.
                if message.get("type") != "COMPONENT_OPERATION":
                    await asyncio.gather(*(client.send(encoded) for client in list(server.clients)), return_exceptions=True)

        client = UpdateClient()
        server._automatic_update_client = client
        server.client_authority[client] = "dashboard"
        try:
            for provider, version in jobs:
                if not server.config.automatically_install_updates:
                    break
                # Back off failures, including across the rest of this batch.
                self.retry_after[(provider, version)] = time.monotonic() + self.RETRY_SECONDS
                client.error = None
                message = {"type": "APPLY_COMPANION_UPDATE" if provider == "companion" else "INSTALL_LATEST_GRAPHICS_ADDON",
                           "provider": provider, "version": version, "operationId": uuid.uuid4().hex}
                await server.process_message(client, json.dumps(message))
                if client.error:
                    logger.warning("Automatic %s update failed: %s", provider, client.error)
                    # Leave a visible failure and let the user retry; do not restart over it.
                    break
                if server._update_handoff:
                    break
            if not server._update_handoff:
                await server._refresh_addon_updates(force=False)
                await server.broadcast_full_data_update()
        finally:
            server.client_authority.pop(client, None)
            server._automatic_update_client = None
            if not server._update_handoff:
                await server._run_background_thread(server.automation.end_update)
                if server.component_operation_status:
                    await server._broadcast_component_operation()
