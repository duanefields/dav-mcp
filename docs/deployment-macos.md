# Running as a background service on macOS

How to host the HTTP server on an unattended Mac and reach it from a Claude
connector over a Cloudflare Tunnel.

This is the sibling of things-mcp's `docs/deployment-macos.md`, and most of it is
the same. The differences are worth stating up front, because they are the parts
that make this the easier of the two to run.

## What this server does not need

things-mcp reads another application's group container and drives Things through
Apple Events. That is what forces it to be a LaunchAgent with a live GUI session,
and it is what makes a macOS privacy prompt hang the process on first start with a
live PID, an unbound port and an empty log.

**None of that applies here.** calendar-mcp talks to `caldav.icloud.com` over
HTTPS and touches no local application, no protected container and no Apple
Events. So:

- There is no Full Disk Access grant to make, and none to re-grant after an
  interpreter upgrade.
- There is no Automation prompt waiting to ambush the first write.
- It does not need a GUI session, so it *could* be a LaunchDaemon.

Run it as a **LaunchAgent anyway** if the host already supervises other
services that way and already logs in automatically -- one mechanism then covers
all of them. `/health` still publishes `python_version`, because it costs nothing and
keeps the two services' monitoring identical.

## Invoke the interpreter directly, not `uv run`

`uv run` spawns the interpreter as a child, so launchd ends up supervising the
wrapper. Killing the job leaves the real server holding the port. Point
`ProgramArguments` at the venv's interpreter.

## Credentials

The server authenticates to iCloud with an **app-specific password** generated at
appleid.apple.com, not the Apple ID account password. Two-factor accounts reject
the account password outright, with a `401` that the server reports as "iCloud
rejected the credentials".

The app-specific password grants full access to iCloud data. It lives in the
plist, which is why the plist is `chmod 600`.

## Template

Save as `~/Library/LaunchAgents/com.example.calendar-mcp.plist`, replacing the
placeholders. It contains two secrets, so `chmod 600` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.calendar-mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/Code/calendar-mcp/.venv/bin/python</string>
    <string>-m</string>
    <string>calendar_mcp</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/Code/calendar-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>APPLE_ID</key><string>you@example.com</string>
    <key>APPLE_APP_PASSWORD</key><string>abcd-efgh-ijkl-mnop</string>
    <!-- iCloud publishes no default calendar, so pin it. Without this the
         server writes to whichever calendar it happens to list first, which
         changes the moment a calendar is added to the account. -->
    <key>CALENDAR_MCP_DEFAULT_CALENDAR</key><string>Personal</string>

    <key>CALENDAR_MCP_TRANSPORT</key><string>http</string>
    <key>CALENDAR_MCP_HOST</key><string>127.0.0.1</string>
    <key>CALENDAR_MCP_PORT</key><string>18790</string>
    <key>CALENDAR_MCP_AUTH</key><string>password</string>
    <key>CALENDAR_MCP_PASSWORD</key><string>REPLACE-WITH-A-LONG-RANDOM-VALUE</string>
    <key>CALENDAR_MCP_BASE_URL</key><string>https://calendar.example.com</string>
    <key>CALENDAR_MCP_STATE_DIR</key><string>/Users/USERNAME/.calendar-mcp</string>
    <!-- Without this, Python block-buffers to the log file and it stays empty,
         which makes a startup problem look like total silence. -->
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/.calendar-mcp/server.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.calendar-mcp/server.log</string>
</dict>
</plist>
```

```bash
chmod 600 ~/Library/LaunchAgents/com.example.calendar-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.calendar-mcp.plist
curl -s localhost:18790/health
```

**Port 18790** is this project's default. Bind to `127.0.0.1` and reach it
through the tunnel. The choice follows the same rule as its sibling: not 8080,
which collides with the first thing any other developer tool tries, and below
49152 so it cannot clash with an outbound ephemeral socket.

## Cloudflare Tunnel

Give the server its own ingress hostname on a Cloudflare Tunnel, pointed at its
localhost port. If the host already runs a tunnel for another service, add a
second entry to `~/.cloudflared/config.yml` rather than a second tunnel:

```yaml
ingress:
  - hostname: other-service.example.com
    service: http://127.0.0.1:18789
  - hostname: calendar.example.com
    service: http://127.0.0.1:18790
  - service: http_status:404
