"""PowerView Gen3 gateway interaction.

Async port of scripts/extract_gateway3_homekey.py for use inside the
integration. Used by the config flow to pull the AES home key out of a
local PowerView gateway so users don't have to hand-edit const.py.

The gateway speaks the same byte-level GetShadeKey protocol as the shades
themselves, just over HTTP instead of BLE. Each shade in a home returns
the same 16-byte home key — we only need one successful shade query.
"""

from __future__ import annotations

import base64
import struct
from typing import Any, Final

import aiohttp

from .const import LOGGER

GATEWAY_HTTP_TIMEOUT: Final[int] = 10
DEFAULT_GATEWAY_HOST: Final[str] = "http://powerview-g3.local"


class GatewayError(Exception):
    """Anything went wrong talking to the PowerView gateway."""


def _frame(sid: int, cid: int, sequence_id: int, data: bytes) -> bytes:
    return struct.pack("<BBBB", sid, cid, sequence_id, len(data)) + data


def _decode(packet: bytes) -> dict[str, Any]:
    if len(packet) < 4:
        raise GatewayError("Response packet too small")
    sid, cid, seq, length = struct.unpack("<BBBB", packet[0:4])
    if len(packet) != 4 + length or length < 1:
        raise GatewayError("Malformed response packet")
    (error_code,) = struct.unpack("<B", packet[4:5])
    return {"cid": cid, "sid": sid, "seq": seq, "err": error_code, "data": packet[5:]}


def _normalize_host(host: str) -> str:
    """Accept 'powerview-g3.local', '192.168.1.50', or a full URL."""
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host


async def _list_shades(
    session: aiohttp.ClientSession, host: str
) -> list[dict[str, Any]]:
    url = f"{host}/home/shades"
    async with session.get(url, timeout=GATEWAY_HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _get_shade_key(
    session: aiohttp.ClientSession, host: str, ble_name: str
) -> bytes:
    """Query one shade via the gateway; the returned key is the home key."""
    req = _frame(251, 18, 1, b"")  # GetShadeKey
    url = f"{host}/home/shades/exec?shades={ble_name}"
    async with session.post(
        url, json={"hex": req.hex()}, timeout=GATEWAY_HTTP_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        result = await resp.json(content_type=None)
    if result.get("err") != 0 or len(result.get("responses", [])) != 1:
        raise GatewayError(f"Gateway rejected GetShadeKey for {ble_name}")
    decoded = _decode(bytes.fromhex(result["responses"][0]["hex"]))
    if decoded["err"] != 0:
        raise GatewayError(f"BLE errorCode {decoded['err']} for {ble_name}")
    if len(decoded["data"]) != 16:
        raise GatewayError(
            f"Expected 16-byte home key from {ble_name}, got {len(decoded['data'])}"
        )
    return decoded["data"]


async def probe_gateway(host: str) -> dict[str, Any]:
    """Confirm a host is a PowerView gateway and return basic info.

    Returns {"host": normalized_host, "shade_count": int, "sample_name": str}
    or raises GatewayError.
    """
    host = _normalize_host(host)
    async with aiohttp.ClientSession() as session:
        try:
            shades = await _list_shades(session, host)
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise GatewayError(f"Cannot reach gateway at {host}: {ex}") from ex
    if not isinstance(shades, list) or not shades:
        raise GatewayError(f"Gateway at {host} reports no shades")
    sample = shades[0]
    try:
        sample_name = base64.b64decode(sample.get("name", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        sample_name = sample.get("bleName", "(unnamed)")
    return {"host": host, "shade_count": len(shades), "sample_name": sample_name}


async def extract_home_key(host: str) -> str:
    """Extract the 16-byte home key from a PowerView gateway, hex-encoded.

    Tries each shade in turn — some shades may be powered off or out of
    range from the gateway. Any single successful response is sufficient
    because all shades in one home share the same key.
    """
    host = _normalize_host(host)
    async with aiohttp.ClientSession() as session:
        try:
            shades = await _list_shades(session, host)
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise GatewayError(f"Cannot reach gateway at {host}: {ex}") from ex
        if not shades:
            raise GatewayError("Gateway returned no shades to query")
        last_err: Exception | None = None
        for shade in shades:
            ble_name = shade.get("bleName")
            if not ble_name:
                continue
            try:
                key = await _get_shade_key(session, host, ble_name)
                LOGGER.debug(
                    "Got home key from gateway %s via shade %s", host, ble_name
                )
                return key.hex()
            except (aiohttp.ClientError, TimeoutError, GatewayError) as ex:
                LOGGER.debug("GetShadeKey via %s failed: %s", ble_name, ex)
                last_err = ex
                continue
    raise GatewayError(
        f"Gateway reachable but no shade returned a key (last error: {last_err})"
    )
