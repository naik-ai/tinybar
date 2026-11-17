# Local model backend

Status: backend built and tested, 17 November 2026, after two review rounds; it starts at login as a
per-user LaunchAgent and loads no model until a request needs one. No UI yet; the API shapes below are what
the Swift Models page is written against (additive changes only). Owner decisions it follows:
[handoff](handoff-local-model-platform.md#owner-decisions-17-november-2026-his-words-in-session).

`backend/` is a small Python package, managed by `uv`, beside the Swift app (which stays dependency-free).
It gives every repository on this Mac **one loopback address** that speaks the OpenAI chat-completions API,
starts a model's MLX worker the first time a request names it, stops it when it has been idle or when the
memory budget needs the room, and keeps a request ledger with no content. Models come only from its
registry and its one weights store, `~/Models`.

## Changes since the first draft

For the Swift client: every change is additive; no field or route was renamed or removed.

- `GET /api/models[/<name>]` adds `phase` (`stopping` while a stopped worker's process is still exiting;
  `state` then says `unloading`), `server {kind, maxTokens}`, `capabilities`, `licence {id, readFrom,
  commercialUse}`, `readyTimeoutSeconds`, `idleSecondsLeft`, `pinnedBy`, `lastError {at, kind, message}`,
  and in `weights`: `verifiedAt`, `reused`, `sha256Pinned`, `filesVerified`, `approxSizeGb`, `download`,
  `alternative {source, repo, revision, approxSizeGb, commercialUse}`. `weights.path` is now `~`-relative.
- `GET /api/status` adds `version`, `startedAt`, `uptimeSeconds`, `port`, `inFlightTotal`, `waitingTotal`,
  `registryPath` (`~`-relative), `defaults`, `storeFreeGb`, `refusals {unauthenticated, host, origin}`, and
  `memory.abandoned` and `memory.stoppingModels`.
- Pulls: progress adds `currentFile` and a `bytesTotal` while `searching`; phases add `cancelled`; new
  `POST /api/models/<name>/pull/cancel` (409 when none runs). Pull messages are `~`-relative and carry no
  exception text.
- `GET /api/logs` adds `since` (a byte offset) and returns `next` (and `rotated` with `since`).
- `GET /api/requests` adds `model` and `outcome` filters and a `before` cursor; returns `next`.
- Ledger rows hold route templates and registry model names only; unauthenticated callers write no row.
- Behaviour: one gateway per state folder (`gateway.lock`); `load` never takes a request slot; workers bind
  their own port; an abandoned request stops its worker; non-`ok` health or two failed health probes restart
  an idle worker; images over 50 million pixels are refused in the gateway for every model; a worker's small
  files are re-hashed at each launch; `Retry-After: 3` when a stopping worker holds the budget.
- Availability (owner, 17 Nov): `POST /api/models/<name>/available {"available": true|false}`; models gain
  `available` and `availableBy` (`"registry default"` or `"user"`); `/api/status` gains `availableModels`.
  A chat or `load` naming a model that is off gets **403 with `error.code` `"model_not_available"`** (a string
  code; every other error keeps the HTTP status as its `code`). `/v1/models` lists only available, downloaded
  models. The ledger records such a request with outcome `not_available`.
- Start at login (owner, 17 Nov): `/api/status` gains `startedBy` (`"launchd"` or `"manual"`) and `loginItem`
  (`"installed"` or `"not installed"`). CLI: `start`, `enable`, `disable`, `login install|remove|status`.
- Durable request rows (review, 17 Nov): every accepted request has its ledger row at once, before work is
  dispatched. Rows gain `phase` (`queued`, `loading`, `generating` while in flight; null once done),
  `ended_at` (null while in flight and for interrupted rows) and, in `/api/requests` only, `elapsed_ms` (for
  in-flight rows). New outcomes: `in_flight`, `interrupted` (error kind `gateway_restart`), `partial` (error
  kinds `stream_ended_early`, `body_ended_early`); all are `/api/requests?outcome=` filters. The row's id is
  the request's `X-Request-Id`.
- Pulls: new phase `cancelling` (before `cancelled`); `POST /api/models/<name>/pull` answers 409 (error
  kind `pull_cancelling`) while a cancelled pull is still stopping.
- CLI: `install` and `uninstall` (the stable installation below).
- Nemotron's weights now live in the store (no `weights.external` in its entry); its `weights.path` reads
  `~/Models/hf/hub/models--nvidia--Nemotron-3.5-Parse/snapshots/62cb1e4b…`.

## Architecture

```text
 Draftbook · scripts · Tinybar page (later)
            │  Authorization: Bearer <key>, Host: 127.0.0.1 (IPv4 only)
            ▼
 ┌──────────────────────── gateway 127.0.0.1:18900 ────────────────────────┐
 │ Host/Origin/key checks → chat gate (fields, data: images, 25 MB)        │
 │ registry.json → Manager: load on demand · queue · budget · LRU · TTL    │
 │ ledger.sqlite3 (no content) · logs/ (rotated by size) · pulls → ~/Models│
 └───────┬───────────────┬───────────────┬──────────────────────────────────┘
         │ per-run worker key; stdin pipe (EOF = gateway gone); port pipe
         ▼               ▼               ▼
   mlx_server       nemotron_server   jev_server (+ render.py)
   (mlx-vlm 0.7.12   (generate +       (Jev scorer per block)
    behind gate.py)   block parser)
         └──── memory.py in each: MLX limits, cache cleared per request, 503 over budget
```

Each worker binds port 0 on 127.0.0.1 itself and writes the port it got to a pipe the gateway gave it, so
the gateway never sends the worker key to a port it guessed. Each worker's stdin is a pipe the gateway
holds: if the gateway dies by any signal, SIGKILL included, the worker reads end-of-file and exits.

Ported from Draftbook's `services/models` at commit `3d7d468` (initiative 003 with its S-06a memory bounds):
`wire.py`, `gate.py`, `memory.py` (with `work_lock` and the non-blocking `health()`), `mlx_server.py`,
`nemotron_server.py`, `jev_server.py`, `render.py` (byte-identical, so `draftbook-render/2` scores carry
over), the catalogue as `registry.py`/`registry.json`, `pull.py` as `store.py`, the key handling as
`keys.py`, and the proof scripts as `backend/proof/`. Changed from Draftbook in the port: workers report
their port and exit with the gateway, an over-budget 503 carries `Retry-After: 30`, mlx-vlm's access log is
off, and images are capped at 50 million pixels. New here: `gateway.py`, `workers.py`, `ledger.py`,
`paths.py`, `cli.py`. Only the standard library is added; MLX, mlx-vlm and huggingface-hub are pinned to
Draftbook's versions (mlx 0.32.4, mlx-vlm 0.7.12, huggingface-hub 2.4.0; `backend/uv.lock` started from
Draftbook's lock, so every transitive pin matches).

## Run it

```sh
uv run --directory backend tinybar-models serve      # 127.0.0.1:18900; --port or TINYBAR_MODELS_PORT
uv run --directory backend tinybar-models status     # memory totals and each model's state
uv run --directory backend tinybar-models pull NAME  # or `all`; verifies or downloads into ~/Models
uv run --directory backend tinybar-models load NAME | unload NAME
uv run --directory backend tinybar-models stop       # stops every worker, then the gateway
uv run --directory backend pytest                    # no model, MLX or network needed
```

Further commands: `start` (starts the gateway the right way: `launchctl kickstart` when the login item is
installed, else a detached `serve` in its own session), `enable NAME` / `disable NAME` (availability, below),
and `login install|remove|status` (start at login, below). `status` shows each model as on or off.

SIGTERM, SIGINT and SIGHUP all stop the workers and then
the gateway; shutdown shuts the read side of idle keep-alive client connections, so it does not wait for
their 30 s timeout (launchd allows 20 s). The gateway at rest, measured by the reviewer: 17 MB, 0% CPU, 23 ms to import, and it never
imports MLX (only workers do).

`serve` takes an exclusive lock on `<state folder>/gateway.lock` for its whole life: a second `serve` on
the same state folder exits 1 naming the pid that holds it, and touches nothing (not `gateway.json`, not the
budget). It refuses any host but `127.0.0.1`. Before binding, it connects to the port; if anything answers
(a wildcard `0.0.0.0` listener included) or the bind fails, it exits 1 naming the process. Nothing ever
stops a process the gateway did not start. `status`, `load`, `unload`, `pull` and `stop` send the key only
after checking `gateway.json`: it must name `127.0.0.1`, and its pid must be alive and be a tinybar-models
process.

The key is `TINYBAR_MODELS_API_KEY`, else the file `~/Library/Application Support/TinybarLocal/api-key`
that the first `serve` generates (mode 0600; a key under 32 characters, or a file group or others can read,
is refused). It is compared in constant time and never printed, logged or put on a command line. Workers
get a different key, generated per run; clients never reach a worker.

## API

| Route | Key | What |
|---|---|---|
| `POST /v1/chat/completions` | yes | OpenAI chat completions; `model` is a registry name |
| `GET /v1/models` | yes | the downloaded registry entries, OpenAI list shape |
| `GET /health` | no | `{status, memory: {budgetTotalMb, reservedMb, footprintMb, loadedModels}}`, numbers only |
| `GET /api/status` | yes | `memory` (budgetTotalMb, reservedMb, footprintMb, loadedModels, startingModels, stoppingModels, inFlightTotal, waitingTotal, evictions, restarts, abandoned), `ledgerRows`, `refusals`, `version`, `startedAt`, `uptimeSeconds`, `port`, `inFlightTotal`, `waitingTotal`, `registryPath`, `defaults`, `storeFreeGb` |
| `GET /api/models`, `GET /api/models/<name>` | yes | per model: `state` (registered, downloaded, starting, loaded, unloading) and `phase` (the same, or `stopping`), budget, footprint, last use, `idleSecondsLeft`, pin and `pinnedBy`, TTL, ready timeout, in flight, waiting, `server`, `capabilities`, `licence`, `weights` (source, repo, revision, `~`-relative path, verifiedAt, reused, sha256Pinned, filesVerified, approxSizeGb, download, alternative), `commercialUse`, `lastError`, pull progress |
| `POST /api/models/<name>/load` · `/unload` | yes | start now (waits until ready; never takes a request slot, so it answers at once for a busy loaded model) · stop (after its requests finish) |
| `POST /api/models/<name>/pin` | yes | `{"pinned": true\|false}`; kept in `pins.json` |
| `POST /api/models/<name>/available` | yes | `{"available": true\|false}`; kept in `availability.json`; answers `{name, available, availableBy, state?, note?}` |
| `POST /api/models/<name>/pull` · `GET /api/pulls` | yes | verify or download into the store (202), then poll progress: `phase` (searching, verifying, downloading, done, failed, cancelled), `bytesDone`, `bytesTotal`, `currentFile`, `ok`, `message` |
| `POST /api/models/<name>/pull/cancel` | yes | stop a running pull (409 when none runs); the phase reads `cancelling` until the download process has exited, then `cancelled`; a new pull of that model is refused (409) meanwhile |
| `GET /api/requests?limit=N&model=&outcome=&before=` | yes | ledger rows, newest first, in-flight ones included (`outcome=in_flight`, with `phase` and `elapsed_ms`); `next` is the cursor for `before` |
| `GET /api/logs?source=gateway\|<model>&lines=N` or `&since=B` | yes | the tail of a log file, or what was written since byte offset `B`; `next` is the offset to pass next time (`rotated: true` when the file started over) |
| `POST /api/shutdown` | yes | stop every worker, then the gateway |

**Address.** Call `http://127.0.0.1:18900`, not `localhost`: the gateway listens on IPv4 only, and
`localhost` may resolve to `::1` first. The `Host` header must name `127.0.0.1`, `localhost` or `[::1]`
(403 otherwise, against DNS rebinding), and no `Origin` header may be present (browsers get 403). No CORS
header is ever sent, and a worker's are stripped. A client socket that sends nothing for 30 s (headers,
body) is closed.

