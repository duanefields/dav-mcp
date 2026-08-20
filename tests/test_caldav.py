"""CalDAV transport behavior, against a mocked HTTP layer.

No socket is opened. What is pinned here is how the client reacts to the
responses iCloud actually returns -- particularly the throttling that shows up
after a burst of writes and looks like nothing else.
"""

import httpx
import pytest

from calendar_mcp.caldav import (
    AuthError,
    CalDavClient,
    CalDavError,
    NotFound,
    Throttled,
)


def client(handler):
    """A client whose transport is a scripted handler."""
    c = CalDavClient(username="me@example.com", password="app-specific")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "test"}
    )
    return c


class TestThrottling:
    async def test_a_503_is_retried_and_can_succeed(self, monkeypatch):
        monkeypatch.setattr("calendar_mcp.caldav._RETRY_BACKOFF", (0, 0, 0))
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503)
            return httpx.Response(207, text="<ok/>")

        response = await client(handler)._request("PROPFIND", "https://x/", expect=(207,))
        assert response.status_code == 207
        assert len(calls) == 3

    async def test_persistent_throttling_says_it_is_not_a_credential_problem(
        self, monkeypatch
    ):
        # The failure this message exists for: a burst of writes gets the
        # account throttled, and "503" alone reads like the server is broken.
        monkeypatch.setattr("calendar_mcp.caldav._RETRY_BACKOFF", (0, 0, 0))
        with pytest.raises(Throttled) as excinfo:
            await client(lambda request: httpx.Response(503))._request(
                "PROPFIND", "https://x/", expect=(207,)
            )
        message = str(excinfo.value)
        assert "rate limiting" in message
        assert "not a credential problem" in message

    async def test_a_429_is_treated_the_same_way(self, monkeypatch):
        monkeypatch.setattr("calendar_mcp.caldav._RETRY_BACKOFF", (0,))
        with pytest.raises(Throttled):
            await client(lambda request: httpx.Response(429))._request(
                "PROPFIND", "https://x/", expect=(207,)
            )

    async def test_retry_after_is_honored_but_capped(self, monkeypatch):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("calendar_mcp.caldav.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("calendar_mcp.caldav._RETRY_BACKOFF", (1.0,))
        with pytest.raises(Throttled):
            await client(
                lambda request: httpx.Response(503, headers={"Retry-After": "9999"})
            )._request("PROPFIND", "https://x/", expect=(207,))
        # Capped. iCloud really does send Retry-After: 30 while throttling, and
        # obeying it literally made a tool call hang for a minute and a half.
        assert slept == [5.0]

    async def test_the_whole_retry_budget_stays_short(self, monkeypatch):
        # A tool call that blocks for a minute is worse than one that fails in
        # two seconds saying to try again.
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("calendar_mcp.caldav.asyncio.sleep", fake_sleep)
        with pytest.raises(Throttled):
            await client(
                lambda request: httpx.Response(503, headers={"Retry-After": "30"})
            )._request("PROPFIND", "https://x/", expect=(207,))
        assert sum(slept) <= 10.0

    async def test_an_ordinary_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr("calendar_mcp.caldav._RETRY_BACKOFF", (0, 0, 0))
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500, text="boom")

        with pytest.raises(CalDavError):
            await client(handler)._request("PROPFIND", "https://x/", expect=(207,))
        assert len(calls) == 1


class TestErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_rejected_credential_names_the_app_specific_password(self, status):
        with pytest.raises(AuthError) as excinfo:
            await client(lambda request: httpx.Response(status))._request(
                "PROPFIND", "https://x/", expect=(207,)
            )
        assert "app-specific" in str(excinfo.value)

    async def test_a_missing_resource_is_not_found(self):
        with pytest.raises(NotFound):
            await client(lambda request: httpx.Response(404))._request(
                "GET", "https://x/e.ics", expect=(200,)
            )
