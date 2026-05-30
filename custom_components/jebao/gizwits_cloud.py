"""Gizwits cloud REST client for the MD-4.4 doser.

The MD-4.4's LAN protocol on the firmware shipping today ACKs 0x93 writes
but silently drops them — control only works through the Gizwits cloud
API at ``{region}api.gizwits.com``. This module wraps the handful of
endpoints we need: login, device list, latest datapoints, and the
``/app/control`` write path.

The app id was extracted from the decompiled Jebao Aqua Android app
(``com.gizwits.xb``); see ``custom_components/jebao/const.py``.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from .const import GIZWITS_APP_ID, GIZWITS_REGIONS

_LOGGER = logging.getLogger(__name__)


class GizwitsCloudError(Exception):
    """Anything that goes wrong talking to the Gizwits cloud."""


class GizwitsAuthError(GizwitsCloudError):
    """Bad credentials or expired token."""


class GizwitsCloudClient:
    """Small async wrapper over the Gizwits app REST API.

    Token lifecycle: ``login`` populates ``token`` / ``expire_at`` /
    ``uid``. Callers should check ``needs_relogin()`` before doing work
    that requires auth — or just let the next 401 trigger a relogin.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        region: str = "us",
        app_id: str = GIZWITS_APP_ID,
    ) -> None:
        if region not in GIZWITS_REGIONS:
            raise ValueError(f"Unknown region {region!r}")
        self._session = session
        self.region = region
        self.app_id = app_id
        self.token: str | None = None
        self.uid: str | None = None
        self.expire_at: int = 0
        self._username: str | None = None
        self._password: str | None = None

    @property
    def base_url(self) -> str:
        return GIZWITS_REGIONS[self.region]

    def _headers(self) -> dict[str, str]:
        h = {
            "X-Gizwits-Application-Id": self.app_id,
            "Content-Type": "application/json",
        }
        if self.token:
            h["X-Gizwits-User-token"] = self.token
        return h

    def needs_relogin(self) -> bool:
        if not self.token:
            return True
        # Renew with a bit of slack before the server-side expiry.
        return time.time() >= self.expire_at - 300

    async def login(self, username: str, password: str) -> None:
        """Authenticate and cache the token. Stored credentials are reused
        on subsequent ``ensure_logged_in`` calls."""
        self._username = username
        self._password = password
        await self._do_login()

    async def _do_login(self) -> None:
        if not self._username or not self._password:
            raise GizwitsAuthError("No stored credentials to log in with")
        url = f"{self.base_url}/app/login"
        body = {"username": self._username, "password": self._password, "lang": "en"}
        async with self._session.post(
            url, json=body, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or "token" not in data:
                raise GizwitsAuthError(
                    f"Login failed: HTTP {resp.status}, body={data}"
                )
            self.token = data["token"]
            self.uid = data.get("uid")
            self.expire_at = int(data.get("expire_at", 0))
            _LOGGER.info(
                "Gizwits cloud login OK (uid=%s, expires=%s)",
                self.uid,
                self.expire_at,
            )

    async def ensure_logged_in(self) -> None:
        if self.needs_relogin():
            await self._do_login()

    async def get_bindings(self) -> list[dict[str, Any]]:
        """Return the list of devices bound to this account."""
        await self.ensure_logged_in()
        url = f"{self.base_url}/app/bindings"
        params = {"show_disabled": "0", "limit": "50", "skip": "0"}
        async with self._session.get(
            url, params=params, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                # Token died early — try once with a fresh login.
                await self._do_login()
                return await self.get_bindings()
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise GizwitsCloudError(f"bindings failed: HTTP {resp.status}: {data}")
            return data.get("devices", [])

    async def get_device_data(self, did: str) -> dict[str, Any]:
        """Fetch the latest datapoint values for one device.

        Returns the contents of ``response["attr"]`` — a plain dict of
        attribute-name -> value (bool/int/str depending on data type).
        Schedule blobs come back as hex strings (192 chars = 96 bytes).
        """
        await self.ensure_logged_in()
        url = f"{self.base_url}/app/devdata/{did}/latest"
        async with self._session.get(
            url, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                await self._do_login()
                return await self.get_device_data(did)
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise GizwitsCloudError(
                    f"devdata failed: HTTP {resp.status}: {data}"
                )
            return data.get("attr", {})

    async def control(self, did: str, attrs: dict[str, Any]) -> None:
        """Set one or more attributes on the device.

        The cloud returns an empty body (``{}``) on success; failures
        come back with a non-200 status.
        """
        await self.ensure_logged_in()
        url = f"{self.base_url}/app/control/{did}"
        body = {"attrs": attrs}
        async with self._session.post(
            url, json=body, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                await self._do_login()
                await self.control(did, attrs)
                return
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise GizwitsCloudError(
                    f"control failed: HTTP {resp.status}: {data}"
                )
            _LOGGER.debug("cloud control %s <- %s", did, attrs)
