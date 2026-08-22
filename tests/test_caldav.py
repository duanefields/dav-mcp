"""Calendar selection, against a stubbed discovery step.

``default_calendar`` is the path every unqualified ``create_event`` takes, and
``test_server.py`` mocks it wholesale -- so nothing exercised the real one until
this file. That is how it came to reference ``os`` while the import sat inside
``client_from_env``, one function away, raising NameError on every call.
"""

from unittest.mock import AsyncMock, patch

import pytest

from dav_mcp import caldav
from dav_mcp.caldav import CalDavClient, CalDavError, NotFound


def calendar(id, name, read_only=False):
    return caldav.Calendar(
        id=id,
        name=name,
        color="",
        url=f"https://p64-caldav.icloud.com/1/calendars/{id}/",
        components=("VEVENT",),
        read_only=read_only,
    )


def client_with(*calendars):
    client = CalDavClient(username="me@example.com", password="pw")
    patcher = patch.object(
        CalDavClient, "calendars", AsyncMock(return_value=list(calendars))
    )
    patcher.start()
    return client, patcher


@pytest.fixture
def account():
    client, patcher = client_with(
        calendar("c1", "Home"), calendar("c2", "Work"), calendar("c3", "Holidays", True)
    )
    yield client
    patcher.stop()


class TestDefaultCalendar:
    async def test_falls_back_to_the_first_writable_calendar(self, account, monkeypatch):
        monkeypatch.delenv("DAV_MCP_DEFAULT_CALENDAR", raising=False)
        assert (await account.default_calendar()).id == "c1"

    @pytest.mark.parametrize("configured", ["c2", "Work", "work"])
    async def test_the_environment_names_it_by_id_or_display_name(
        self, account, monkeypatch, configured
    ):
        monkeypatch.setenv("DAV_MCP_DEFAULT_CALENDAR", configured)
        assert (await account.default_calendar()).id == "c2"

    async def test_a_name_that_matches_nothing_lists_what_is_available(
        self, account, monkeypatch
    ):
        # Silently falling back would write the event to the wrong calendar and
        # look like it worked.
        monkeypatch.setenv("DAV_MCP_DEFAULT_CALENDAR", "Personal")
        with pytest.raises(NotFound) as excinfo:
            await account.default_calendar()
        assert "'Home'" in str(excinfo.value)

    async def test_a_read_only_calendar_is_never_the_default(self, monkeypatch):
        monkeypatch.delenv("DAV_MCP_DEFAULT_CALENDAR", raising=False)
        client, patcher = client_with(calendar("c3", "Holidays", True), calendar("c1", "Home"))
        try:
            assert (await client.default_calendar()).id == "c1"
        finally:
            patcher.stop()

    async def test_an_account_with_nothing_writable_says_so(self, monkeypatch):
        monkeypatch.delenv("DAV_MCP_DEFAULT_CALENDAR", raising=False)
        client, patcher = client_with(calendar("c3", "Holidays", True))
        try:
            with pytest.raises(CalDavError):
                await client.default_calendar()
        finally:
            patcher.stop()