**Chat requests.** A chat body passes Draftbook's gate before any model loads; anything else is a 400:
- fields: `model`, `messages`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `top_k`,
  `min_p`, `seed`, `stop`, `stream`, `stream_options`, `response_format`, `repetition_penalty`,
  `presence_penalty`, `frequency_penalty` (so no `tools`, `logprobs`, `n`, `user` or other field);
- messages: `role` `system`, `user` or `assistant`, with `content` and optionally `name` only;
- content: a string, or parts of `{"type": "text", "text"}` and
  `{"type": "image_url", "image_url": {"url": "data:image/(png|jpeg|webp);base64,…", "detail"?}}`; no URL is
  ever fetched, no file read; image bytes must be PNG, JPEG or WebP;
- every image's size is read from its header (PNG, JPEG, WebP) and over 50 million pixels is refused, for
  every model, before it loads; an unreadable header is refused too;
- a Content-Length of at most 25 MB, no Transfer-Encoding; management bodies at most 64 KB; numbers in
  headers and queries are ASCII digits, at most 10 of them.

The body is then forwarded to the worker byte for byte and the answer relayed as it comes. **Streaming**
(`stream: true`, chunked server-sent events) works for the mlx-vlm models (`minicpm-ocr`, `qwen3.5-4b`,
`qwen3.5-27b`); `nemotron-parse` and `jev` always answer one plain JSON completion. Errors use the OpenAI
envelope `{"error": {message, type, code}}`.

