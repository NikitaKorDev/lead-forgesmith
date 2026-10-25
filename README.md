# Forgesmith

Asynchronous B2B lead pipeline: discover e-commerce merchants, enrich them with
contact data and business context, generate a personalized Polish pitch, and
send it at a human pace.

Status: **Phases 0–5 implemented** — scaffold, Shopify/OLX extraction,
storefront enrichment (domain resolution, contact emails, business context),
grounded Polish pitch generation, an approval-gated SMTP mailer with dry-run
`.eml` output, read-only IMAP ingest for bounces and replies (Phase 4.5),
one-shot Docker packaging driven by Windows Task Scheduler, and a PyQt5 desktop
console over the same CLI. See [Roadmap](#roadmap).

## Quick start

```powershell
uv sync
uv run playwright install chromium      # OLX + enrichment browser fallback
uv run forgesmith db init
uv run forgesmith source shopify        # probe data/shopify_seeds.txt (37 verified stores)
uv run forgesmith source olx -q meble --city warszawa --pages 1
uv run forgesmith enrich --limit 10
uv run forgesmith contacts
uv run forgesmith compose --limit 5         # requires sender fields in .env
uv run forgesmith drafts
uv run forgesmith approve --all-composed    # composed -> queued (review first)
uv run forgesmith send                      # dry run: data/outbox/*.eml
uv run forgesmith send --live --yes --limit 1   # real SMTP, allowlist first
uv run forgesmith sends
uv run forgesmith leads
uv run forgesmith stats
uv run forgesmith status                     # counts, window, kill switch, last runs
uv run forgesmith doctor                     # pre-flight checks (exit 1 fail, 3 warn)
```

For the unattended/container deployment, see
[Containers and scheduling](#containers-and-scheduling) and
[`ops/README.md`](ops/README.md).

## Desktop console (PyQt5)

The CLI has a companion desktop app that exposes every stage, ledger and status
in one window. It is a thin front end: each button builds the same argv you would
type and runs it as a one-shot `python -m forgesmith` subprocess, so approvals,
the kill switch, dry-run gating, locks and the run ledger behave exactly as they
do on the command line. The GUI never re-implements pipeline logic.

```powershell
uv sync --extra gui          # PyQt5 is an optional extra; the pipeline stays headless
uv run forgesmith-gui        # or: uv run python -m forgesmith.gui
```

| Page | What it does |
|---|---|
| Dashboard | `status` + `doctor` cards, doctor checks, run ledger, 30 s auto-refresh |
| Prospects | `leads` and `contacts` tables; jump to enrich/compose/approve/mark for a row |
| Drafts | `drafts` with the full body beside the table; approve, unapprove, suppress, copy |
| Sources | `source shopify`, `source olx` |
| Enrichment | `enrich` (including `--resolve-only` and `--deep-resolve`) |
| Compose | `compose` with a link straight to review |
| Mailer | `send` (dry run or `--live`), live policy line from `status` |
| Ledger | `sends` + `outbox`, open an `.eml`, resolve an uncertain attempt |
| Inbox | `inbox sync`, `inbox list`, `inbox show`, `inbox apply` |
| Operations | `db init`, `doctor`, `backup`, `mark`, `suppress`, `unsuppress`, `stats`, `version` |

Notes that matter in practice:

- **Confirmation is the modal.** A subprocess has no TTY, so the app appends
  `--yes` for bulk commands; the warning dialog in front of you *is* the human
  approval. `send --live` additionally shows a red LIVE banner and its own
  dialog, and still needs `FORGESMITH_MAIL_DRY_RUN=false`.
- **One stage at a time.** The runner owns a single process; **Stop** kills it
  (SIGTERM/Kill) and the CLI's own stop handling still runs.
- **`--json` is the contract.** Read-only commands accept `--json`; the GUI
  renders those payloads and buffers the raw JSON out of the console. stderr
  still streams, so warnings and tracebacks stay visible.
- **Settings come from the repo root.** `.env` is read from the project
  directory, not from wherever you launched the app, and stages run with that
  directory as their working directory.
- Tables sort by real values (ids sort `9 < 51`), and every page refreshes on
  show once the runner is free.

## Commands

| Command | What it does |
|---|---|
| `forgesmith db init` | Create tables (idempotent) |
| `forgesmith source shopify [--seeds FILE ...] [--limit N]` | Probe Shopify storefronts, store catalogue metadata |
| `forgesmith source olx -q meble --city warszawa --pages 2 [--limit N] [--engine http]` | Crawl OLX listing pages into business leads |
| `forgesmith enrich [--limit N] [--source shopify\|olx] [--lead-id N] [--force]` | Resolve storefronts, extract role-first emails and page context |
| `forgesmith enrich --resolve-only [--source olx] [--deep-resolve]` | Write candidate domains only; no storefront crawl |
| `forgesmith contacts [--lead-id N] [--limit 50]` | Inspect contacts Phase 3 will use |
| `forgesmith compose [--limit N] [--source shopify\|olx] [--lead-id N] [--regenerate] [--retry-failed]` | Generate grounded Polish drafts with local Ollama; never sends |
| `forgesmith drafts [--lead-id N] [--limit 20] [--message-id N]` | Review the latest draft per lead |
| `forgesmith suppress --email ADDRESS\|--domain DOMAIN [--reason TEXT]` | Record an opt-out |
| `forgesmith approve --lead-id N [--message-id M]` | Approve the latest draft: `composed -> queued` |
| `forgesmith approve --all-composed [--limit N] [--yes]` | Bulk approval with a preview |
| `forgesmith unapprove --lead-id N` | Retract approval when no live send exists |
| `forgesmith send [--limit N] [--lead-id N] [--source X] [--live] [--yes] [--no-delay] [--force-window] [--max-runtime S]` | Dry-run `.eml` by default; `--live` sends |
| `forgesmith sends [--status ...] [--lead-id N] [--limit 50]` | Attempt ledger (sent/failed/bounced/uncertain/aborted) |
| `forgesmith outbox [--limit 20]` | Rendered `.eml` evidence files |
| `forgesmith inbox sync [--dry-run] [--limit N]` | Read new mail over read-only IMAP; apply hard bounces and thread-matched replies |
| `forgesmith inbox list [--status ...] [--limit 50]` | Inspect the inbound ledger |
| `forgesmith inbox show --id N` | Explain one inbound message and its proposed action |
| `forgesmith inbox apply --id N [--yes]\|--all [--yes]` | Apply a stored outcome; opt-outs require `--yes` |
| `forgesmith status [--json]` | Pipeline counts, window state, kill switch, last run per stage |
| `forgesmith doctor [--json] [--smtp-check]` | Pre-flight environment check; exit 1 fail, 3 warn |
| `forgesmith backup [--out DIR] [--keep N] [--outbox/--no-outbox]` | Consistent SQLite `VACUUM INTO` snapshot (+ outbox archive) |
| `forgesmith mark --lead-id N --status replied\|bounced\|sent\|suppressed [--reason TEXT]` | Record a manual reply/bounce/opt-out |
| `forgesmith mark --send-id S --status sent\|failed --yes` | Resolve an uncertain/pending attempt |
| `forgesmith unsuppress --email ADDRESS\|--domain DOMAIN [--reason TEXT]` | Reverse a suppression with an audit event |
| `forgesmith leads [--status new] [--source shopify] [--limit 50]` | Inspect stored leads |
| `forgesmith stats` | Counts by source and status |
| `forgesmith --log-level debug <command>` | Verbose run |

## Architecture

```
data/shopify_seeds.txt ──► ShopifySource ──┐
OLX listing pages ───────► OlxSource ──────┴─► LeadCandidate ──► LeadRepo
                                                                  │
                                                        (Phase 2) enrichment
                                                                  │
                                                    Lead + Contact + evidence
                                                                  │
                                              (Phase 3) Ollama pitch generation
                                                                  │
                                                              Message
                                                                  │
                                                 (Phase 4) approve → dry-run .eml
                                                       → SMTP with caps/jitter
                                                        → Send / Suppression
```

### Why two stages

Marketplaces (OLX, Allegro) are a *lead source*, not a *contact source*: sellers
hide their email behind a contact form, and the offer page is not an outbound
target. So a lead is an identity (`name`, `city`, `category`, `domain`), and a
separate enrichment stage visits the merchant's own storefront to find a company
email and the product context the pitch needs.

### Source notes (measured, not assumed)

**Shopify** — `/products.json` returns the real catalogue, so the lead carries
product titles, types and prices for personalization. The `name` comes from
`og:site_name`/`<title>` (product `vendor` is usually the goods brand, not the
shop). `data/shopify_seeds.txt` ships 37 stores verified live; a dead or
non-Shopify domain is skipped silently, so appending candidates is safe.

**OLX** — the listing page embeds `window.__PRERENDERED_STATE__`, a
double-encoded JSON string with full ad objects. It sits behind a CloudFront WAF
that rejects plain HTTP clients (403 at any User-Agent), so the source drives
headless Chromium through `forgesmith.browser.BrowserFetcher`. Only
`isBusiness` ads are kept, and ads are keyed by **seller id**, not ad id, so a
shop posting twenty near-identical listings yields one lead. Most business ads
expose no website (`externalUrl` is null), so OLX leads land with no domain and
are resolved during enrichment.

**Allegro** — deliberately disabled: listings are behind DataDome and the ToS
forbids scraping. The source interface exists so it can be enabled later through
the official API.

### Enrichment

Enrichment is sequential by design: per-host pacing already serializes requests,
and one lead at a time keeps the SQLite write path and Ctrl-C behaviour simple.
Each lead gets one row in `enrichments` and one terminal status (`ok`,
`no_site`, `no_contact`, `blocked`, or `error`); retries stay `new` until
`ENRICH_MAX_ATTEMPTS`.

1. **Resolve** — use `lead.domain` (OLX already maps `externalUrl` into it),
   then `data/domain_overrides.csv`, then the OLX shop subdomain page, then
   (only with `--deep-resolve`) the offer page. No search-engine guesses.
2. **Fetch** — HTTP first, with a 2 MB cap, manual redirect validation against
   SSRF, and `robots.txt` respected. A 403/challenge/JS shell escalates that
   host to Chromium for the rest of the run; both engines share one host pacer.
3. **Extract** — `HTMLParser` visible text (no scripts, styles, or hidden
   honeypots), `mailto:` links, and JSON-LD. Eligibility is a gate; ranking is
   role-first (`kontakt`, `sales`, `biuro`, …), capped at 3, with one personal
   fallback and free-provider role addresses kept.
4. **Persist** — contacts, NIP/legal name when evidenced, platform, a 3000-char
   main-content excerpt, headings, a product sample, and page provenance.
   Raw HTML is never stored. A resolved domain is promoted onto the lead only
   after the homepage passes the parked/empty check.

Run `forgesmith enrich --source olx --limit 14 --resolve-only` before a full
crawl to review the OLX domains; that command writes only `enrichments.domain`
and leaves lead status untouched. Rerunning a completed lead is a no-op;
`--force` retries it but never downgrades an already-enriched lead.

### Pitch generation

`forgesmith compose` turns one enriched lead into one reviewable draft. It is
sequential, uses local Ollama by default, and never sends. The pipeline is:

1. **Select** leads that are `enriched` with an `ok` enrichment and a role
   contact. Suppressed emails/domains are excluded.
2. **Clean** the stored site context: consent, shipping/language, cart,
   accessibility, nav and price/size chrome are removed before hashing or
   prompting. `enrichments.evidence` and `context.pages` are never sent.
3. **Generate** JSON (`subject`, `body`, `evidence`) with a strict system prompt.
   Scraped text is delimited as untrusted data.
4. **Validate** hard gates (formal plain text, 80–140 words, no HTML/Markdown/
   emoji/URLs/placeholders/model signature, grounded numbers and verbatim
   evidence) and retry once with the error list on failure.
5. **Persist** the immutable draft plus a deterministic sender/opt-out footer,
   set `lead.status = composed`, and record a `composed` event. Failures only
   record `compose_failed`; they never downgrade a draft.

`messages` are immutable. The **latest draft is `max(messages.id)` per lead**;
reruns are no-ops unless `--regenerate` is passed. Failed attempts live in
`events` and `select_composable` orders by `(failed_attempts, lead.id)`, so one
poison lead cannot starve the queue.

#### Status contract

| Status | Meaning |
|---|---|
| `enriched` | Site + contact data ready; no draft yet. |
| `composed` | A draft exists and awaits human review. **Not approved for sending.** |
| `queued` | Operator approved the latest draft; Phase 4 may send it. |
| `sent` / `replied` / `bounced` / `suppressed` | Phase 4 lifecycle. |

Phase 3 only writes `enriched -> composed`. Phase 4 owns
`composed -> queued` (approval) and `queued -> composed` (retraction), and
re-checks immediately before every send:

- `lead.status == queued`
- the latest message is still the approved one
- the contact still exists
- the email and domain are still unsuppressed
- `hooks.footer_version` and `prompt_version` are still acceptable

`messages.body` already contains the footer; Phase 4 must not append it again.

### Delivery

`forgesmith send` renders `.eml` files by default and sends only with `--live`.
Both paths re-use the exact latest approved message and never append the footer
again. A live run requires valid sender config, a successful SMTP preflight,
an allowlist match when one is configured, and confirmation: `--yes` is
mandatory when more than one message is selected or stdin is not a TTY.

1. **Select** `queued` leads whose latest message carries an `approved` event,
   ordered by `(attempts, lead.id)` so a poison lead cannot starve the queue.
2. **Preflight** the SMTP connection/TLS/login and render the MIME bytes before
   any ledger row exists. Failures here write nothing and exit 1.
3. **Reserve** in one transaction: re-check status, approval, contact identity,
   suppression, footer version, attempt/backoff state, existing attempts,
   per-domain and per-recipient uniqueness, allowlist, window and daily cap.
   A `pending` row plus a `send_attempt` event commit *before* any SMTP verb.
4. **Archive** immutable `data/outbox/lead{id}-msg{id}-s{send_id}.eml` evidence,
   then **send** with explicit `mail`/`rcpt`/`data` stages (never `sendmail`).
   An unwritable archive aborts the attempt before any SMTP verb.
5. **Finalize** as `sent` (250 after DATA), `bounced` (5.1.x at RCPT, which also
   suppresses the address), `failed` (other 4xx/5xx or pre-DATA loss),
   `uncertain` (post-DATA loss or an unclassifiable transport exception — never
   auto-retried) or `aborted` (connection lost at MAIL FROM or archive failure;
   no attempt consumed).

Rules enforced in code:

- Polish text is `quoted-printable`; the subject uses RFC 2047; no Bcc.
- Gaussian jitter (`mean`/`sigma`, clamped) between live sends; `--no-delay` skips it.
- Per-message exponential backoff after `failed`; `mail_max_attempts` caps retries.
- Window is weekdays 09:00–17:00 `Europe/Warsaw` by default; the bound is
  converted to UTC before SQL. `--force-window` ignores the window, never the cap.
- Daily cap counts `sent` + `uncertain` for the current Warsaw day.
- Same registrable domain and same recipient can receive only one live attempt,
  ever (freemail domains are excluded from the domain rule).
- One live process at a time via `data/mailer.lock`; at startup every leftover
  `pending` row is recovered to `uncertain`, which is loud and excluded from
  selection until `mark --send-id` resolves it.
- A partial unique index (`uq_sends_message_live`) plus a reservation
  transaction make "one live attempt per message" true across processes; a race
  that loses the index maps to `existing_attempt` instead of double-sending.
- `sent` means "accepted by the relay", not "delivered to the inbox".

#### Kill switch, budget and shutdown

- `FORGESMITH_MAIL_DRY_RUN=true` is a real kill switch, not just a default. A
  requested live run sends nothing, writes no `.eml`, and exits 0 with
  `stop_reason=kill_switch`. Dry runs keep working while it is armed.
- `--max-runtime S` (or `FORGESMITH_SEND_MAX_RUNTIME`) caps one live tick;
  the jitter sleep is clamped to the remaining budget and the run stops
  cleanly between messages with `stop_reason=budget`.
- The window and daily cap are checked *before* the SMTP connection is opened,
  and again per message inside the authoritative reservation.
- `SIGTERM` is cooperative: it is observed only between messages, never inside
  `transport.deliver`, so a container stop cannot turn a send `uncertain`.
  A busy run lock exits 75 (skip and retry) rather than 1 (incident).

### Inbox ingest (Phase 4.5)

`forgesmith inbox sync` is the read-only mailbox reader. It uses `EXAMINE` and
`BODY.PEEK` exclusively: it never marks a message `\Seen`, moves it, or deletes
it, so a human can keep reading the same inbox. Only messages above the stored
UID cursor are fetched, and the first run is bounded by
`IMAP_LOOKBACK_DAYS`.

The parser is deliberately conservative. It classifies DSNs (`multipart/report;
report-type=delivery-status`), ARF/MDN reports, auto-replies, thread replies and
strong opt-out phrases in Polish/English. Only the *unquoted* part of a reply is
scanned, so quoting our own footer or an old message cannot trigger anything.

Effects are split by confidence:

- **Hard DSN (5.x, `Action: failed`) matched to a send** applies automatically:
  the lead becomes `bounced` and the exact `Final-Recipient` address is
  suppressed (`reason=dsn:5.1.x`). A `pending`/`uncertain` attempt is finalized
  as `bounced`. An already-accepted (`sent`) row is left untouched, because the
  relay did accept it and the daily-cap count must not shrink.
- **A reply matched by `In-Reply-To`/`References`** applies automatically:
  the lead becomes `replied` (never downgraded).
- **Everything else is review-only.** Soft/delayed DSNs, ARF/MDN, auto-replies
  and unmatched messages land as `ignored` or `needs_review`. An opt-out phrase
  never suppresses by itself: `inbox apply --id N --yes` records the
  confirmation and suppresses only the sender's email address. Domain
  suppressions are never created from inbound mail.

Each message is one transaction (ledger row + at most one transition + cursor
advance), and the two unique constraints make replay harmless. A separate
`data/inbox.lock` turns a concurrent sync into exit 75; correctness does not
depend on the lock. A UIDVALIDITY change resets the cursor and re-bootstraps from
the lookback window.

Manual ops remain available: `mark` records a reply/bounce/opt-out,
`unsuppress` reverses a suppression (including automatic ones), and
`sends`/`outbox` expose the audit trail.

### Lead model

`leads` — one row per prospect, unique on `(source, source_id)`, deduped by
`domain` (one outbound target per domain). `raw` keeps the evidence a source
collected (product sample, types, prices) for later personalization.

`enrichments` (1:1 with leads) holds resolution provenance, crawl status,
click-through context, NIP/legal name, and page evidence. `contacts` ·
`messages` · `sends` · `suppressions` · `events` complete the schema and are
created up front so the pipeline stages can be added without migrations churn.

## Database

Development and production both default to SQLite (`data/forgesmith.db`); there
is no database server to run. Connections enable WAL, `synchronous=NORMAL`,
`foreign_keys=ON` and a `busy_timeout`, so short concurrent stage containers
queue instead of failing with `database is locked`. Backups use SQLite's
`VACUUM INTO`, which is consistent while writers are active.

In containers the database and the mailer lock live on the named `fs-data`
volume (`/app/data`), not on a Windows bind mount: virtiofs bind mounts do not
provide reliable `flock()` for the single-writer guard. Only `outbox/` and
`backups/` are bind-mounted for operator access.

Postgres remains supported in code for branch hygiene and is available under
the optional `tools` profile, but it is **not** the Phase 5 production path:

```powershell
docker compose --profile tools up -d db
$env:FORGESMITH_DATABASE_URL = "postgresql+asyncpg://forgesmith:forgesmith@localhost:5432/forgesmith"
uv run forgesmith db init
```

Adminer is exposed on <http://localhost:8080> with the same profile.

## Configuration

Copy `.env.example` to `.env`. All keys are prefixed `FORGESMITH_`; the crawler
knobs that matter are `REQUEST_DELAY` (per-host seconds between requests,
default 1.5), `MAX_CONCURRENCY`, and `MAX_RETRIES`. Enrichment adds
`ENRICH_MAX_PAGES`, `ENRICH_LEAD_TIMEOUT`, `ENRICH_PAGE_TIMEOUT`,
`ENRICH_RESPECT_ROBOTS`, and `ENRICH_DOMAIN_OVERRIDES`. Phase 3 uses
`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT`, the `COMPOSE_*` limits, and
the required sender identity: `MAIL_FROM_NAME`, `MAIL_FROM_ADDRESS`,
`SENDER_COMPANY`, and `SENDER_OFFER` (optional `SENDER_PORTFOLIO_URL`). Phase 4
adds the `SMTP_*` transport keys, `MAIL_DAILY_CAP`, `MAIL_DELAY_*`,
`MAIL_BACKOFF_*`, `MAIL_WINDOW_*`, `MAIL_MAX_ATTEMPTS`, `MAIL_ALLOWLIST`, and
`MAIL_WEEKDAYS_ONLY`. Phase 5 adds `MAIL_DRY_RUN` (kill switch), `LOG_JSON`,
`BROWSER_NO_SANDBOX`, `APPROVAL_CHANNEL`, `SEND_MAX_RUNTIME`,
`DB_BUSY_TIMEOUT_MS`, and `STAGE_STALE_AFTER`. Phase 4.5 adds `IMAP_HOST`,
`IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_SSL` (465 implicit TLS) /
`IMAP_STARTTLS` (143 STARTTLS), `IMAP_TIMEOUT`, `IMAP_MAILBOX`,
`IMAP_LOOKBACK_DAYS`, `IMAP_FETCH_LIMIT`, and `IMAP_MAX_BODY_BYTES`. Plaintext
IMAP is refused when both TLS options are off. `tzdata` is a data-only
dependency required for `Europe/Warsaw` on Windows. Run `forgesmith db init`
after upgrading: it adds `sends.created_at`, four reporting indexes, and the
partial unique index in place, aborting with message ids if historical duplicate
live attempts would make that index impossible.

## Containers and scheduling

The container is one-shot, not a daemon: the host scheduler starts a stage,
the container exits, and the next tick is a fresh process. Every compose
service is behind a profile, so `docker compose up -d` starts nothing.

```powershell
docker compose --profile app build
docker compose --profile browser build
docker compose --profile app run --rm -T app forgesmith db init
docker compose --profile app run --rm -T app forgesmith doctor
docker compose --profile app run --rm -T app forgesmith status
```

Services:

| Service | Image target | Purpose |
|---|---|---|
| `app` | `slim` | source shopify, compose, approve, dry-run send, backup, reports |
| `app-live` | `slim` + `.env.live` | scheduled live send (`--profile live`) |
| `app-browser` | `browser` | OLX source and browser fallback enrichment |
| `ollama` | upstream | optional co-located model server (`--profile llm`) |
| `db`, `adminer` | upstream | optional Postgres tooling only (`--profile tools`) |

Live sending needs four independent gates: human approval to `queued`, the task
definition passing `--live --yes`, `FORGESMITH_MAIL_DRY_RUN=false` in
`.env.live`, and the `live` profile. `approve` is never scheduled and refuses
to run when `FORGESMITH_APPROVAL_CHANNEL=unattended`.

`ops/run.ps1` is the Task Scheduler wrapper (per-stage mutex, logs under
`state\`, exit 75 when busy, best-effort alerts). `ops/install-tasks.ps1`
imports the committed task definitions; the full timetable, kill-switch and
restore drill live in [`ops/README.md`](ops/README.md).

## Tests

```powershell
uv run pytest
```

Tests never touch the network: sources are exercised against HTML/JSON fixtures,
enrichment uses fake fetchers and `respx`, compose uses a fake LLM/respx and
real-corpus cleaning fixtures, the mailer uses `FakeTransport`/`FakeSMTP`, the
inbox uses `FakeInbox`/`FakeIMAP` with checked-in DSN/reply shapes, and the DB
runs on a temporary SQLite file. Deployment assets are checked structurally
(Dockerfile targets, profiled compose services, task XML, wrapper scripts).
Live Ollama generation, a live SMTP smoke and `docker build` are manual and
not part of the default gate.

The desktop console is covered without a display server: `tests/test_gui_commands.py`
checks argv building, confirmations and the JSON payload helpers, and walks the
Typer app so a renamed CLI flag cannot leave a dead button behind.
`tests/test_gui_smoke.py` builds the real window under `QT_QPA_PLATFORM=offscreen`
with the runner stubbed, and asserts every command is reachable and that JSON
runs stay out of the console. Both skip cleanly when the `gui` extra is absent.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, config, schema, CLI | done |
| 1 | Extraction: Shopify + OLX | done |
| 2 | Enrichment: domain resolution, contacts, context, platform detection | done |
| 3 | LLM: local Ollama, JSON-schema drafts, Polish prompt, grounding/validation, review CLI | done |
| 4 | Mailer: approval (`queued`), dry-run `.eml`, SMTP with caps/jitter/backoff, manual bounce/reply marking | done |
| 4.5 | IMAP ingest: DSN bounces, reply/opt-out detection, automatic suppression | done |
| 5 | Docker packaging, one-shot containers, SQLite on a named volume, Task Scheduler + ops runbook | done |
| 6 | Desktop console (PyQt5) over the same CLI: every stage, ledger and status in one window | done |

### Legal / deliverability notes

Outbound B2B email in Poland sits under RODO and the e-services act. The design
keeps to company addresses (not named individuals), always includes sender
identity plus a one-line opt-out, honours `suppressions` before every send, and
rate-limits per domain. Gmail app-password SMTP is a development crutch with low
caps and real suspension risk; the mailer defaults to a dry run that writes
`.eml` files to `data/outbox/`, and volume caps are enforced in code. Before a
real campaign, move to a dedicated domain with SPF/DKIM/DMARC.
