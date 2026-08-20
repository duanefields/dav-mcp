# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code
in this repository.

## Commands

### Development Setup

```bash
# Install dependencies (uses uv package manager)
uv sync

# Run the MCP server (stdio transport, default)
uv run dav-mcp

# Run with HTTP transport
DAV_MCP_TRANSPORT=http uv run dav-mcp
```

### Testing

```bash
uv sync --extra test
uv run pytest
uv run pytest -v
uv run pytest tests/test_ical.py
uv run pytest -k "recurrence"
```

The suite is entirely offline. Nothing in `tests/` opens a socket, and no test
touches a real iCloud account.

## Architecture Overview

An MCP server that bridges a model to Apple iCloud Calendar over CalDAV. Layered
so that each file has exactly one thing that can go wrong in it:

0. **src/dav_mcp/dav.py** — HTTP plumbing shared by both protocols: auth,
   retries, throttling, error mapping, the principal→home discovery walk, and
   resource GET/PUT/DELETE. Both clients extend `DavClient`, so a fix here
   applies to calendar and contacts alike.
1. **src/dav_mcp/caldav.py** — the protocol. Discovery (principal →
   calendar-home-set → calendars, cached 300s), `calendar-query` REPORTs,
   `GET`/`PUT`/`DELETE` of `.ics` resources. Everything Apple-specific lives
   here. Returns `Resource` objects holding raw iCalendar text; it does not
   parse events.
2. **src/dav_mcp/ical.py** — translation between VEVENTs and the dicts the
   tools return, in both directions. Uses the `icalendar` library so escaping,
   line folding and VTIMEZONE generation are not hand-rolled.
3. **src/dav_mcp/ids.py** — opaque, stateless event ids encoding
   `(calendar, resource, recurrence key)`. There is no database.
4. **src/dav_mcp/dates.py** — `after`/`before` parsing (ISO plus relative
   expressions) and ISO 8601 duration conversion.
5. **src/dav_mcp/server.py** — the tools, the `/health` route, and
   transport selection in `main()`.
6. **src/dav_mcp/carddav.py** — CardDAV protocol: address book discovery,
   `addressbook-query` search, resource writes.
7. **src/dav_mcp/vcard.py** — vCard ↔ dict, via `vobject`. Reading is
   lossy by design (photos and internal properties are not reported); **writing
   is not** — see below.
8. **src/dav_mcp/availability.py** — interval arithmetic for
   `find_free_time`: merge, invert, per-day working windows, slot search. Pure
   functions over aware datetimes, so it is tested without a network or a
   clock. Which events *count* as busy is decided in `server._busy_intervals`,
   not here — that is policy, not arithmetic.
7. **src/dav_mcp/auth.py** — password-guarded OAuth 2.1 provider for the
   HTTP transport. Ported from things-mcp; domain-independent apart from the
   scope name and the env prefix.

## Key Implementation Details

- FastMCP 3.x provides the protocol. Tools are bare `@mcp.tool` on `async def`;
  the schema comes from the signature and the docstring, so the docstrings are
  written **at the model**, not at a developer.
- Tools return a `ToolResult` carrying both a human-readable text channel and
  `structured_content`. Errors return `_error_result`, never raise — a raised
  exception reaches the model as an opaque failure it cannot act on.
- Transport env vars are read inside `main()`, not at import time, so launchd
  and the tests can set them after import.
- `stateless_http` defaults to `True`. A remote client dials from a pool of
  addresses; a request arriving from a different address than the one that
  opened the session is rejected with a 400 and the connection wedges.
- Writes use `If-None-Match: *` on create and `If-Match: <etag>` on update, so a
  UID collision or a concurrent edit fails loudly instead of silently
  overwriting.
- Editing one occurrence of a series adds a second VEVENT with a
  `RECURRENCE-ID` to the same resource; cancelling one adds an `EXDATE` to the
  master rather than deleting the resource.

## Contacts: never rebuild a card

`update_contact` mutates the parsed vCard in place. It must stay that way.

A real contact carried `PHOTO`, three `X-SOCIALPROFILE` entries, two
`X-ABRELATEDNAMES` (Brother, Father), `X-ADDRESSBOOKSERVER-PHONEME-DATA`,
`X-IMAGEHASH` and more — none of which the tool surface models. Rebuilding a
card from `to_dict` output would delete every one of them, silently, on an edit
as small as fixing a typo in a name. `tests/test_vcard.py` pins this.

Apple's own conventions that the parser handles, all seen on real cards:

- Labels live in a companion `itemN.X-ABLabel`, wrapped as `_$!<Work>!$_` for
  built-ins and left bare for user-defined ones (`Google Voice`).
- **Not every grouped property has a label.** A real `item4.ADR` was grouped
  only with `item4.X-ABADR` (a country code), so a group with no label must
  still fall back to `TYPE`.