| Status | When |
|---|---|
| 400 / 413 | the body fails the gate, a number or URL cannot be parsed / the body is over 25 MB |
| 401 / 403 | no or wrong key / a non-loopback Host, an Origin header, a model whose served weights source is not `allowed` for use in the registry, or a model turned off (`error.code` `model_not_available`) |
| 404 | `model` is not in the registry (nothing is substituted) |
| 409 | registered but not downloaded, or its files changed since the pull: `tinybar-models pull <name>` |
| 499 (ledger only) | the client hung up |
| 502 | the worker failed, or broke off a plain answer (ledger `partial`, `body_ended_early`); once a stream has started, a failure closes the connection without an error body (ledger `partial`, `stream_ended_early`) |
| 503 + Retry-After | no room in the memory budget (30, or 3 while a stopping worker still holds part of it), queue full (2), unloading, a worker that failed to start or the gateway stopping (5); a worker's own over-budget 503 passes through with its `Retry-After: 30` |
| 504 | the worker did not answer within an hour (the worker is stopped) |

**Cancellation.** When the client goes away (or the worker times out) after the request reached the
worker, that worker may still be generating for nobody; the gateway stops it at once rather than counting
it idle, and the next request loads it afresh (2 to 3 s for these models). A client that leaves while its
request is still queued or loading just leaves the queue. Streams cut by the client end the same way.
**A client's timeout must be longer than the model's load plus its longest generation**: a request
abandoned by its client stops the worker, so a timeout shorter than that can never succeed (each retry
reloads and is cut again).

