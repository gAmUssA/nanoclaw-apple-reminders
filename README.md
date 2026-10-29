# NanoClaw Apple Reminders

A private-by-default NanoClaw plugin in the `gamussa` namespace. A Python 3.11+
CLI runs inside Linux agent containers and calls an authenticated loopback
bridge on the owner's Mac. The bridge uses a signed local EventKit helper to
read, create, update, and complete actual Apple Reminders.

## Why a Mac bridge

Upgraded iCloud Reminders and CalDAV task calendars are separate stores.
A successful CalDAV VTODO write does not establish that a reminder will appear
in Apple's Reminders app. This implementation uses EventKit instead, following
the operator's approved change from the original CalDAV design.

- [BusyCal's explanation of the separate stores](https://www.busymac.com/docs/faqs/112990-reminders-in-ios-13-and-macos-catalina-drops-support-for-caldav/)
- [Apple: requesting Reminders access](https://developer.apple.com/documentation/eventkit/ekeventstore/requestfullaccesstoreminders(completion:))

No iCloud username, app-specific password, CalDAV discovery, or cloud session is
required. Advanced Data Protection is not bypassed. The Mac must be signed in
to the account and already syncing Reminders. A local save does not prove sync
to an iPhone or another Mac.

## Requirements

- macOS 14+ with Reminders configured, a logged-in GUI session, and Xcode command
  line tools (`xcrun swiftc` supporting Swift 6).
- Python 3.11+ on the Mac and in the Linux agent. Runtime Python is stdlib-only.
- NanoClaw with an explicit `gamussa/nanoclaw-apple-reminders` overlay and a
  container-to-host connection (tested deployment uses OrbStack).
- OneCLI for credential injection, or a securely supplied
  `APPLE_REMINDERS_BRIDGE_TOKEN` environment variable in a trusted local client.

## Install the host service

```sh
python3 host/install.py --authorize --start
```

Allow **NanoClaw Reminders** in the macOS prompt. Reminders requires full access;
there is no write-only EventKit Reminders permission. The bridge only exposes
the commands below, not arbitrary AppleScript, shell execution, or calendar APIs.

The installer creates a local ad-hoc-signed app and LaunchAgent
`io.gamov.nanoclaw.reminders`. It binds **127.0.0.1:10304**. Its random bearer is
stored in `~/.config/nanoclaw/apple-reminders/bridge-token` (0600, parent 0700).
Runtime app files default to the NanoClaw data directory when installed under
`local-plugins`, otherwise `~/.local/share/nanoclaw/data/macos`.
Use `--runtime-dir` to override that location. Rebuilding an ad-hoc-signed helper
can require granting permission again; rerun with `--authorize` if necessary.

To vault the token for **only your main agent**, obtain its non-secret ID with
`onecli agents list`, then run:

```sh
python3 host/configure_onecli.py \
  --project-env /path/to/nanoclaw/.env --agent-id YOUR_MAIN_AGENT_ID
```

This setup first creates an inert credential, removes other agents' grants,
verifies the remaining grants, then stores the real token. It injects an
Authorization bearer only for `host.docker.internal` and
`/v1/reminders/command`. Never paste the token into chat or put it in CLI arguments.
The API/CLI project selected in OneCLI must match the supplied project .env.

## Install the plugin

Install from this checkout with Tessl, retaining the `gamussa` namespace. In the
NanoClaw Tessl workspace, add a file dependency pointing to this plugin:

```json
{"gamussa/nanoclaw-apple-reminders":{"source":"file:/path/to/nanoclaw-apple-reminders"}}
```

Run `tessl install --yes --accept-warnings`. Add
`gamussa/nanoclaw-apple-reminders` to the main chat's `additionalTiles` through
the authorized host configuration. NanoClaw's overlay loader must support
explicit `owner/plugin` names; older single-owner loaders require that small
host change. Do not install under another owner's namespace as a workaround.

Fresh agents receive `tessl__apple-reminders`. The CLI defaults to
`http://host.docker.internal:10304` and honors `HTTP_PROXY` for OneCLI injection.
For host-side testing, set `APPLE_REMINDERS_BRIDGE_URL=http://127.0.0.1:10304`
and supply the bearer through process environment, never printed or committed.

## Commands

```sh
python3 skills/apple-reminders/scripts/apple_reminders.py doctor
python3 skills/apple-reminders/scripts/apple_reminders.py list-lists
python3 skills/apple-reminders/scripts/apple_reminders.py list-todos --list @default
python3 skills/apple-reminders/scripts/apple_reminders.py list-todos --list LIST_ID --include-completed
python3 skills/apple-reminders/scripts/apple_reminders.py add-todo \
  --list @default --title 'Example reminder' --uid 'scheduler-task-123' \
  --notes 'Optional notes' --due '2026-11-01T14:00:00Z'
python3 skills/apple-reminders/scripts/apple_reminders.py complete-todo \
  --list LIST_ID --uid 'scheduler-task-123'
```

Success: one `{ "ok": true, "command": "…", "data": … }` JSON line on stdout,
exit 0. Failure: `{ "ok": false, "error": "short_token", "detail": "…" }` on
stderr, exit 1. `--help` prints ordinary help text.

`list-lists` reports native list IDs, names, writable status and the actual
default list. CalDAV hrefs/principals are not relevant to this backend.
`doctor` checks the bridge and EventKit permission and reports discovered lists.

Reuse `--uid` and the same concrete list ID to update instead of duplicate.
The marker is stored in the reminder's URL field. Editing that URL manually
removes the marker. Omitting `--uid` generates a new one; the response returns
it. Updates preserve completed status and omitted notes/due values. Completion
also accepts the native ID returned for an unmanaged existing reminder.
A supplied due date also creates one absolute alarm at the saved due instant.
Retries do not duplicate it. Changing the due replaces the absolute alarm at
its previous due time, preserving other absolute, relative and location alarms.
Omitting `--due` leaves due dates and alarms unchanged. Due dates retain the
existing second precision. Records include `alarm`: the ISO8601 UTC absolute
alarm matching the due time, or null when absent. This is stored-alarm evidence,
not proof of a displayed notification; device notification settings and sync
still apply. No delete or list-creation endpoint is exposed.

## Optional schedule-task mirror

The companion NanoClaw admin integration reads
`/workspace/group/reminder-mirror.json`:

```json
{"backend":"apple-reminders","list_id":"@default"}
```

Missing configuration keeps `google-tasks` as the default. There is no automatic
fallback or double write. Apple mirrors pass the scheduler task ID as `--uid`,
persist `mirror_backend`, `external_uid`, and the concrete returned list ID in
`reminders-state.json`, and use `complete-todo` on completion. Historical entries
without `mirror_backend` remain Google Tasks entries. This plugin alone does
not change schedule-task; install the companion host/admin change as well.

## Security and failure behavior

The server authenticates every request, rejects browser Origin headers,
disallows redirects, limits input size, and accepts a fixed set of commands.
Private request files are short-lived and stored in a 0700 directory. HTTP
access logs omit request data. Tokens and native error bodies are never echoed.
Reads and writes are serialized; native operations have a 50-second watchdog.
On timeout, retry with the original UID, never a new one.

The scope of a bridge grant is full read/write Reminders access. Give it only
to agents authorized for that access. On macOS, the helper accesses lists the
signed-in user can access, including shared lists; select only the destination
the user requested. Read-only lists reject writes.

## Tests

```sh
uv run --no-project --with pytest python -m pytest tests -q
```

Tests use mocked native operations behind real local HTTP requests to check
authentication, envelope shape, argument validation, stable-UID transport,
completion, timeouts, redirect rejection, and credential file permissions.
Native EventKit idempotency and device sync require an explicit real-account
acceptance test; mocked tests do not establish either.

## Stop / uninstall

```sh
launchctl bootout gui/$(id -u)/io.gamov.nanoclaw.reminders
```

Remove the LaunchAgent plist from `~/Library/LaunchAgents`, revoke its OneCLI
grant/secret, and remove the plugin overlay if desired. Existing reminders are
not deleted. Keep Google selected until an Apple mirror has been verified.

## Ownership

Owned and published by `gAmUssA`; plugin namespace `gamussa`. No credentials,
account IDs, personal reminders, generated app bundles, or runtime logs belong
in this repository. Licensed under MIT.
