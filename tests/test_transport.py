"""Transport selection and the health endpoint.

Nothing here binds a socket. The server object is exercised directly and
``mcp.run`` is patched, which is enough to pin the decisions that only ever go
wrong on the deployed host: which transport, which port, whether auth is
attached, and whether sessions are on.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from dav_mcp import server


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "DAV_MCP_TRANSPORT",
        "DAV_MCP_HOST",
        "DAV_MCP_PORT",
        "DAV_MCP_STATELESS",
        "DAV_MCP_AUTH",
        "DAV_MCP_PASSWORD",
        "DAV_MCP_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class TestTransportSelection:
    def test_stdio_is_the_default_and_takes_no_auth(self):
        with patch.object(server.mcp, "run") as run:
            server.main()
        run.assert_called_once_with()

    def test_http_binds_localhost_and_the_project_port_by_default(self, monkeypatch):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        with patch.object(server.mcp, "run") as run:
            server.main()
        kwargs = run.call_args.kwargs
        assert kwargs["transport"] == "http"
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 18790

    def test_host_and_port_are_configurable(self, monkeypatch):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("DAV_MCP_PORT", "9999")
        with patch.object(server.mcp, "run") as run:
            server.main()
        assert run.call_args.kwargs["host"] == "0.0.0.0"
        assert run.call_args.kwargs["port"] == 9999

    def test_the_environment_is_read_at_run_time_not_import_time(self, monkeypatch):
        # launchd sets the environment for a process that has already imported
        # the module, and the tests rely on the same property.
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_PORT", "12345")
        with patch.object(server.mcp, "run") as run:
            server.main()
        assert run.call_args.kwargs["port"] == 12345

    def test_an_unrecognized_transport_falls_back_to_stdio(self, monkeypatch):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "carrier-pigeon")
        with patch.object(server.mcp, "run") as run:
            server.main()
        run.assert_called_once_with()


class TestStatelessHttp:
    """Sessions are the wrong model for a client dialed from a pool of
    addresses: a request landing from a different address than the one that
    opened the session is rejected, and the connection wedges."""

    def test_stateless_is_on_by_default(self, monkeypatch):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        with patch.object(server.mcp, "run") as run:
            server.main()
        assert run.call_args.kwargs["stateless_http"] is True

    @pytest.mark.parametrize("value", ["false", "FALSE", " False "])
    def test_only_an_explicit_false_restores_sessions(self, monkeypatch, value):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_STATELESS", value)
        with patch.object(server.mcp, "run") as run:
            server.main()
        assert run.call_args.kwargs["stateless_http"] is False

    @pytest.mark.parametrize("value", ["true", "yes", "", "0", "no"])
    def test_anything_else_stays_stateless(self, monkeypatch, value):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_STATELESS", value)
        with patch.object(server.mcp, "run") as run:
            server.main()
        assert run.call_args.kwargs["stateless_http"] is True


class TestAuthAttachment:
    def test_password_auth_is_attached_for_http(self, monkeypatch):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_AUTH", "password")
        monkeypatch.setenv("DAV_MCP_PASSWORD", "a-long-random-value")
        monkeypatch.setenv("DAV_MCP_BASE_URL", "https://calendar.example.com")
        with patch.object(server.mcp, "run"):
            server.main()
        assert server.mcp.auth is not None

    def test_a_misconfigured_password_mode_fails_loudly_at_startup(self, monkeypatch):
        # Better to refuse to boot than to serve the calendar unauthenticated.
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_AUTH", "password")
        with patch.object(server.mcp, "run"), pytest.raises(ValueError) as excinfo:
            server.main()
        assert "DAV_MCP_PASSWORD" in str(excinfo.value)


async def health_json():
    # The probe is cached to keep an outsider from using /health to hammer
    # iCloud; each test wants a fresh look at its own mock.
    server._health_cache = None
    response = await server.health(None)
    return json.loads(response.body)


class TestHealth:
    async def test_reports_ok_when_the_account_is_reachable(self):
        with patch.object(server, "client") as factory:
            factory.return_value.calendars = AsyncMock(return_value=[object(), object()])
            body = await health_json()
        assert body["status"] == "ok"
        assert body["caldav_reachable"] is True
        assert body["event_calendars"] == 2

    async def test_reports_degraded_when_caldav_cannot_be_reached(self):
        with patch.object(server, "client") as factory:
            factory.return_value.calendars = AsyncMock(side_effect=OSError("boom"))
            body = await health_json()
        assert body["status"] == "degraded"
        assert body["caldav_reachable"] is False
        assert body["error"] == "OSError"

    async def test_an_account_with_no_calendars_is_degraded_not_ok(self):
        with patch.object(server, "client") as factory:
            factory.return_value.calendars = AsyncMock(return_value=[])
            body = await health_json()
        assert body["status"] == "degraded"

    async def test_publishes_the_python_version_for_the_privacy_grant_warning(self):
        # A macOS privacy grant is bound to the interpreter's versioned path; an
        # upgrade silently invalidates it and the service hangs on next restart.
        with patch.object(server, "client") as factory:
            factory.return_value.calendars = AsyncMock(return_value=[object()])
            body = await health_json()
        assert body["python_version"].count(".") == 2

    async def test_leaks_no_account_detail(self):
        # The endpoint is unauthenticated and the hostname shows up in
        # certificate transparency logs within hours of going live.
        with patch.object(server, "client") as factory:
            factory.return_value.calendars = AsyncMock(
                side_effect=RuntimeError("password rejected for someone@example.com")
            )
            body = await health_json()
        assert "someone@example.com" not in json.dumps(body)

    async def test_surfaces_the_last_failed_write(self):
        server._record_write("create_event", False, RuntimeError("409 from iCloud"))
        try:
            with patch.object(server, "client") as factory:
                factory.return_value.calendars = AsyncMock(return_value=[object()])
                body = await health_json()
            assert body["last_write"]["ok"] is False
            assert body["last_write"]["action"] == "create_event"
            assert body["last_write"]["at"] is not None
            # Only the class, never the message: a DavError's text carries the
            # request URL (principal, calendar, resource) and up to 400
            # characters of the server's response body, and healthcheck.sh
            # forwards this field off the host to a ping service.
            assert body["last_write"]["error"] == "RuntimeError"
            assert "409 from iCloud" not in json.dumps(body)
        finally:
            server._last_write.update(
                {"at": None, "ok": None, "error": None, "action": None}
            )


class TestHealthIsCheap:
    """/health is unauthenticated and the hostname is public within hours.

    Every miss costs the full discovery walk against iCloud, and iCloud answers
    a burst by throttling the whole Apple ID -- reads included, for tens of
    minutes. Uncached, anything on the internet could use this endpoint to take
    every calendar tool offline.
    """

    async def test_the_probe_is_cached_between_requests(self):
        server._health_cache = None
        with patch.object(server, "client") as factory:
            calendars = AsyncMock(return_value=[object()])
            factory.return_value.calendars = calendars
            for _ in range(5):
                response = await server.health(None)
                assert json.loads(response.body)["status"] == "ok"
        assert calendars.await_count == 1

    async def test_a_stale_entry_is_refreshed(self):
        server._health_cache = None
        with patch.object(server, "client") as factory:
            calendars = AsyncMock(return_value=[object()])
            factory.return_value.calendars = calendars
            await server.health(None)
            stamp, cached = server._health_cache
            server._health_cache = (stamp - server.HEALTH_TTL_SECONDS - 1, cached)
            await server.health(None)
        assert calendars.await_count == 2


class TestUnauthenticatedBind:
    async def test_warns_when_a_public_bind_has_no_auth(self, monkeypatch, caplog):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_HOST", "0.0.0.0")
        with patch.object(server.mcp, "run"), caplog.at_level("WARNING"):
            server.main()
        assert "DAV_MCP_AUTH" in caplog.text

    async def test_stays_quiet_on_loopback(self, monkeypatch, caplog):
        monkeypatch.setenv("DAV_MCP_TRANSPORT", "http")
        monkeypatch.setenv("DAV_MCP_HOST", "127.0.0.1")
        with patch.object(server.mcp, "run"), caplog.at_level("WARNING"):
            server.main()
        assert "DAV_MCP_AUTH" not in caplog.text

    @pytest.mark.parametrize(
        "host, loopback",
        [
            ("127.0.0.1", True),
            ("localhost", True),
            ("::1", True),
            ("0.0.0.0", False),
            ("192.168.1.10", False),
            ("calendar.example.com", False),
        ],
    )
    def test_recognizes_loopback(self, host, loopback):
        assert server._is_loopback(host) is loopback


class TestScope:
    """The OAuth scope is embedded in every issued token and registration.

    Changing it invalidates both and forces every client to authorize again, so
    it is pinned here to make that a deliberate act rather than a side effect.
    """

    def test_the_scope_names_the_server_not_one_of_its_protocols(self):
        from dav_mcp.auth import SCOPE

        assert SCOPE == "dav:manage"

    def test_discovery_advertises_exactly_that_scope(self, monkeypatch):
        from dav_mcp.auth import SCOPE, PasswordOAuthProvider

        provider = PasswordOAuthProvider(
            password="x", base_url="https://dav.example.com", state_path=None
        )
        assert provider.required_scopes == [SCOPE]


class TestClientRegistryIsBounded:
    """``/register`` is unauthenticated by necessity -- a client has to register
    before anyone can log in -- which makes it the one endpoint a stranger can
    write to, and every call rewrites the whole state file.
    """

    def provider(self):
        from dav_mcp.auth import PasswordOAuthProvider

        return PasswordOAuthProvider(
            password="x", base_url="https://dav.example.com", state_path=None
        )

    def registration(self, client_id):
        from mcp.shared.auth import OAuthClientInformationFull

        return OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=["https://client.example.com/callback"],
        )

    async def test_a_flood_of_registrations_does_not_grow_without_bound(self):
        from dav_mcp.auth import MAX_CLIENTS

        provider = self.provider()
        for n in range(MAX_CLIENTS + 50):
            await provider.register_client(self.registration(f"client-{n}"))
        assert len(provider.clients) == MAX_CLIENTS

    async def test_a_client_holding_a_token_outlives_an_idle_one(self):
        from dav_mcp.auth import MAX_CLIENTS

        provider = self.provider()
        await provider.register_client(self.registration("keeper"))
        provider._issue_tokens("keeper", ["dav:manage"])

        for n in range(MAX_CLIENTS + 10):
            await provider.register_client(self.registration(f"drifter-{n}"))

        # "keeper" registered first, so pure oldest-first would have dropped it;
        # losing a registration costs a round trip, losing a token costs a login.
        assert "keeper" in provider.clients
        assert len(provider.clients) == MAX_CLIENTS