```

Then route the new hostname and restart the tunnel:

```bash
cloudflared tunnel route dns <TUNNEL-NAME> calendar.example.com
sudo launchctl kickstart -k system/com.cloudflare.cloudflared
```

Two rules carry over from the things-mcp deployment and both are load-bearing:

- **No Cloudflare Access application in front of this hostname.** Access
  intercepts the OAuth callbacks and breaks the connector handshake.
- **The hostname is settled before cutover**, because it becomes the OAuth issuer
  and the resource identifier. Changing it later invalidates every existing
  connector registration.

Cloudflare terminates TLS at its edge, so traffic is plaintext inside
Cloudflare's network. Tunnel hostnames appear in certificate transparency logs
within hours and get scanned automatically, which is why `/health` publishes no
account detail and failed logins are logged with their source IP.

## Connecting from Claude

Hand Claude's custom connector setup the bare URL:

```text
https://calendar.example.com/mcp
```

It registers itself (dynamic client registration is enabled), gets redirected to
the server's own `/login` page, and you enter `CALENDAR_MCP_PASSWORD` once.

MCP Inspector succeeding is **not** sufficient evidence. There is a known pattern
of servers that authenticate fine in Inspector and fail in Claude connectors.
Test against Claude directly and treat that as the only gate.

Cutover order matters, because the issuer is baked into the registration:
configure the server with the final `CALENDAR_MCP_BASE_URL` → start HTTP mode →
verify over the tunnel from off-network → add the connector in Claude →
authorize once.

## Monitoring

`scripts/healthcheck.sh` polls `/health` and reports to a dead-man's-switch.
Configure it in `~/.calendar-mcp/check.env`:

```bash
HEALTH_URL=http://127.0.0.1:18790/health
PING_URL=https://hc-ping.com/your-uuid-here
```

```cron
*/10 * * * * /Users/you/Code/calendar-mcp/scripts/healthcheck.sh >> /Users/you/.calendar-mcp/check.log 2>&1
```

`chmod 600` the config: the ping URL is a capability, not just an address. If the host runs more than one such
service, give each its **own** healthchecks.io UUID, or one service's silence
will be masked by the other's pings.

It reports failure on three things:

- **No response.** The server is down or wedged.
- **`status: degraded`.** iCloud could not be reached, or the account returned no
  event calendars — which is what a revoked app-specific password looks like.
  This is the check that matters here: an app-specific password can be revoked
  from appleid.apple.com without anything local changing, and every tool call
  starts failing while the process stays perfectly healthy.
- **The last write failed.** `/health` carries the outcome of the most recent
  write, so a calendar that has silently stopped accepting them is visible
  without waiting for someone to notice.

An outward ping is what makes the whole machine being gone detectable. A monitor
running on the same host cannot report its own host's death. Set the expected
period to match the cron interval, with a grace of two or three intervals.

## Deploying by pushing

`scripts/self-update.sh` pulls the tracked branch, syncs dependencies if they
moved, and restarts the service — so a push is a deploy. Configure it in
`~/.calendar-mcp/update.env`:

```bash
REPO_DIR=/Users/you/Code/calendar-mcp
BRANCH=main
LAUNCH_LABEL=com.example.calendar-mcp
PING_URL=https://hc-ping.com/a-different-uuid
RESET_HARD=true
```

```cron
*/15 * * * * /Users/you/Code/calendar-mcp/scripts/self-update.sh >> /Users/you/.calendar-mcp/update.log 2>&1
```

`RESET_HARD=true` is right for a host that is only ever deployed to: without it
one stray edit wedges every future deploy and nobody is reading the log to
notice.

One trap when setting this up: **build the virtualenv where it will finally
live.** `uv` records absolute paths, so a venv created in one directory and then
moved leaves the editable install pointing at the old location, and the service
fails with `No module named calendar_mcp`. Re-run `uv sync` after any move.

## Checking on it

```bash
launchctl list | grep calendar-mcp     # pid, last exit code
curl -s localhost:18790/health         # reachability, calendar count, last write
tail -f ~/.calendar-mcp/server.log
```

A healthy response looks like:

```json
{
  "status": "ok",
  "caldav_reachable": true,
  "event_calendars": 2,
  "error": null,
  "python_version": "3.12.13",
  "last_write": {"at": null, "ok": null, "action": null, "error": null}
}
```
