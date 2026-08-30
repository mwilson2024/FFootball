import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MFLSnapshot

logger = logging.getLogger(__name__)


class MFLError(RuntimeError):
    pass


class MFLAuthenticationError(MFLError):
    pass


@dataclass(frozen=True)
class MFLResponse:
    export_type: str
    payload: dict[str, Any]
    source_url: str
    fetched_at: datetime
    stale: bool = False


def _xml_to_dict(node: ET.Element) -> dict[str, Any]:
    result: dict[str, Any] = dict(node.attrib)
    children = list(node)
    if not children:
        if node.text and node.text.strip():
            result["value"] = node.text.strip()
        return result
    for child in children:
        value = _xml_to_dict(child)
        existing = result.get(child.tag)
        if existing is None:
            result[child.tag] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[child.tag] = [existing, value]
    return result


class MFLClient:
    # These league-independent exports are rejected by an individual league host.
    # MFL requires them to use the central API host even after a league baseURL is known.
    CENTRAL_API_EXPORTS = {"allRules", "playerRanks", "adp", "aav", "myleagues"}

    CACHE_TTLS: dict[str, timedelta] = {
        "players": timedelta(hours=24),
        "allRules": timedelta(days=7),
        "league": timedelta(minutes=15),
        "rules": timedelta(hours=6),
        "playerRanks": timedelta(hours=6),
        "adp": timedelta(hours=6),
        "aav": timedelta(hours=6),
        "projectedScores": timedelta(hours=2),
        "rosters": timedelta(minutes=2),
        "selectedKeepers": timedelta(minutes=2),
        "auctionResults": timedelta(seconds=30),
        "draftResults": timedelta(minutes=15),
        "transactions": timedelta(minutes=2),
    }

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._league_hosts: dict[str, str] = {}
        self._cookie: str | None = None
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.mfl_user_agent, "Accept": "application/json, text/xml"},
            timeout=httpx.Timeout(connect=10, read=30, write=15, pool=10),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> "MFLClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _base(self, league_id: str | None = None) -> str:
        host = self._league_hosts.get(league_id or "", "api.myfantasyleague.com")
        return f"https://{host}/{self.settings.mfl_season}"

    async def authenticate(self, username: str, password: str) -> None:
        if not username or not password:
            raise MFLAuthenticationError("MFL credentials are required")
        response = await self._request(
            "POST",
            f"https://api.myfantasyleague.com/{self.settings.mfl_season}/login",
            data={
                "USERNAME": username,
                "PASSWORD": password,
                "XML": "1",
            },
        )
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise MFLAuthenticationError("MFL returned an invalid login response") from exc
        if root.tag == "error" or root.find(".//error") is not None:
            raise MFLAuthenticationError("MFL rejected the supplied commissioner credentials")
        status = root if root.tag == "status" else root.find(".//status")
        cookie = response.cookies.get("MFL_USER_ID")
        if status is not None:
            cookie = cookie or status.attrib.get("MFL_USER_ID")
            if not cookie and status.attrib.get("cookie_name") == "MFL_USER_ID":
                cookie = status.attrib.get("cookie_value")
        if not cookie:
            raise MFLAuthenticationError("MFL login response did not contain MFL_USER_ID")
        self._cookie = cookie

    async def login(self) -> None:
        if not self.settings.mfl_username or not self.settings.mfl_password:
            raise MFLAuthenticationError("Commissioner credentials are not configured")
        await self.authenticate(self.settings.mfl_username, self.settings.mfl_password)

    async def export(
        self,
        export_type: str,
        *,
        league_id: str | None = None,
        params: dict[str, str] | None = None,
        db: Session | None = None,
        force: bool = False,
    ) -> MFLResponse:
        request_params = {"TYPE": export_type, "JSON": "1", **(params or {})}
        if league_id:
            request_params["L"] = str(league_id)
            api_key = self.settings.api_key_for(str(league_id))
            if api_key:
                request_params["APIKEY"] = api_key
        now = datetime.now(UTC)
        if db is not None and not force:
            cached = db.scalar(
                select(MFLSnapshot)
                .where(
                    MFLSnapshot.league_id == league_id,
                    MFLSnapshot.export_type == export_type,
                    MFLSnapshot.expires_at > now,
                )
                .order_by(MFLSnapshot.fetched_at.desc())
            )
            if cached and isinstance(cached.payload_json, dict):
                return MFLResponse(
                    export_type, cached.payload_json, cached.source_url, cached.fetched_at
                )
        base = (
            f"https://api.myfantasyleague.com/{self.settings.mfl_season}"
            if export_type in self.CENTRAL_API_EXPORTS
            else self._base(league_id)
        )
        url = f"{base}/export"
        try:
            headers = {"Cookie": f"MFL_USER_ID={self._cookie}"} if self._cookie else None
            response = await self._request("GET", url, params=request_params, headers=headers)
            payload = self._parse_response(response)
        except MFLError:
            if db is None:
                raise
            cached = db.scalar(
                select(MFLSnapshot)
                .where(MFLSnapshot.league_id == league_id, MFLSnapshot.export_type == export_type)
                .order_by(MFLSnapshot.fetched_at.desc())
            )
            if cached and isinstance(cached.payload_json, dict):
                return MFLResponse(
                    export_type,
                    cached.payload_json,
                    cached.source_url,
                    cached.fetched_at,
                    stale=True,
                )
            raise
        if league_id:
            host = urlparse(str(response.url)).hostname
            league_payload = payload.get("league")
            if isinstance(league_payload, dict) and league_payload.get("baseURL"):
                host = urlparse(str(league_payload["baseURL"])).hostname or host
            if host:
                self._league_hosts[str(league_id)] = host
        result = MFLResponse(export_type, payload, str(response.url), now)
        if db is not None:
            ttl = self.CACHE_TTLS.get(export_type, timedelta(minutes=5))
            db.add(
                MFLSnapshot(
                    league_id=league_id,
                    season=self.settings.mfl_season,
                    export_type=export_type,
                    source_url=str(response.url).split("APIKEY=")[0],
                    parameters_json={k: v for k, v in request_params.items() if k != "APIKEY"},
                    payload_json=payload,
                    fetched_at=now,
                    expires_at=now + ttl,
                )
            )
            db.commit()
        return result

    async def import_auction_results(
        self,
        league_id: str,
        payload_xml: str,
        *,
        clear: bool = False,
        overwrite: bool = False,
    ) -> str:
        if not self.settings.mfl_enable_imports:
            raise MFLAuthenticationError("MFL imports are disabled")
        if self._cookie is None:
            await self.login()
        data = {"TYPE": "auctionResults", "L": str(league_id), "DATA": payload_xml}
        if clear:
            data["CLEAR"] = "1"
        if overwrite:
            data["OVERWRITE"] = "1"
        response = await self._request(
            "POST",
            f"{self._base(league_id)}/import",
            data=data,
            headers={"Cookie": f"MFL_USER_ID={self._cookie or ''}"},
        )
        text = response.text.lstrip()
        if "json" in response.headers.get("content-type", "") or text.startswith(("{", "<")):
            self._parse_response(response)
        return response.text

    @staticmethod
    def _response_error(response: httpx.Response) -> str:
        prefix = f"MFL returned HTTP {response.status_code}"
        text = response.text.strip()
        if not text:
            return prefix
        try:
            if "json" in response.headers.get("content-type", "") or text.startswith("{"):
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    detail = error.get("$t") or error.get("message") or error.get("value")
                else:
                    detail = error
                if detail:
                    return f"{prefix}: {str(detail)[:500]}"
            if text.startswith("<"):
                root = ET.fromstring(response.content)
                error_node = root if root.tag == "error" else root.find(".//error")
                if error_node is not None:
                    detail = (error_node.text or "").strip() or error_node.attrib.get("message")
                    if detail:
                        return f"{prefix}: {detail[:500]}"
        except (ValueError, ET.ParseError):
            pass
        if not text.startswith("<"):
            return f"{prefix}: {text[:500]}"
        return prefix

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code in {401, 403}:
                    raise MFLAuthenticationError("MFL authorization failed")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 3:
                        raise MFLError(self._response_error(response))
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else (2**attempt + random.random())
                    await self._sleep(min(delay, 15))
                    continue
                if response.status_code >= 400:
                    raise MFLError(self._response_error(response))
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt == 3:
                    break
                await self._sleep(min(2**attempt + random.random(), 15))
        logger.error("MFL request failed: method=%s endpoint=%s", method, url.split("?")[0])
        raise MFLError("MFL request failed after retries") from last_error

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        text = response.text.lstrip()
        try:
            if "json" in response.headers.get("content-type", "") or text.startswith("{"):
                payload = response.json()
                if not isinstance(payload, dict):
                    raise MFLError("MFL response root must be an object")
                if "error" in payload:
                    raise MFLError(str(payload["error"]))
                return payload
            root = ET.fromstring(response.content)
            if root.tag == "error":
                raise MFLError(root.text or "MFL returned an error")
            return {root.tag: _xml_to_dict(root)}
        except (ValueError, ET.ParseError) as exc:
            raise MFLError("MFL returned an unreadable response") from exc