- `type=pref` marks the preferred value, as repeated `type=` params rather than
  a comma-separated list. Preferred entries are returned first so `emails[0]`
  is the right one.
- A street can be genuinely multi-line (`\n`-escaped). The structured `street`
  keeps the break for a mailing label; `formatted` must not, since it is meant
  for a single field such as an event location.
- `vobject` serializes property names upper-cased (`X-ABLABEL`). Verified live
  that iCloud accepts this and labels survive the round trip.

## Working with iCloud

Verified against a live account. Each of these cost a real bug before it was
understood, so check here before assuming the spec applies:

- **`<C:expand>` works**, so recurrence is expanded server-side and there is no
  local cache. Do not add one without a measurement showing it is needed.
- **A `prop-filter` is silently ignored when a `time-range` is present**, and
  the reverse order is refused with a `412`. Text matching is therefore done
  client-side in `server._matches`. Do not "optimize" it back into the query.
- **`RECURRENCE-ID` is returned in UTC** while `DTSTART` keeps its own zone.
  Event ids carry `ical.recurrence_key` — the literal server form — and only
  ever round-trip it. Never rebuild a recurrence id from a formatted local time.
- **Resource names are not tame.** Anything imported from Google is named
  `<uid>@google.com.ics`. Event ids join their parts with `\x1f` for exactly
  this reason; `/` and `@` both split such a name in the middle.
- **iCloud publishes no default calendar.** `schedule-default-calendar-URL`
  comes back empty, so `DAV_MCP_DEFAULT_CALENDAR` is how a default is set.
- **A burst of writes gets the account throttled**, and iCloud answers `503`
  with `Retry-After: 30` — on *every* request, including reads, until it
  clears. It looks like an outage and reads like a broken server. `_request`
  retries briefly and then raises `Throttled`, whose message says explicitly
  that it is rate limiting rather than a credential problem. Keep the retry
  budget under ~10s: obeying `Retry-After` literally made tool calls hang for
  90 seconds, which is worse than failing fast and letting the caller retry.

## Scope

The tool surface deliberately mirrors the Fastmail calendar tools, with
`find_free_time` added on top — Fastmail has no availability tool, so that one
is an addition rather than parity.

Free/busy lookup for *other people* (RFC 6638 scheduling-outbox `VFREEBUSY`) is
deliberately not built. The account has a `schedule-outbox` and the mechanism
exists, but there is no working federated free/busy across providers on the
open internet: iCloud asking Gmail about an external address generally returns
`3.7` or nothing. Do not add it without first probing real addresses and
confirming useful data comes back.
`compose_event` is intentionally absent: it stages an event into a confirmation
widget that only exists inside claude.ai, and a third-party server cannot summon
that UI. Write confirmation is left to the client's own tool-approval prompt.

Reminders (VTODO) are out of scope. VTODO calendars are filtered out of
`list_calendars`.

## Scheduling sends real, irrevocable email

`create_event(participants=…)`, `update_event(addParticipants/removeParticipants)`
and `rsvp_event` all make iCloud send iMIP mail the instant the PUT lands. There
is no local send step to intercept and no way to recall a message. When changing
this code:

- **Validate before writing, never partially.** A malformed participant list is
  rejected whole; half-inviting a list mails some people and not others.
  Every rejection path is covered by a test asserting `put` was never awaited —
  keep it that way.
- **The organizer must be one of the account's own calendar-user-addresses.**
  iCloud accepts a PUT naming a foreign organizer and silently sends nothing,
  which looks exactly like success. `_resolve_organizer` checks it up front.
- **Do not add an ORGANIZER to an event with no guests.** It turns a personal
  event into a one-person meeting, and some clients then mail on every edit.
- **Any edit to an event with guests mails all of them.** The tool replies say
  so; do not make them quieter.
- The organizer is also an attendee with `PARTSTAT=ACCEPTED`, and is never
  removable via `removeParticipants` — dropping them orphans the event for
  everyone else.

### Confirming mail actually went out

iCloud stamps each ATTENDEE with `SCHEDULE-STATUS` after it tries to deliver
(RFC 6638). Re-`GET` the resource after a write and read it: `1.1` means
delivered, `5.1`/`5.2` mean it could not be. This is the only way to tell a
successful send from a PUT that iCloud accepted and then quietly did nothing,
which is exactly what happens when the ORGANIZER is not one of the account's
own addresses.

iCloud rewrites addresses in two different ways, and **neither leaves a usable
address in the property value**:

- In the copy it *stores*, ORGANIZER and the account's own ATTENDEE become an
  internal principal URL: `…/aMTEwMjY5…/principal/`.
- In the iMIP mail it *sends*, they become an opaque reply-routing token:
  `mailto:2_GEYTAMRWHE4DE…@imip.me.com`.

In both cases the real address survives only in the `EMAIL` parameter. This is
why `ical._address_email` reads `EMAIL` first and only falls back to the
property value — reverse that order and every organizer comes back as an
unreadable Apple token.
