# calendar-mcp

An MCP server for Apple iCloud Calendar, over CalDAV.

It exposes the same calendar surface the Fastmail MCP server does — list, search,
create, update, delete, RSVP — so a model that has driven one can drive the other
without relearning field names. It runs over stdio locally, or over authenticated
HTTP for a remote client such as a Claude connector.

No local app, no EventKit, no AppleScript: it talks to `caldav.icloud.com`
directly, so it does not need to run on the machine your calendar is synced to,
and it needs none of the macOS privacy grants that an EventKit-based server does.

## Tools

| Tool | What it does |
| :--- | :--- |
| `list_calendars` | Calendar ids, names and colors. Event calendars only; reminder lists are out of scope. |
| `search_events` | List a period or search it. Recurring events come back expanded, one result per occurrence, each independently addressable. |
| `create_event` | Create a timed, all-day, or recurring event. |
| `update_event` | Change any field. Given an occurrence id, edits that occurrence alone; given the series id, edits the series. |
| `delete_event` | Delete an event. Given an occurrence id, cancels that occurrence and leaves the series intact. |
| `rsvp_event` | Respond to an invitation: accepted, tentative or declined. |

`GET /health` is served unauthenticated alongside them, for monitoring.

### Invitations send real mail

`create_event(participants=…)`, `update_event(addParticipants=…/removeParticipants=…)`
and `rsvp_event` all cause iCloud to send iMIP email, immediately and
irrevocably. iCloud advertises `calendar-auto-schedule`, so it does the sending
itself the moment a PUT lands — this server never touches SMTP and has no way to
recall anything.

Two consequences worth knowing before wiring this to a model:

- **Any** edit to an event that already has guests mails all of them, including
  a one-word title fix. The tools say so in their replies rather than presenting
  such an edit as silent.
- The organizer address must be one of the account's own
  calendar-user-addresses. iCloud accepts a PUT naming a foreign organizer and
  then silently sends nothing, which is indistinguishable from success — so
  `from` is validated against the account's real identities before writing.
- **Sending is not the same as delivering.** After a write that touches
  participants, the event is re-read and iCloud's per-attendee
  `SCHEDULE-STATUS` is reported: who it reached, and who it did not. An address
  whose mail server refuses the message comes back `5.1`, and the tool says so
  rather than claiming the invitation was sent.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then fill in the two credentials
```

`APPLE_APP_PASSWORD` is an **app-specific password** generated at
[appleid.apple.com](https://appleid.apple.com), not your Apple ID password. A
two-factor account rejects the account password outright.

```bash
uv run calendar-mcp                              # stdio
CALENDAR_MCP_TRANSPORT=http uv run calendar-mcp  # http on 127.0.0.1:18790
```

To use it from Claude Code over stdio:

```bash
claude mcp add calendar -- uv --directory /path/to/calendar-mcp run calendar-mcp
```

## Configuration

Everything is environment variables; nothing is read from `.env` by the server
itself, which only documents them.

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `APPLE_ID` | — | Apple ID email. Required. |
| `APPLE_APP_PASSWORD` | — | App-specific password. Required. |
| `CALENDAR_MCP_DEFAULT_CALENDAR` | first writable | Calendar `create_event` writes to when the caller names none, by id or display name. |
| `CALENDAR_MCP_TIMEZONE` | host zone | IANA zone assumed when a caller omits `timeZone`. |
| `CALENDAR_MCP_CALDAV_ROOT` | `https://caldav.icloud.com` | CalDAV entry point. |
| `CALENDAR_MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `CALENDAR_MCP_HOST` | `127.0.0.1` | HTTP bind address. |
| `CALENDAR_MCP_PORT` | `18790` | HTTP bind port. |
| `CALENDAR_MCP_STATELESS` | `true` | `false` restores per-client sessions. |
| `CALENDAR_MCP_AUTH` | `none` | `none` or `password`. HTTP only. |
| `CALENDAR_MCP_PASSWORD` | — | Shared password. Required when `AUTH=password`. |
| `CALENDAR_MCP_BASE_URL` | — | Public URL; becomes the OAuth issuer. Required when `AUTH=password`. |
| `CALENDAR_MCP_STATE_DIR` | `~/.calendar-mcp` | Where OAuth state is persisted. |

**Set `CALENDAR_MCP_DEFAULT_CALENDAR`.** iCloud does not publish
`schedule-default-calendar-URL` — it comes back empty — so with nothing
configured the only available tie-break is the order the server happens to list
collections in, which changes the moment you add a calendar to the account.

## Authentication

A remote MCP client has one input field: a URL. There is nowhere to put an API
key. So `CALENDAR_MCP_AUTH=password` starts a self-contained OAuth 2.1
authorization server whose only credential is one shared password — the client
discovers it, registers itself, and gets redirected to a password form.

Dynamic client registration, PKCE, discovery metadata and the `401` challenge
come from FastMCP and the MCP SDK. This project adds the login screen and the
credential check. Registered clients and tokens persist across restarts;
authorization codes and in-flight logins are deliberately memory-only.

Bind to localhost and put a tunnel or reverse proxy in front of it. See
[docs/deployment-macos.md](docs/deployment-macos.md).

## Notes on iCloud

Four findings that shaped the implementation, each verified against a live
account rather than taken from the spec:

- **Recurrence is expanded server-side.** `calendar-query` honors `<C:expand>`,
  so occurrences arrive individually with their own `RECURRENCE-ID`. There is no
  local expansion and no cache — ranged queries are fast enough not to need one.
- **Text search and time ranges are mutually exclusive.** RFC 4791 permits a
  `prop-filter` alongside a `time-range`, but iCloud *silently ignores* the text
  match when both are present, and rejects the reverse order with a `412`. So
  `query` is matched client-side, which also lets it cover descriptions,
  locations and participants rather than titles alone.
- **`RECURRENCE-ID` comes back in UTC** while `DTSTART` stays in the event's own
  zone. Event ids therefore carry the literal server form of the key and only
  ever round-trip it; the human-readable `recurrenceId` is converted for display.
- **Scheduling is the server's job, not ours.** `OPTIONS` advertises
  `calendar-auto-schedule`, so an ORGANIZER plus ATTENDEEs on a PUT is all it
  takes to send invitations, and changing your own `PARTSTAT` is all it takes to
  reply.

## Development

```bash
uv sync --extra test
uv run pytest
```

The suite is entirely offline — no test touches iCloud. Fixtures reproduce the
exact payload shapes iCloud returns, with identities replaced.

For manual verification against a real account, create a scratch calendar and
point `calendarId` at it. Do not run write experiments against a calendar you
care about.

## License

MIT
