"""Shared WebDAV transport behavior, against a mocked HTTP layer.

No socket is opened. What is pinned here is how the client reacts to the
responses iCloud actually returns -- particularly the throttling that shows up
after a burst of writes and looks like nothing else.

This covers ``dav.DavClient``, which both the CalDAV and CardDAV clients extend,
so a regression here would break contacts and calendar alike.
"""

import httpx
import pytest

from dav_mcp.dav import AuthError, DavClient, DavError, NotFound, Throttled


def client(handler):
    """A client whose transport is a scripted handler."""
    c = DavClient(
        username="me@example.com", password="app-specific", root="https://x"
    )
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "test"}
    )
    return c


class TestThrottling:
    async def test_a_503_is_retried_and_can_succeed(self, monkeypatch):
        monkeypatch.setattr("dav_mcp.dav.RETRY_BACKOFF", (0, 0, 0))
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
        monkeypatch.setattr("dav_mcp.dav.RETRY_BACKOFF", (0, 0, 0))
        with pytest.raises(Throttled) as excinfo:
            await client(lambda request: httpx.Response(503))._request(
                "PROPFIND", "https://x/", expect=(207,)
            )
        message = str(excinfo.value)
        assert "rate limiting" in message
        assert "not a credential problem" in message

    async def test_a_429_is_treated_the_same_way(self, monkeypatch):
        monkeypatch.setattr("dav_mcp.dav.RETRY_BACKOFF", (0,))
        with pytest.raises(Throttled):
            await client(lambda request: httpx.Response(429))._request(
                "PROPFIND", "https://x/", expect=(207,)
            )

    async def test_retry_after_is_honored_but_capped(self, monkeypatch):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("dav_mcp.dav.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("dav_mcp.dav.RETRY_BACKOFF", (1.0,))
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

        monkeypatch.setattr("dav_mcp.dav.asyncio.sleep", fake_sleep)
        with pytest.raises(Throttled):
            await client(
                lambda request: httpx.Response(503, headers={"Retry-After": "30"})
            )._request("PROPFIND", "https://x/", expect=(207,))
        assert sum(slept) <= 10.0

    async def test_an_ordinary_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr("dav_mcp.dav.RETRY_BACKOFF", (0, 0, 0))
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500, text="boom")

        with pytest.raises(DavError):
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


class TestDiscoveryStaysInTheAccount:
    """Discovery follows hrefs out of the response and then sends the account's
    credentials to whatever they name. httpx strips ``Authorization`` across a
    redirect, but an href is an ordinary new request it never sees.
    """

    def dav(self, root="https://caldav.icloud.com"):
        return DavClient(username="me@example.com", password="pw", root=root)

    @pytest.mark.parametrize(
        "href",
        [
            "/11026982/principal/",
            "https://caldav.icloud.com/11026982/principal/",
            # iCloud really does move the account to a numbered host between the
            # principal and the home-set; rejecting this would break every
            # account.
            "https://p64-caldav.icloud.com/11026982/calendars/",
        ],
    )
    def test_allows_the_account_s_own_hosts(self, href):
        resolved = self.dav()._resolve("https://caldav.icloud.com/", href)
        assert resolved.endswith(("principal/", "calendars/"))

    @pytest.mark.parametrize(
        "href",
        [
            "https://evil.example.com/steal/",
            "https://icloud.com.evil.example.net/steal/",
            "http://caldav.icloud.com/downgraded/",
        ],
    )
    def test_refuses_to_follow_an_href_off_the_account(self, href):
        with pytest.raises(DavError) as excinfo:
            self.dav()._resolve("https://caldav.icloud.com/", href)
        assert "credentials" in str(excinfo.value)

    def test_a_two_label_root_does_not_admit_its_whole_tld(self):
        # "example.com" must not widen to everything under "com".
        with pytest.raises(DavError):
            self.dav(root="https://example.com")._resolve(
                "https://example.com/", "https://evil.com/x"
            )