**What a client sees when an answer is cut.** A streamed answer (chunked server-sent events) that ends
without its terminal `data: [DONE]`, because the worker died, broke off or simply stopped, is relayed up to
that point and then the connection is closed **without the chunked terminator** (`0\r\n\r\n`): an HTTP client
reports an incomplete read (Python `http.client.IncompleteRead`, curl exit 18, fetch an aborted body), never
a clean end. The ledger records `partial` (`stream_ended_early`), and a worker that broke off is stopped. A
plain answer cut short is a 502 with an error envelope (`partial`, `body_ended_early`) and the worker is
stopped. A complete answer is `ok` only after the whole body, or the `[DONE]` event, was relayed.

**Ledger.** Each authenticated request gets an id, returned as `X-Request-Id`; a caller that keeps traces
(Draftbook's call records) stores it to join on. A row holds a fixed route template (`POST
/v1/chat/completions`, `POST /api/models/{name}/load`, … or `other`), the model only when it is a registry
name, `X-Client-Name` (64 plain characters at most), queue, load and total ms, token counts when the worker
reports them (unknown stays null), the status, an outcome and an error kind; never content. Unauthenticated
or refused requests (401, Host, Origin) write nothing: they only increase counters in `/api/status`.
Retention: 30 days and 100,000 rows (rows still in flight are never pruned).

Every accepted request (chat, and management calls that change something) has its row **from acceptance**,
before its work is dispatched: outcome `in_flight`, `phase` `queued`, then `loading` while its model starts,
then `generating` once the worker has it. The row is completed in place when the request ends (`ended_at`
set, `phase` cleared). If the gateway dies meanwhile, the row survives: at the next start every row still
`in_flight` becomes `interrupted` with error kind `gateway_restart`, keeps its last phase, and its `ended_at`
stays null (unknown, never invented). One SQLite connection writes, in WAL mode with `synchronous=NORMAL`,
which survives a crash of the gateway (only a power loss can drop the last commits). Measured on this Mac:
the acceptance row and its phase updates add a median 0.032 ms to a request (p99 0.081 ms, max 0.9 ms over
2,000 requests).

| Outcome | Meaning |
|---|---|
| `in_flight` | accepted, not finished; see `phase` |
| `ok` | answered in full |
| `partial` | an answer cut short (`stream_ended_early`, `body_ended_early`) |
| `refused` | not taken (gate, unknown model, no room, queue full, licence, …) |
| `not_available` | the model is turned off (`model_not_available`) |
| `cancelled` | the client left (499) |
| `error` | the worker failed or timed out |
| `interrupted` | the gateway stopped (crash, kill) while it was in flight (`gateway_restart`) |

### Calling it from another repository

```sh
curl http://127.0.0.1:18900/v1/chat/completions \
  -H "Authorization: Bearer $TINYBAR_MODELS_API_KEY" -H "Content-Type: application/json" \
  -H "X-Client-Name: my-script" \
  -d '{"model": "qwen3.5-4b", "max_tokens": 64, "messages": [{"role": "user", "content": "Say hello."}]}'
```

```ts
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText } from 'ai';

