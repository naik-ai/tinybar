# The Models workspace: a native client of the local gateway

Date: 17 November 2026.
Status: built against a stand-in and checked once against the real gateway; owner review pending.

## Context

The owner asked for a page in Tinybar (now Modelbar) that shows model selection and how models run, the unified API with
its requests and responses, logs and traces, as a clear API client he can test inside the app (in session,
17 November 2026). The backend is `backend/` (docs/local-backend.md); this app is its client, never its
supervisor of record.

## Decision

- `LocalModelCore`, a Foundation-only target: the gateway's wire types and an async `URLSession` client.
  It calls `http://127.0.0.1:<port>` only (no `localhost`, no other host, no redirects, proxies, cookies or
  cache), reads the key from its 0600 file for each call (a link or a group-readable file is refused) and
  never keeps, logs or shows it. Every failure is a `GatewayError`; unknown token counts stay `nil`.
- The workspace is the Stats window's **Local models** page (⌘6, and a popover footer button that only
  opens the window). Sections: Overview, Models (details drawer), Try, Activity, Logs, Settings. It reuses
  the Stats palette and type, and it is built when shown and released with the window.
- It asks the gateway only while its window is visible (occlusion tracked), one poll loop for the section in
  view (default 5 s; 1 s while a pull or load runs); Logs follow only while following. The popover asks
  nothing.
- Start runs `<backend>/.venv/bin/tinybar-models serve --port <port>` detached; Stop runs `tinybar-models
  stop`. A gateway that already answers makes Start a success that starts nothing. The app signals only the
  one process it started (SIGTERM, if still alive 20 s after `stop`), never by port or name, and asks before
  stopping a gateway it did not start.
- Try sends exactly the bytes it shows (encoded once, or the raw text as typed), per model kind: chat for
  mlx-vlm models, one image for `nemotron-parse`, image plus blocks for `jev`. Nothing typed or returned is
  saved; Copy as curl reads the key with `$(cat <key file>)`.
- Settings keep only an address, a backend folder, a key file path and an interval, in the fork's own
  defaults domain.
- `Tests/StandIn/` holds a standard-library stand-in gateway and a fake backend folder, for development and
  snapshots without the real gateway.

## Consequences

- The main window is now resizable and can go full screen; the 960 × 600 card stays centred.
- The gateway it starts is a long-lived child of the user's choosing, outside SPEC §2's rule against
  long-lived child processes, which keeps holding for everything the app starts by itself.
- Fields the gateway's API lacks are read from `registry.json` and marked so.
