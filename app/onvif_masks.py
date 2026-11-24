"""Write privacy masks to a camera through ONVIF Media2, so its recordings carry them.

ONVIF masks are polygons in the video source's normalized space (-1..1, y up). Each
rectangle drawn in the NVR becomes one filled mask on the camera's video source
configuration. Masks the NVR created before are deleted first, by their tokens.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from .onvif_destination import UnsafeDestination, confine
from .onvif_soap import call as _soap_call, escape as _escape, local as _local
from .onvif_stream import DEVICE, MAX_RESPONSE_BYTES, SCHEMA, SOAP

MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"


class MasksUnsupported(Exception):
    """The camera has no Media2 service or does not accept privacy masks."""


class MaskWriteFailed(Exception):
    """Writing stopped part way; ``created`` names the masks already on the camera, so they can be removed later."""

    def __init__(self, created: list[str]) -> None:
        super().__init__("Privacy masks were only partly written to the camera")
        self.created = created


def _envelope(action: str, body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:tr2="{MEDIA2}" '
            f'xmlns:tt="{SCHEMA}"><s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode("utf-8")


def polygon(zone: dict[str, float]) -> list[tuple[float, float]]:
    """A rectangle from 0..1 picture space (top left origin) as ONVIF points (-1..1, y up)."""
    left, top = zone["x"] * 2 - 1, 1 - zone["y"] * 2
    right, bottom = (zone["x"] + zone["width"]) * 2 - 1, 1 - (zone["y"] + zone["height"]) * 2
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


async def _call(client: httpx.AsyncClient, url: str, action: str, body: str) -> ET.Element:
    return await _soap_call(client, url, action, _envelope(action, body), MAX_RESPONSE_BYTES)


def _client(username: str, password: str) -> httpx.AsyncClient:
    auth = httpx.DigestAuth(username, password) if username else None
    return httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(10, connect=5), trust_env=False, follow_redirects=False)


async def _media2(client: httpx.AsyncClient, device_service_url: str) -> str:
    """The camera's Media2 address, kept to the camera's own host. Raises ``MasksUnsupported`` when it has none."""
    services = await _call(client, device_service_url, f"{DEVICE}/GetServices", "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
    media2 = None
    for service in services.iter():
        if _local(service.tag) != "Service":
            continue
        namespace = next((child.text for child in service if _local(child.tag) == "Namespace"), None)
        if namespace == MEDIA2:
            media2 = next((child.text.strip() for child in service if _local(child.tag) == "XAddr" and child.text), None)
    if not media2:
        raise MasksUnsupported()
    try:
        return confine(device_service_url, media2)
    except UnsafeDestination as exc:
        raise MasksUnsupported() from exc


def _rounded(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    return tuple((round(x, 3), round(y, 3)) for x, y in points)


async def read_masks(device_service_url: str, username: str, password: str) -> dict[str, tuple[bool, tuple[tuple[float, float], ...]]]:
    """The masks the camera holds now (#468): each token with whether it is enabled and its polygon (points rounded to 0.001).

    ``MasksUnsupported`` when the camera has no Media2 service or will not list masks (a different camera at the same address, or one reset to
    a state without them); any other exception when it could not be asked."""
    async with _client(username, password) as client:
        media2 = await _media2(client, device_service_url)
        try:
            reply = await _call(client, media2, f"{MEDIA2}/GetMasks", "<tr2:GetMasks/>")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403} or exc.response.status_code >= 500:
                raise  # Not an answer about masks: the login or the camera failed
            raise MasksUnsupported() from exc
    found: dict[str, tuple[bool, tuple[tuple[float, float], ...]]] = {}
    for node in reply.iter():
        if _local(node.tag) != "Masks":
            continue
        token = node.get("token") or next((child.text.strip() for child in node if _local(child.tag) == "token" and child.text), None)
        if not token:
            continue
        points = [(float(point.get("x", "nan")), float(point.get("y", "nan"))) for point in node.iter() if _local(point.tag) == "Point"]
        enabled = next((child.text.strip().lower() != "false" for child in node.iter() if _local(child.tag) == "Enabled" and child.text), True)
        found[token] = (enabled, _rounded(points))
    return found


def expected_polygons(zones: list[dict[str, float]]) -> set[tuple[tuple[float, float], ...]]:
    """The polygons the NVR wrote for ``zones``, as ``read_masks`` reports them."""
    return {_rounded(polygon(zone)) for zone in zones}


async def write_masks(device_service_url: str, username: str, password: str, zones: list[dict[str, float]], previous_tokens: list[str]) -> list[str]:
    """Replace the NVR's masks on the camera with ``zones``. Returns the new mask tokens."""
    async with _client(username, password) as client:
        media2 = await _media2(client, device_service_url)
        configurations = await _call(client, media2, f"{MEDIA2}/GetVideoSourceConfigurations", "<tr2:GetVideoSourceConfigurations/>")
        # Every video source: a stream from a source left unmasked would carry the private area.
        sources = list(dict.fromkeys(node.get("token") for node in configurations.iter() if _local(node.tag) == "Configurations" and node.get("token")))
        if not sources:
            raise MasksUnsupported()
        for source in sources:
            try:
                await _call(client, media2, f"{MEDIA2}/GetMaskOptions", f"<tr2:GetMaskOptions><tr2:ConfigurationToken>{_escape(source)}</tr2:ConfigurationToken></tr2:GetMaskOptions>")
            except httpx.HTTPStatusError as exc:
                raise MasksUnsupported() from exc
        for token in previous_tokens:
            try:
                await _call(client, media2, f"{MEDIA2}/DeleteMask", f"<tr2:DeleteMask><tr2:Token>{_escape(token)}</tr2:Token></tr2:DeleteMask>")
            except httpx.HTTPStatusError:
                pass  # Already gone on the camera.
        tokens: list[str] = []
        try:
            for source in sources:
                await _create(client, media2, source, zones, tokens)
            return tokens
        except MasksUnsupported:
            raise
        except Exception as exc:
            raise MaskWriteFailed(tokens) from exc


async def _create(client: httpx.AsyncClient, media2: str, source: str, zones: list[dict[str, float]], tokens: list[str]) -> None:
    """Create each mask on one video source, adding its token to ``tokens`` as soon as the camera has it."""
    for zone in zones:
        points = "".join(f'<tt:Point x="{x:.4f}" y="{y:.4f}"/>' for x, y in polygon(zone))
        reply = await _call(client, media2, f"{MEDIA2}/CreateMask",
                            f"<tr2:CreateMask><tr2:Mask><tt:ConfigurationToken>{_escape(source)}</tt:ConfigurationToken>"
                            f"<tt:Polygon>{points}</tt:Polygon><tt:Type>Color</tt:Type>"
                            '<tt:Color X="0" Y="0" Z="0" Colorspace="http://www.onvif.org/ver10/colorspace/YCbCr"/>'
                            "<tt:Enabled>true</tt:Enabled></tr2:Mask></tr2:CreateMask>")
        token = next((node.text.strip() for node in reply.iter() if _local(node.tag) == "Token" and node.text), None)
        if not token:
            raise ValueError("The camera did not return a mask token")
        tokens.append(token)