const local = createOpenAICompatible({
  name: 'local',
  baseURL: 'http://127.0.0.1:18900/v1',
  apiKey: process.env.TINYBAR_MODELS_API_KEY,   // or read the 0600 key file
  supportsStructuredOutputs: true,               // false for nemotron-parse and jev
});
const { text } = await generateText({ model: local('qwen3.5-4b'), prompt: 'Say hello.' });
```

Request shapes per model are those of Draftbook's servers and stay in each worker's docstring:
`nemotron-parse` takes one page image and answers `{text, blocks: [{class, box, text}]}`; `jev` takes one
page image and a text part `{"blocks": [{block, text, box}]}` and answers `{"scores": [{block, score}]}`
(unshapeable blocks in `X-Unshaped-Blocks`); the mlx-vlm models take text and images and honour
`response_format` `json_schema`.

## Availability: models on and off

The owner's rule (17 November): the gateway starts at login but models cost no memory until they are used,
and each model can be made available to the API, or not, by an API call or from the UI.

- `POST /api/models/<name>/available` with `{"available": false}` turns a model off: from then on a chat
  request or `load` naming it gets **403** with
  `{"error": {"message": "model '<name>' is turned off on this Mac, so it is not loaded or served. Turn it on
  with POST /api/models/<name>/available {\"available\": true}, or: tinybar-models enable <name>",
  "type": "permission_error", "code": "model_not_available"}}`. It is never loaded on demand. If it is loaded,
  it is unloaded once its requests in flight finish (requests queued for it get the 403; its memory budget is
  released when its process has exited). A pinned model turned off is unloaded too: off wins, and the answer
  says so in `note`.
- `{"available": true}` turns it back on: exactly the usual behaviour (load on first request, idle unload,
  eviction, pinning). Turning a model on loads nothing by itself.
- The choice is kept in `availability.json` in the state folder (`availableBy: "user"`). Without one, the
  registry decides (`availableBy: "registry default"`): `availableByDefault` on the entry, else
  `defaults.availableByDefault`, which is `false`, so a newly added model serves nothing until someone turns
  it on. The five current entries say `true`, as Draftbook already calls them.
- `GET /api/models` lists every entry with `available`; `/v1/models` lists only models that are available
  and downloaded. CLI: `tinybar-models enable <name>`, `tinybar-models disable <name>`.

## Installation path

For now two layouts work, and you choose:

- **The checkout** (`backend/` in this repository, what `login install` uses). Simple for development; the
  LaunchAgent then runs `<checkout>/backend/.venv/bin/tinybar-models` and the registry is
  `<checkout>/backend/registry.json`. If the checkout moves or is deleted, the agent's program is gone: launchd
  fails to start it (see `logs/launchd.err`) until `login install` is run from the new place.
- **An installed copy** (`tinybar-models install`): the package, `registry.json`, `pyproject.toml`, `uv.lock`
  and `.python-version` are copied to `~/Library/Application Support/TinybarLocal/backend/<version>-<hash>`,
  its own environment is built there with `uv sync --locked --no-dev --offline` (from uv's cache, no network),
  and the LaunchAgent is pointed at that copy's `.venv/bin/tinybar-models`. Moving the checkout breaks
  nothing. Changes in the checkout (code or registry) take effect only at the next `install`, which builds a
  new folder (the hash covers every copied file) and switches the agent; an unchanged checkout reuses its
  folder. A failed build leaves no folder. `tinybar-models uninstall` removes the login item if it runs an
  installed copy and deletes the copies; the state folder and the weights are never touched.

On 17 November the agent on this Mac runs the checkout; `install` was built and tested (and its offline build
checked) but not switched to, so that fixes in the checkout go live with `stop` and `start`.

## Start at login

`tinybar-models login install` writes `~/Library/LaunchAgents/ai.naik.tinybar-models.plist` (mode 0644) and
loads it with `launchctl bootstrap gui/$UID`; no sudo, nothing system-wide. The agent runs this backend's own
`backend/.venv/bin/tinybar-models serve` by absolute path (not `uv run`, so launchd's signals reach the
gateway), with `RunAtLoad`, `KeepAlive {SuccessfulExit: false}`, `ProcessType Background`, `ExitTimeOut 20`,
stdout and stderr in the state folder's `logs/launchd.out|err`, and an environment of `PATH` and
`TINYBAR_STARTED_BY=launchd` only (no key: the gateway reads its 0600 key file). `install` checks that the
binary is inside this backend folder, owned by the user and not writable by group or others, and refuses
while a gateway launchd did not start is running. `login remove` boots it out (a clean stop) and deletes the
plist; `login status` says whether it is installed and loaded, and its pid. All three are idempotent.

- A crash or kill (non-zero exit, or death by a signal) is restarted by launchd.
- A clean stop (`tinybar-models stop`, `POST /api/shutdown`, SIGTERM) exits 0 and stays stopped. Start it
  again with `tinybar-models start` or `launchctl kickstart gui/$UID/ai.naik.tinybar-models`.
- A start refused because the state folder's lock or the port is taken exits 1 by hand, but 0 under launchd,
  so launchd does not retry it every 10 seconds (the reason is in `logs/launchd.err`).
- At login the gateway loads no model: at rest it takes about 17 MB and no CPU.

## Registry

[`backend/registry.json`](../backend/registry.json), validated by `registry.py` at start (a broken entry
stops the gateway). A model not listed cannot be served or pulled. Per entry:

| Field | Meaning |
|---|---|
| `name`, `displayName`, `work` | the name a request uses; work kinds `read-page`, `check-reading`, `extract-fields`, `chat` |
| `weights.original`, `weights.mlx` | `repo` and pinned 40-character `revision`; optional `sha256` per file (filled from the 17 Nov verification) |
| `weights.reuse`, `weights.download` | which sources an existing copy may be, and which one a pull downloads |
| `weights.external` | a folder registered where it lies (see the store) |
| `server.kind`, `server.maxTokens` | `mlx-vlm`, `nemotron-wrapper` or `jev-wrapper`; no port: each worker binds its own and reports it |
| `memory` | `budgetMb`, `cacheLimitMb`, `clearCacheAfterRequest` (Draftbook's S-06a figures) |
| `ttlSeconds`, `concurrency`, `queueLimit`, `readyTimeoutSeconds`, `pinned`, `availableByDefault` | optional; defaults in `defaults` (600 s, 1, 8, 600 s, false; `availableByDefault` false) |
| `capabilities`, `licence` | as Draftbook's catalogue; each source's `commercialUse` is `allowed`, `not_allowed` or `unread`, and only an `allowed` source is served |

`memoryBudgetTotalMb` (49,152 MB) bounds all loaded workers together. `TINYBAR_MODELS_REGISTRY` points the
gateway (and its workers) at another file, as the eviction proof did.

## The weights store

`~/Models` (`TINYBAR_MODELS_HOME`): `hf/` is `HF_HOME`, its `hub/` the Hugging Face cache layout; `gguf/`
and `other/` are for other tools. On 17 November the whole of `~/.cache/huggingface` (26 GB, 423 entries)
was renamed into `~/Models/hf` on the same volume and `~/.cache/huggingface` became a link to it, so every
existing tool, and Draftbook's `models:status` (its weights links point through that path), still resolves.
Nothing was deleted. GGUF files were not moved.

**Nemotron-3.5-Parse is in the store** (owner's word, 17 November): the OCR lab's folder was renamed, on the
same volume, to `~/Models/hf/hub/models--nvidia--Nemotron-3.5-Parse/snapshots/62cb1e4ba153d113ff64aaf2be6e068c377cf53c`
(2.36 GB, 252 entries; its files keep their hard links into the lab's other folders), and
`~/src/ocr-review-lab/.cache/models/Nemotron-3.5-Parse` is now a symlink to it, so the lab and Draftbook's
`~/.cache/draftbook-models/weights/nemotron-parse` still resolve. The snapshot holds plain files rather than
links into `blobs/`; the store finds it like any other pinned revision, so the entry needs no
`weights.external`. A pull verified it in place with no download. It is served only from its original
weights (`reuse` and `download` both `original`); the `mlx` source is listed for reference only.

**The second Nemotron copy was deleted on the owner's word** (17 November): the MLX conversion
`geoHeil/Nemotron-3.5-Parse-MLX-bf16`, `~/Models/hf/hub/models--geoHeil--Nemotron-3.5-Parse-MLX-bf16` and its
`.locks` entry (9 MB freed). Its 1.26 GB weights file lives in the hub's shared blob folder
(`~/Models/hf/hub/blobs/39/39e2dddeba7d…`), referenced by nothing else now; it was left for the owner's
explicit word, being outside the one folder named for deletion.

**What the Nemotron worker runs from its weights folder.** Traced on 17 November: no module is imported from
the folder itself. Transformers (`trust_remote_code`) copies `hf_nemotron_parse_config.py` from it into
`HF_MODULES_CACHE` (the worker's run folder) and executes that copy, byte-identical to the original. That
file is part of the pinned revision: a pull checks its blob hash and records it, every request checks its
size, and every worker launch re-hashes it with all the other small (non-LFS) files of the record (config,
tokenizer, processor, code); a mismatch refuses the launch ("changed since it was pulled; pull it again").
The large weights are hashed at pull and size-checked on every request. Workers run with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPYCACHEPREFIX` in the
run folder, so no bytecode is written into, or read from, the weights folder (the lab's own
`nemotron_parse/__pycache__` is never used).

An entry is *downloaded* when a verification record `weights/<name>.json` exists in the state folder, names
the revision the registry pins, its folder still holds `config.json`, and every recorded file still has its
recorded size (checked on each request). A pull writes that record only after the folder matches the pinned
revision file by file: every top-level file present with the revision's hash (SHA256 for weights; the blob
hash for small files), nothing extra (the downloader's `.cache/huggingface`, Python `__pycache__` folders,
and Finder's `.DS_Store` and `._*` files aside), and every registry `sha256` holding. A pull first looks for
a copy (the store, `weights.external`, `--from` folders), and downloads only when none matches and 25 GB
stays free afterwards. An entry keeps the source it was first verified from: a pull that would switch it to
another source is refused. A failed or partial pull leaves no record. A download runs in its own child
process (`python -m tinybar_models.fetch`), so cancelling a pull terminates it for certain: the pull reads
`cancelling` until that process has exited and only then `cancelled`. Files it had half fetched stay in the
hub cache as `.incomplete` blobs, which the next pull of that model resumes; nothing is marked downloaded
until the folder is verified. Workers run offline with an empty hub
cache.

To add a model: add its entry (pinned revision, licence read and recorded, budget from a measurement), then
`tinybar-models pull <name>` (through the gateway when it runs, so progress shows in `/api/pulls`).

## Memory

Each worker sets MLX's memory and cache limits from its entry before the model loads, clears MLX's cache
after every request, and refuses work with 503 (`Retry-After: 30`) while its physical footprint (libproc
`phys_footprint`, as Activity Monitor shows, Metal buffers included) stays over its budget or cannot be
read. The gateway holds the total:

- a starting, loaded, unloading or stopping worker holds its `budgetMb`; a stopping one holds it until its
  process has exited. Before a start, least-recently-used workers that are loaded, unpinned and idle
  (nothing in flight or waiting) are stopped until the new budget fits, else 503;
- a worker with a request in flight or waiting is never stopped to make room;
- an unpinned idle worker stops after its TTL. A supervision pass every 15 s reads each idle worker's
  footprint (libproc, no request to the worker); once it passes 90% of the budget, or cannot be read, it
  asks the worker's `/health`, and restarts the worker when the status is anything but `ok`
  (`over_budget`, `footprint_unknown`), when two probes in a row fail or time out, or when the footprint is
  over budget. The replacement loads in its own thread. The pass also notices crashed workers;
- one worker process per model, so stopping it gives its memory back.

Logs: the gateway's own log and each worker's output are files in `logs/`, rotated at 5 MB while running
(one previous file kept). mlx-vlm's per-request access log is off. If a worker's log cannot be written, its
output is read and dropped, so the worker never blocks.

Measured on this Mac (M4 Max, 128 GB) on 17 November, through the gateway, on Draftbook's invented page.
First run (before the fix round):

| Model | Load, s | Request, s | Footprint after the call, MB | Budget, MB | Answer (counts only) |
|---|---|---|---|---|---|
| nemotron-parse | 1.0 | 2.6 | 2,808 | 6,144 | 11 blocks, 421 tokens |
| minicpm-ocr | 1.8 | 4.5 | 3,244 | 6,144 | 284 tokens |
| jev | 1.5 | 3.3 | 5,640 | 8,192 | 11 blocks scored, median 0.98 |
| qwen3.5-4b | 1.5 | 5.6 | 3,851 | 6,144 | 274 tokens |
| qwen3.5-27b | 2.1 | 8.7 | 16,339 | 22,528 | 10 tokens (short answer) |

Rerun after the fix round (workers on port 0 with the pipes): nemotron-parse 1.8 s load, 2.3 s request,
2,814 MB; minicpm-ocr 2.9 / 2.6 s, 3,046 MB; jev 3.4 / 1.5 s, 5,326 MB (11 blocks, median 0.98);
qwen3.5-4b 2.6 / 3.9 s, 3,864 MB. All answered 200 with the same counts as the first run.

Load is until the worker answers; MLX maps weights lazily and the files were in the OS cache, so part of a
cold load lands in the first request. With all five loaded the budgets held 49,152 of 49,152 MB and the
real footprint was 30,355 MB.

- **Idle unload**: qwen3.5-27b (TTL 300 s) stopped after 302 s idle; its 16,088 MB worker exited, the
  gateway's footprint went from 30,104 to 13,991 MB and macOS free memory from 6,730 to 23,082 MB.
- **Eviction** (a registry copy with the total set to 12,288 MB): jev loaded (8,192 MB held), then a
  qwen3.5-4b request stopped jev (least recently used, idle; its process gone), loaded in 1.6 s and answered.
  With qwen3.5-4b pinned, a jev request got 503 and `Retry-After: 30` naming the pinned worker.
- **Soak**: 20 jev requests, median 1.47 s each, footprint between 5,653 and 5,827 MB (first 5,740, last
  5,788), no trend, under its 8,192 MB budget; no request refused.
- **Abandoned request** (after the fix round): a qwen3.5-4b request for a long story, its client cut at
  2.0 s: a 499 row, the worker stopped at once ("request abandoned by its client"), and the next request
  loaded a fresh worker (new pid, 2.8 s load) and answered 200.
- **Orphans** (after the fix round): with four workers loaded, `kill -9` of the gateway; all four workers
  had exited when checked 0.1 s later, and the port was free.
- **Streaming**: a streamed qwen3.5-4b answer came back chunked with its usage in the ledger.
- **Draftbook, unchanged** (first run): its own model entry point and AI SDK openai-compatible adapter,
  with two policy rows whose `baseUrl` was `http://127.0.0.1:18900/v1` and the gateway's key file as its
  local key file, read the invented page with nemotron-parse (11 blocks) and checked it with jev (4
  subjects, the changed one flagged at 0.18). No Draftbook file changed; its own servers on 18901–18905
  keep working beside this.

The proof scripts: `backend/proof/live_proof.py` (`calls`, `soak`, `unload`, `evict`) and
`backend/proof/draw_invented_page.py`; they print counts and timings only.

## CI

`.github/workflows/ci.yml` has a `backend` job beside the Swift job: on `macos-15` (MLX's wheels are
Apple-silicon only) it installs uv 0.13.4, runs `uv sync --locked` in `backend/`, then
`uv run --locked --offline pytest -q -rf --maxfail=5`, within a 15-minute job limit. Each test has a 60 s
timeout (pytest-timeout, in the dev group) that dumps every thread's stack, and a fake worker that has not
reported its port within 8 s dumps its own stack into its log, which the gateway logs on a failed start. The
suite loads no model and uses no network: `tests/conftest.py` refuses any connection that is not to this
machine, and a test checks that it does. Nothing is skipped on the runner: the LaunchAgent tests fake
`launchctl`, and the footprint test reads only its own process.

The first CI runs hung because `http.server.HTTPServer.server_bind` calls `socket.getfqdn(host)`, a reverse
DNS lookup that stalled on the runner, so no worker reported its port. Every server here (the gateway, the
wrapper workers, the test worker) now uses `wire.LoopbackHTTPServer`, which skips that lookup; an offline Mac
could have stalled the same way.

## Not built

- The Models page in Tinybar (list, pull, load, unload, test, logs, usage, memory): next, by the design
  agents, from this API.
- The menu-bar app supervising the gateway (launchd does that now).
- Traces: the caller keeps them and joins on `X-Request-Id`; no OpenTelemetry or Langfuse export here.
- Cancelling generation inside a worker without stopping it: an abandoned request costs a reload.
- GGUF models; Kokoro or other non-chat runtimes.
- `/v1/models` lists only downloaded entries and no capabilities beyond the OpenAI shape.

## Licence note

oMLX (Apache-2.0) was read, by the Draftbook session on 17 November (commit `eec2544`), as prior art for one
address, load on demand, idle TTL, least-recently-used eviction and pinning. **No oMLX code was copied**:
those ideas were reimplemented here from that summary
([handoff › oMLX](handoff-local-model-platform.md#omlx-from-a-source-read-commit-eec2544-v080rc1-nothing-was-installed-or-run)),
and this backend's code is Draftbook's (same owner) or new. mlx-vlm is used as a pinned dependency, not
vendored.
