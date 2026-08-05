import json

import httpx
import pytest

from app.config import Settings
from app.mfl import MFLAuthenticationError, MFLClient


@pytest.mark.asyncio
async def test_export_keeps_string_ids_and_api_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"players": {"player": [{"id": "0001234"}]}}).encode(),
            request=request,
        )

    settings = Settings(mfl_keeper_league_id="00123", mfl_keeper_api_key="secret")
    async with MFLClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = await client.export("players", league_id="00123")
    assert result.payload["players"]["player"][0]["id"] == "0001234"
    assert "APIKEY=secret" in seen["url"]


@pytest.mark.asyncio
async def test_league_base_url_drives_following_league_requests():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.params.get("TYPE") == "league":
            return httpx.Response(
                200,
                json={"league": {"id": "0001", "baseURL": "https://www46.myfantasyleague.com"}},
                request=request,
            )
        return httpx.Response(200, json={"rosters": {"franchise": []}}, request=request)

    async with MFLClient(Settings(), transport=httpx.MockTransport(handler)) as client:
        await client.export("league", league_id="0001")
        await client.export("rosters", league_id="0001")
    assert seen == ["api.myfantasyleague.com", "www46.myfantasyleague.com"]


@pytest.mark.asyncio
async def test_central_exports_stay_on_api_host_after_league_host_is_known():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        export_type = request.url.params.get("TYPE", "")
        seen.append((export_type, request.url.host))
        if export_type == "league":
            payload = {"league": {"id": "0001", "baseURL": "https://www46.myfantasyleague.com"}}
        else:
            payload = {export_type: {}}
        return httpx.Response(200, json=payload, request=request)

    async with MFLClient(Settings(), transport=httpx.MockTransport(handler)) as client:
        await client.export("league", league_id="0001")
        await client.export("rosters", league_id="0001")
        for export_type in ("allRules", "playerRanks", "adp", "aav"):
            await client.export(export_type, league_id="0001")

    assert seen == [
        ("league", "api.myfantasyleague.com"),
        ("rosters", "www46.myfantasyleague.com"),
        ("allRules", "api.myfantasyleague.com"),
        ("playerRanks", "api.myfantasyleague.com"),
        ("adp", "api.myfantasyleague.com"),
        ("aav", "api.myfantasyleague.com"),
    ]


@pytest.mark.asyncio
async def test_429_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"league": {"id": "0001"}}, request=request)

    async def no_sleep(_: float) -> None:
        return None

    async with MFLClient(
        Settings(), transport=httpx.MockTransport(handler), sleep=no_sleep
    ) as client:
        await client.export("league", league_id="0001")
    assert calls == 2


@pytest.mark.asyncio
async def test_login_cookie_and_auth_failure():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<status MFL_USER_ID="abc+/="/>', request=request)

    settings = Settings(mfl_username="user", mfl_password="pass")
    async with MFLClient(settings, transport=httpx.MockTransport(ok)) as client:
        await client.login()

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    async with MFLClient(settings, transport=httpx.MockTransport(denied)) as client:
        with pytest.raises(MFLAuthenticationError):
            await client.export("rosters", league_id="0001")
