"""Gizwits app-side WebSocket client.

Connects to ``wss://wxm2m.gizwits.com:8880/ws/app/v1`` (or the regional
equivalent), logs in with the same uid/token the REST client already
holds, and subscribes to state pushes for the configured ``did``.

Replaces 30-second polling of ``/app/devdata/<did>/latest`` with
near-realtime ``s2c_noti`` push messages. Writes can also go through the
same channel via ``c2s_write`` — the JSON ``attrs`` payload is identical
to the REST ``/app/control`` endpoint, so we just route both write paths
through here once connected.

Protocol reference:
https://docs.gizwits.com/en-us/cloud/WebsocketAPI.html
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional, Union

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Region-specific WebSocket hosts. These match the ``host`` field the
# REST ``/app/bindings`` response advertises for each device — the same
# m2m endpoint the device itself uses for its MQTT control channel, just
# accessed over WebSocket from the app side. Verified by probing:
# wxm2m.gizwits.com:8880 isn't reachable, and wxm2m.gizwits.com:443
# rejects login_req for this app id; ``usm2m.gizwits.com:8880/ws/app/v1``
# accepts login + subscribe for our doser.
WS_HOSTS = {
    "us": "usm2m.gizwits.com",
    "eu": "eum2m.gizwits.com",
    "cn": "m2m.gizwits.com",
}
WS_PORT = 8880
WS_PATH = "/ws/app/v1"

# Cloud asks us to ping on a configurable interval. We default to 60 s
# (well inside the broker's typical idle-disconnect window) and react to
# the actual value the cloud confirms in its ``login_res``.
DEFAULT_HEARTBEAT = 60

# Reconnect backoff bounds. The cloud rate-limits aggressive reconnects.
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0

AttrsCallback = Callable[[str, "dict[str, Any]"], Union[Awaitable[None], None]]


class GizwitsWebSocketError(Exception):
    """Raised for WebSocket-level failures the caller might want to know
    about. Connection / reconnection happens in the background and is
    logged but not raised."""


class GizwitsWebSocketClient:
    """Persistent WebSocket subscription to one or more devices.

    Lifecycle:
      * ``start()`` kicks off the connect-reconnect loop in the background.
      * ``register_device(did, on_attrs)`` queues a subscription and a
        callback for when push notifications land. Multiple devices can
        be registered on the same socket.
      * ``write_attrs(did, attrs)`` sends a control command. If the socket
        isn't connected the write is rejected with an exception so the
        caller can fall back to REST.
      * ``stop()`` cancels the background task and closes the socket.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        app_id: str,
        region: str,
        uid: str,
        token: str,
        token_refresh: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._session = session
        self._app_id = app_id
        self._region = region
        self._uid = uid
        self._token = token
        # When auth fails mid-flight (token expired etc.) ask the REST
        # client to re-login, then we can carry on with the new token.
        self._token_refresh = token_refresh

        self._devices: dict[str, AttrsCallback] = {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._send_lock = asyncio.Lock()
        self._login_ok = asyncio.Event()
        self._heartbeat = DEFAULT_HEARTBEAT
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._req_sn = 0

    # ------------------------------------------------------------ properties

    @property
    def connected(self) -> bool:
        """True when the socket is open and the login handshake has
        succeeded. False during a reconnect window."""
        return (
            self._ws is not None
            and not self._ws.closed
            and self._login_ok.is_set()
        )

    @property
    def url(self) -> str:
        host = WS_HOSTS.get(self._region, WS_HOSTS["us"])
        return f"wss://{host}:{WS_PORT}{WS_PATH}"

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="jebao-gizwits-ws")

    async def stop(self) -> None:
        self._stopping = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # pylint: disable=broad-except
                pass
        self._ws = None
        self._login_ok.clear()

    # ----------------------------------------------------------- subscribers

    def register_device(self, did: str, on_attrs: AttrsCallback) -> None:
        """Subscribe to ``did``'s push notifications. If the socket is
        already connected an immediate ``subscribe_req`` is queued. If not,
        the connect loop sends it after login."""
        self._devices[did] = on_attrs
        if self.connected:
            asyncio.create_task(self._send_subscribe([did]))

    def unregister_device(self, did: str) -> None:
        self._devices.pop(did, None)

    # ---------------------------------------------------------------- writes

    async def write_attrs(self, did: str, attrs: dict[str, Any]) -> None:
        """Send a ``c2s_write`` control command. Kept available for
        completeness but **not currently used by the integration** —
        live testing showed the cloud ACKs c2s_write but the change
        doesn't reach the device. Writes go through the REST
        ``/app/control`` endpoint instead, and the cloud pushes the
        resulting state back over this same WebSocket within ~1 s, so
        the UI updates near-realtime anyway."""
        if not self.connected:
            raise GizwitsWebSocketError("WebSocket not connected")
        self._req_sn += 1
        await self._send({
            "cmd": "c2s_write",
            "req_sn": self._req_sn,
            "data": {"did": did, "attrs": attrs},
        })

    # ----------------------------------------------------------------- core

    async def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stopping:
            try:
                async with self._session.ws_connect(
                    self.url, heartbeat=None, autoping=False
                ) as ws:
                    self._ws = ws
                    self._login_ok.clear()
                    _LOGGER.info("Gizwits WS connected to %s", self.url)
                    await self._send({
                        "cmd": "login_req",
                        "data": {
                            "appid": self._app_id,
                            "uid": self._uid,
                            "token": self._token,
                            "p0_type": "attrs_v4",
                            "heartbeat_interval": DEFAULT_HEARTBEAT,
                            "auto_subscribe": False,
                        },
                    })
                    # Reset backoff *after* we've at least opened a socket;
                    # actual login success is checked inside the loop.
                    backoff = RECONNECT_MIN
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Gizwits WS connection error: %s", err)
            finally:
                self._login_ok.clear()
                self._ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._heartbeat_task = None

            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _listen(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.warning("Gizwits WS: bad JSON: %s", msg.data[:120])
                    continue
                await self._handle(payload)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSE,
            ):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                _LOGGER.warning("Gizwits WS error frame: %s", ws.exception())
                break

    async def _handle(self, payload: dict[str, Any]) -> None:
        cmd = payload.get("cmd")
        if cmd == "login_res":
            data = payload.get("data") or {}
            if data.get("success"):
                self._login_ok.set()
                _LOGGER.info("Gizwits WS login OK")
                # Subscribe to anything already registered.
                if self._devices:
                    await self._send_subscribe(list(self._devices))
                # Start heartbeat now that we're authenticated.
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(), name="jebao-gizwits-ws-hb",
                    )
            else:
                _LOGGER.error("Gizwits WS login rejected: %s", data)
                # Surface to the caller in case the token went stale.
                if self._token_refresh is not None:
                    try:
                        await self._token_refresh()
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.exception("Token refresh failed")
                # Close the socket so _run reconnects with whatever the
                # refresh handler did to our token.
                if self._ws:
                    await self._ws.close()
        elif cmd == "subscribe_res":
            data = payload.get("data") or {}
            for entry in data.get("failed") or []:
                _LOGGER.warning(
                    "Gizwits WS subscribe failed for %s: %s",
                    entry.get("did"), entry.get("msg"),
                )
        elif cmd == "s2c_noti":
            data = payload.get("data") or {}
            did = data.get("did")
            attrs = data.get("attrs") or {}
            cb = self._devices.get(did)
            if cb is None:
                return
            try:
                result = cb(did, attrs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("s2c_noti handler raised")
        elif cmd == "s2c_online":
            data = payload.get("data") or {}
            _LOGGER.debug("Device %s online", data.get("did"))
        elif cmd == "s2c_offline":
            data = payload.get("data") or {}
            _LOGGER.info("Device %s went offline", data.get("did"))
        elif cmd == "pong":
            pass  # heartbeat ack
        elif cmd in ("c2s_write_res", "s2c_res"):
            pass  # control ack — REST mode never saw an ack body either
        elif cmd == "s2c_invalid_msg":
            _LOGGER.warning("Gizwits WS rejected a message: %s", payload)
        else:
            _LOGGER.debug("Gizwits WS unhandled cmd: %s payload=%s", cmd, payload)

    async def _send_subscribe(self, dids: list[str]) -> None:
        if not dids:
            return
        await self._send({
            "cmd": "subscribe_req",
            "data": [{"did": d} for d in dids],
        })

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise GizwitsWebSocketError("WebSocket not open")
        async with self._send_lock:
            await self._ws.send_str(json.dumps(payload))

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopping and self._ws and not self._ws.closed:
                await asyncio.sleep(self._heartbeat)
                try:
                    await self._send({"cmd": "ping"})
                except Exception:  # pylint: disable=broad-except
                    return
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------- credential rotate

    def update_credentials(self, uid: str, token: str) -> None:
        """Called by the REST client when it re-logs in and the token
        rotates. The next reconnect picks up the new pair."""
        self._uid = uid
        self._token = token
