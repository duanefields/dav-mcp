"""Suite-wide fixtures.

The one thing here is the timezone. Several suites build fixture events with an
explicit ``TZID=America/Chicago`` and then assert on times that the server
computes in ``dates.local_zone()`` -- the working-day window in
``find_free_time`` most of all. Left to itself ``local_zone()`` reads the host,
so those tests passed on a laptop in Chicago and failed on a CI runner in UTC,
where a noon meeting lands at 17:00 and falls outside the working day entirely.

Pinning the zone here rather than in the workflow keeps the suite honest
wherever it runs, instead of making CI the only place it is reproducible.
"""

import pytest


@pytest.fixture(autouse=True)
def pinned_timezone(monkeypatch):
    monkeypatch.setenv("DAV_MCP_TIMEZONE", "America/Chicago")
