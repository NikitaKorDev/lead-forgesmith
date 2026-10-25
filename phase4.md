# Forgesmith — Phase 4: Mailer (final plan)

**Status:** implemented; offline suite and ruff gates pass. A live SMTP smoke is
still operator-supplied (see §15, M4.5).
**Goals source:** `AGENTS.md`, `README.md`, `phase2.md`, `phase3.md`
**Baseline:** Phases 0–3 implemented. 51 leads, 10 contacts, 0 messages, 0 sends, 0
suppressions, 10 events, 171 test functions. `sends`/`suppressions` tables exist but nothing
writes `sends`. No SMTP/IMAP code, no `approve`/`send` CLI.
**Debate record:** `C:\Users\Nikita\.opencode\plan\phase4-draft.md` (draft) plus two review
rounds; all load-bearing claims were verified against this repository (SQLite, Windows,
Python 3.12, SQLAlchemy 2.1).

---

## 1. Goal and definition of success

Turn a reviewed draft into one delivered B2B email, behind a human approval gate, without
double-sending, without mailing a suppressed address, and without exceeding caps — with an
offline default that only writes `.eml` files.

Success means **safe, auditable delivery**, not throughput:

- Nothing sends unless an operator approved that exact message.
- Dry run is the default; live mail needs `--live`, confirmation, and valid sender config.
- Every send re-runs the Phase 3 re-check list inside the reservation transaction.
- The SMTP boundary is crash-safe: a message is never blindly retried after a possible delivery.
- Suppression is re-checked per send; hard bounces and opt-outs self-suppress and are reversible.
- Randomized pacing, per-message exponential backoff, daily cap, sending window, allowlist,
  per-domain and per-recipient uniqueness.
- Every attempt is auditable in `sends` + `events`; `sent` means "accepted by the relay".
- Suite stays offline; JS-free; zero paid services.

**Out of scope:** HTML mail, attachments, tracking pixels, A/B tests, contact forms, warm-up/
deliverability tooling, DKIM signing, provider rotation, web UI, Phase 5 scheduling/Docker,
pre-send RCPT probing or paid verification, IMAP ingest (moved to Phase 4.5, §16).

---

## 2. Constraints verified from Phases 2–3

| Piece | Verified fact | Consequence |
|---|---|---|
| `messages` | Immutable; latest = `max(id)` per lead; `body` already has the footer; `prompt_version` is a **column**, `hooks.footer_version` is in JSON | Never append the footer; read `prompt_version` from the column |
| `Message.contact_id` | Nullable | Approve/send must reject NULL or cross-lead contact ids |
| Status contract | `composed` = awaiting review; `queued` = approved | Phase 4 owns `composed -> queued`, `queued -> composed`, terminal states |
| Upstream dedupe | Exact `lead.domain` string, **not** registrable domain; `registrable_domain()` exists and is unused by ingest | Mailer must key its own uniqueness on `registrable_domain()` |
| Schema | `create_all` only; it does **not** backfill indexes/columns on existing tables; `sends` is empty | Add one column + indexes via a tiny idempotent `ensure_schema()`; no Alembic |
| SQLite datetimes | `DateTime(timezone=True)` reads back **naive** on SQLite; Python aware-minus-naive raises `TypeError`; binding a Warsaw-aware boundary drops the offset (1–2 h error) | All SQL filters bind aware **UTC**; all Python read-backs go through `ensure_utc()` |
| Timezones | `zoneinfo.ZoneInfo("Europe/Warsaw")` raises `ZoneInfoNotFoundError` on this Windows box (`TZPATH=()`); no `tzdata` installed | Add `tzdata` as the only new dependency |
| Sender identity | `mail_from_name` / `mail_from_address` shared with Phase 3 footer; `SenderProfile.from_settings` fails fast | SMTP `From` is exactly these; validate at approve and at send |
| Transport | `sendmail()`/`send_message()` cannot distinguish pre-DATA from post-DATA failure | Stage the protocol manually (`mail`/`rcpt`/`data`) |
| CTE | `set_content(..., charset="utf-8")` yields `8bit` | Force `cte="quoted-printable"` for 7-bit-safe Polish text |
| Indexes | `events` has none; `sends.message_id`, `messages.lead_id` unindexed | Add three indexes in `ensure_schema()` |
| Tests | Offline, `pytest-asyncio` auto mode, `db` fixture on temp SQLite, injected fakes | SMTP/IMAP must be seams; no sockets, no sleeping |

---

## 3. Architecture

```
composed (draft) --approve--> queued (approved)
                                  |
                                  v
+---------------------------------------------------------------------+
| SendRunner — sequential, live only when --live                      |
|  select snapshots -> preflight connection -> render MIME bytes      |
|  -> reservation transaction (re-checks + pending row)               |
|  -> write live .eml -> mail/rcpt/data -> finalize -> event          |
|  -> exponential backoff gate + Gaussian jitter between sends        |
+---------------------------------------------------------------------+
        |                                   |
        v                                   v
  MailTransport protocol              MailRepo (sends + events)
  - SmtpTransport (stdlib, staged)
  - FakeTransport (tests)
        |
        v
  manual workflow: sends / outbox / mark / suppress / unsuppress
  (Phase 4.5: IMAP ingest replaces the manual loop)
```

**Package layout**

```
src/forgesmith/
  mailer/
    __init__.py       # send_queued(), SendStats, module exports
    models.py         # SendMode, SendStatus, RecheckResult, SendOutcome, SendStats
    mime.py           # EmailMessage build, Message-ID, render bytes, .eml write
    transport.py      # MailTransport protocol, SmtpTransport, FakeTransport
    policy.py         # window, cap, allowlist, per-domain/recipient, backoff, jitter
    runner.py         # selection, preflight, reservation, send, finalize
  db/schema.py        # ensure_schema() idempotent column/index completion
  db/repo.py          # + MailRepo (approve, select, reserve, finalize, mark, suppressions)
  cli.py              # + approve, unapprove, send, sends, outbox, mark, unsuppress
  config.py           # + mailer knobs
```

**Concurrency:** one send at a time, one process. A live run takes an OS advisory lock
(`data/mailer.lock`, `msvcrt.locking` / `fcntl.flock`, non-blocking, auto-released on crash,
file never unlinked). Multi-machine/Postgres locking is Phase 5.

**Dependencies:** `tzdata` only (free, data-only, required for `zoneinfo` on Windows and
Alpine). Everything else is stdlib: `smtplib`, `ssl`, `email`, `imaplib` (Phase 4.5),
`zoneinfo`, `random`, `socket`, `asyncio.to_thread`.

**Transactions:** one short `session_scope` per phase. No session is held across SMTP,
sleep, or file I/O. Selection returns frozen id snapshots, never ORM instances
(`expire_on_commit=False` would otherwise keep stale objects looking alive).

---

## 4. Schema completion (the only DDL in Phase 4)

1. Add to `Send` in `models.py`: `created_at: Mapped[datetime | None]` with
   `DateTime(timezone=True), default=utcnow`. Physically nullable so the `ALTER` is portable;
   application code always writes it, and tests assert it. `sends` is empty today, so this is
   completing the ledger, not migrating live data.
2. `db/schema.py::ensure_schema()` — called from `init_db()`, idempotent, runs twice safely:
   - SQLite: `PRAGMA table_info(sends)` → `ALTER TABLE sends ADD COLUMN created_at DATETIME`
     if absent.
   - Postgres: `ALTER TABLE sends ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ`.
   - Indexes on both: `CREATE INDEX IF NOT EXISTS ix_sends_message_id ON sends(message_id)`,
     `ix_messages_lead_id ON messages(lead_id)`, `ix_events_entity_action ON
     events(entity, entity_id, action)`. (`create_all` will not backfill these on the existing
     DB — verified.)
3. No generic migration runner. If a second column ever needs this, stop and adopt Alembic.
   Document in the docstring that the Postgres branch is exercised via `docker-compose` smoke,
   not the default offline suite. README: run `forgesmith db init` after upgrading.

---

## 5. Approval contract

**Storage:** `events(entity="message", entity_id=<messages.id>, action="approved" | "unapproved")`.
`detail` carries `lead_id`, `contact_id`, `to_email`, `footer_version`, `bulk`. No column on
`messages` (immutable; `entity_id` is an Integer, so no JSON extraction or dialect casts).

**Rule:** a send is allowed only when `lead.status == queued` **and** the last approval-family
event (`approved`/`unapproved`) for the lead's latest `messages.id` is `approved`.
`unapprove` writes `unapproved`, so a drifted ledger cannot send. Status remains the coarse
gate; the ledger is the auditable approval record, not decoration.

**`approve`** (`--lead-id N [--message-id M | --all-composed] [--limit N] [--yes]`):
- Refuses non-`composed` leads, non-latest messages, NULL/mismatched `contact_id`, missing
  contact, suppressed email/domain, stale `footer_version`, empty sender config.
- Runs the same footer/prompt checks as send, so problems surface at queue time, not at 09:00.
- `--message-id` exists only as an assertion (must equal the latest); no approval of old drafts.
- Bulk: preview table; `--yes` required when >1 lead or stdin is not a TTY; one transaction per
  lead; skips and refusals are reported separately from idempotent "already queued"; an
  interrupted bulk leaves a safe approved prefix.
- Idempotent: approving an already-approved latest message is a no-op + event.

**`unapprove --lead-id N`:** only from `queued` with no `sent`/`pending`/`uncertain` send.
Writes `unapproved` + `composed`. This is the **only** way to regenerate a queued lead:
`unapprove` → `compose --regenerate` → new message → `approve`.

**Version policy:** `hooks.footer_version` must equal `FOOTER_VERSION` exactly (the opt-out is a
legal statement). `messages.prompt_version` mismatch is recorded and warned but does not block,
so cosmetic prompt edits do not force regeneration.

---

## 6. Selection and pre-send re-check

**Selection** (`MailRepo.select_queued`): `status == queued`; latest message exists; approved;
ordered by `(attempt_count, lead.id)`; optional `--lead-id`, `--source`, `--limit`. Returns
frozen snapshots (`lead_id`, `message_id`, `contact_id`, `to_email`).

**Reservation re-check** — inside one transaction, after the SMTP connection/TLS/login
succeeds, before any `MAIL FROM`:

| Check | Reason code | Outcome |
|---|---|---|
| `lead.status == queued` | `not_queued` | skip |
| latest message id == approved message id | `stale_approval` | skip |
| last approval-family event is `approved` | `not_approved` | skip |
| message/contact exist, `contact_id` non-null and belongs to lead | `contact_missing` | skip |
| `contact.email` == approval `to_email` (normalized) | `contact_changed` | skip |
| email non-empty, ASCII, no CRLF/control chars | `email_invalid` | skip |
| email and domain not suppressed | `suppressed` | skip |
| `hooks.footer_version == FOOTER_VERSION` | `stale_footer` | skip |
| attempts < `mail_max_attempts` | `attempts_exhausted` | skip + surface |
| `now >= retry_not_before` (backoff) | `retry_not_before` | defer |
| no live `sent`/`pending`/`uncertain` attempt for this message | `existing_attempt` | skip |
| no `sent`/`pending`/`uncertain` to same **registrable** domain | `domain_already_contacted` | skip |
| no `sent`/`pending`/`uncertain` to same normalized email | `recipient_already_contacted` | skip |
| within sending window | `outside_window` | stop run |
| daily cap not reached | `daily_cap` | stop run |
| allowlist match (live) | `not_allowlisted` | skip |
| sender identity valid | `config_invalid` | fatal |

Selection is an optimization; **the reservation transaction is authoritative**, including the
existing-attempt guard. The live run lock is what closes the remaining race (no unique
constraint exists on `sends`).

---

## 7. Transport and delivery classification

`MailTransport` protocol:

```python
class MailTransport(Protocol):
    async def send(
        self, message_id: str, sender: str, recipient: str, raw: bytes
    ) -> SendOutcome: ...
```

`SmtpTransport` (stdlib, run in `asyncio.to_thread`), injected SMTP client seam for tests:
1. Connect with `smtp_timeout`; STARTTLS by default (587) or implicit TLS (`smtp_ssl`, 465);
   `login()` when credentials are set.
2. `mail(sender)`; `rcpt(recipient)`; `data(raw)`; `quit()` in `finally`.
3. Return a stage-tagged outcome (`connect`, `mail`, `rcpt`, `data`, `teardown`) plus
   code/response. The stage is what distinguishes `failed` from `uncertain`.

**Classification**

| Condition | `sends.status` | Lead | Side effects |
|---|---|---|---|
| `250` after `DATA` | `sent` | `sent` | `sent_at`, `sent` event |
| `5.1.x` at RCPT (bad mailbox) | `bounced` | `bounced` | suppress email; `5.1.2` may suppress domain; bounce event |
| bare `550` with no enhanced code | `failed` | unchanged | **never** auto-suppress |
| other `5xx` (e.g. `5.7.1` policy/spam) | `failed` | unchanged | counts as attempt; two consecutive `5.7.x` across recipients **abort the run** |
| `4xx` at MAIL/RCPT (greylist, 421) | `failed` | unchanged | retry after backoff; `421` at MAIL aborts the run |
| exception/timeout before the terminating `.` is accepted | `failed` | unchanged | nothing delivered; attempt consumed |
| exception/timeout after `.` / during teardown | `uncertain` | unchanged | never auto-retried |
| connect/TLS/auth/DNS before reservation | no row | unchanged | fatal, abort run, no attempt consumed |
| connection lost at MAIL after reservation | `aborted` | unchanged | excluded from attempts/cap/domain/recipient; abort run |

`failed` always means "a real attempt happened and counts". `aborted` is the explicit
pre-MAIL infrastructure state so "no attempt consumed" is expressible. If the live `.eml`
cannot be written after reservation, the send becomes `aborted` (`stage=archive`) and the run
stops: no transmission without its evidence file. A transport that raises an unclassifiable
exception becomes `uncertain` and also stops the run.

---

## 8. Crash safety and send sequence

1. **Preflight** (before any reservation): live config valid, zone loads, window open, cap not
   reached, lock acquired, SMTP connect/TLS/login succeeds, MIME bytes rendered. Failures here
   write nothing.
2. **Reserve**: one transaction runs the §6 checks, inserts
   `sends(status="pending", mode="live", to_email=..., smtp_message_id=<generated id>,
   created_at=now)` + `events(action="send_attempt")`, commits.
3. **Archive**: write `data/outbox/lead{id}-msg{id}-s{send_id}.eml` atomically (`tmp` +
   `os.replace`). Live files are immutable evidence; retries never overwrite them.
4. **Send**: `mail`/`rcpt`/`data` outside any transaction.
5. **Finalize**: by `send.id`, unconditional last-writer-wins — `sent`/`failed`/`bounced`/
   `uncertain`/`aborted`, `error` code, truncated response, `sent_at` on success; update the
   lead; append the event.
6. **Recovery**: at the start of every live run, **all** pre-existing `pending` rows are marked
   `uncertain` (single writer + lock makes this exact). No timeout config, no clock arithmetic.
   Uncertain rows are loud: counted in `stats`, printed in the run preamble, excluded from
   selection, resolved only via `mark`.

Dry run: same preflight (minus lock/connect), full re-check, render, write deterministic
`data/outbox/lead{id}-msg{id}.eml`, write `events(action="dry_run")`, **no `sends` row**, lead
stays `queued`. Rerunning dry overwrites the file. A later live send still selects the lead.

---

## 9. Pacing, caps, windows, uniqueness

- **Exponential backoff (per message):** next attempt ≥ last `failed` attempt
  `created_at + min(mail_backoff_base * 2**(attempts-1), mail_backoff_cap)`; defaults 300 s /
  3600 s; evaluated in SQL with an aware UTC cutoff.
- **Gaussian jitter (between live sends):** `clamp(random.gauss(540, 180), 120, 1800)`; inject
  `sleep` and `random.Random(seed)` for tests; no sleep in dry runs.
- **Daily cap:** count `sends.status IN ('sent','uncertain')` with `sent_at`/`created_at` inside
  the current `Europe/Warsaw` day, **boundary converted to UTC before SQL**; default 20; dry,
  `failed`, and `aborted` excluded. Cap reached → print and exit 0. `--force-window` does not
  bypass the cap.
- **Window:** weekdays, 09:00–17:00 `Europe/Warsaw` (`mail_weekdays_only`), checked before each
  send. Closed → print the next opening and exit 0 (no multi-hour sleep). `--force-window`
  bypasses only the window.
- **Allowlist:** comma-separated emails/domains; empty = allow all; live only; exact email or
  registrable-domain match; checked in the reservation transaction.
- **Per-domain:** block when `sent`/`pending`/`uncertain` exists for the same
  `registrable_domain(lead.domain)` ever, plus a within-run set; fall back to the contact
  email's registrable domain when `lead.domain` is NULL; **skip freemail domains**
  (`gmail.com`, `wp.pl`, `o2.pl`, `interia.pl`, `outlook.com`, …). Exclude the current lead.
- **Per-recipient:** block a normalized email already sent/pending/uncertain, plus within-run.
- **Timezone helper:** `ensure_utc()` for every read-back; `ZoneInfo` validated at startup with
  an actionable error if `tzdata` is missing; reject `window_start >= window_end` (no
  cross-midnight).

---

## 10. Status transitions

| Trigger | `sends.status` | Lead status |
|---|---|---|
| Approved | — | `composed -> queued` |
| Unapproved | — | `queued -> composed` |
| Reserved | `pending` | unchanged |
| `250` after DATA | `sent` | `sent` |
| Dry run | no row | unchanged (`queued`) |
| Transient 4xx / pre-DATA network error | `failed` | unchanged |
| 5.1.x at RCPT | `bounced` | `bounced` |
| Other 5xx | `failed` | unchanged |
| Post-DATA unknown | `uncertain` | unchanged |
| Pre-MAIL infrastructure loss | `aborted` | unchanged |
| `mark --status replied` | — | `replied` (terminal, stops sending) |
| `mark` / `suppress` opt-out | — | `suppressed` (terminal) |

Failure never downgrades a queued lead; only success writes `sent`. `suppressed` is terminal.

---

## 11. Manual ops (replaces IMAP for this phase)

At ≤20 sends/day, a human reads replies in the real mailbox. Two commands cover the loop:

- `forgesmith mark --lead-id N --status replied|bounced|sent|suppressed [--reason TEXT]`
  - `sent` refused unless a `sent`/`uncertain` send exists (no orphan `sent` leads).
  - `bounced`/`suppressed` also create the suppression for the destination email in the same
    transaction.
  - Writes a `marked` event on `entity="lead"`.
- `forgesmith mark --send-id S --status sent|failed [--yes]`
  - Only for `pending`/`uncertain`; `sent` backfills `sent_at` from `created_at` (documented as
    approximate); `failed` makes the message retryable and still counts an attempt.
  - This replaces `--retry-uncertain` entirely.
- `forgesmith unsuppress --email ADDRESS | --domain DOMAIN [--reason TEXT]`
  - Deletes the row and writes an `unsuppressed` event on `entity="suppression"`, preserving the
    prior reason in `detail`. Required because automatic suppression must be reversible.
- `forgesmith sends [--status ...] [--lead-id N] [--limit 50]` — attempt ledger.
- `forgesmith outbox [--limit 20]` — `.eml` files with lead/message/send ids.
- Every suppression creation (manual or automatic) writes an event.

---

## 12. Configuration (documented in `.env.example`)

```
# existing
mail_dry_run=true                 # default for programmatic/Phase 5 callers; CLI uses --live
mail_daily_cap=20
mail_delay_mean=540.0
mail_delay_sigma=180.0
mail_from_name / mail_from_address
smtp_host / smtp_port / smtp_user / smtp_password

# new
mail_delay_min=120.0
mail_delay_max=1800.0
mail_backoff_base=300.0
mail_backoff_cap=3600.0
mail_window_start=9
mail_window_end=17
mail_window_tz=Europe/Warsaw
mail_weekdays_only=true
mail_max_attempts=3
mail_allowlist=""                 # empty = allow all (live)
smtp_ssl=false                    # 465 implicit TLS
smtp_starttls=true                # 587 STARTTLS
smtp_timeout=30.0
```

`tzdata` added to `pyproject.toml` and `uv.lock`. IMAP keys (`imap_host/port/user/password/
mailbox/lookback_days`) land in Phase 4.5, not here.

---

## 13. CLI contract

| Command | Behavior |
|---|---|
| `approve --lead-id N [--message-id M]` | Approve the latest draft; refusals listed in §5 |
| `approve --all-composed [--limit N] [--yes]` | Bulk, preview, per-lead transactions |
| `unapprove --lead-id N` | Retract approval; only with no sent/pending/uncertain |
| `send [--limit N] [--lead-id N] [--source X] [--live] [--yes] [--no-delay] [--force-window]` | Dry by default |
| `sends [--status ...] [--lead-id N]` | Attempt ledger incl. uncertain/aborted |
| `outbox [--limit N]` | Rendered `.eml` files |
| `mark ...` / `unsuppress ...` | §11 |
| `inbox sync` | **Not in Phase 4** — Phase 4.5 |

**Live safety contract (single source of truth: `--live`):**
- `mail_dry_run` is the default for programmatic/Phase 5 callers; the CLI never consults it.
- `--live` requires: valid sender identity, successful preflight connection, allowlist match
  when configured, and confirmation.
- Confirmation = a LIVE banner with count and first recipients; `--yes` is mandatory when >1
  message **or** stdin is not a TTY. Missing confirmation → exit 2.
- Exit codes: `0` completed (including cap/window stop), `1` fatal (SMTP auth/config after
  reservation, DB, lock held), `2` usage/refused.
- Lock held by another process → print holder PID, exit 1.
- Logging masks recipients (`b***@sklep.pl`) at info level; never log bodies; truncate SMTP
  responses.

---

## 14. Testing (offline; ~45–55 new tests)

| File | Cases |
|---|---|
| `test_mailer_schema.py` | old-shape DB upgraded in place; `ensure_schema()` twice; indexes exist; fresh `create_all` matches; model/DLL agree on `created_at` |
| `test_mailer_repo.py` | approve/unapprove transitions and refusals; non-latest refused; stale approval after an inserted message; ledger drift with forced `queued` blocked; bulk skip/refusal reporting; mark/unsuppress audit |
| `test_mailer_policy.py` | window open/close at 09:00/17:00 Warsaw; DST edges 2026-03-29/2026-10-25; cap counts sent+uncertain and excludes dry/failed/aborted; UTC boundary conversion (23:30 Warsaw = 21:30 UTC); backoff gate; allowlist case/subdomain; registrable-domain + freemail + NULL fallback; recipient dedupe |
| `test_mailer_mime.py` | From/To/Subject/Date/Message-ID; Polish RFC 2047 subject; UTF-8 body with **CTE quoted-printable asserted**; footer exactly once; no Bcc; `.eml` parses with `policy.default`; CRLF/header injection rejected; deterministic dry name; live name includes send id |
| `test_mailer_recheck.py` | one test per reason code in §6; version policy (footer blocks, prompt warns); `hooks.prompt_version` regression (must read the column) |
| `test_mailer_runner.py` | dry run writes files + event and **no sends row**, lead stays queued; a following live run still selects it (regression); success → sent; transient → failed + retry; 5.1.x → bounced + suppression; bare 550 → failed, not suppressed; 5.7.x streak aborts; `aborted` on pre-MAIL loss; injected sleep jitter list is clamped and empty for dry; poison-lead ordering |
| `test_mailer_crash.py` | reserve row visible from a second session before the fake transport is called; pending → uncertain at startup; uncertain excluded from selection; `mark --send-id sent|failed` resolves both ways; lock refused by a second process |
| `test_mailer_smtp.py` | fake SMTP client: 250, 5.1.1, 5.1.2, bare 550, 5.7.1, 4xx, 421 at MAIL, drop after DATA, auth failure; assert status + lead status + error code + abort behavior |
| `test_mailer_config.py` | zone loads (tzdata present); invalid window rejected; live refuses without sender/allowlist/confirmation |
| CLI smoke | approve → send (dry) → outbox → `send --live --yes --limit 1` with fake transport → sends; exit codes |

Fakes: `FakeTransport` (records raw bytes, returns/raises scripted outcomes) and `FakeSMTP`
(injected as the SMTP client seam). No socket-level SMTP stub. Every mailer test sets
`MAIL_FROM_*` / `SENDER_*` and monkeypatches the `settings` singleton, matching
`tests/conftest.py`.

**Gates:** `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.

---

## 15. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| **M4.1** | `ensure_schema` + `sends.created_at` + indexes; approval ledger; `approve`/`unapprove`; footer/prompt checks at approval; `tzdata` | Upgrades the existing DB in place with no data loss; non-latest and suppressed approvals refused; bulk reports skips and refusals; tests green |
| **M4.2** | MIME builder, dry run, `outbox`; selection snapshots; full re-check reason codes | Valid Polish `.eml` (QP, footer once), no sends row, lead stays queued; a subsequent live run still selects it; every reason code tested |
| **M4.3** | Staged `SmtpTransport`, reservation/finalize, recovery, run lock, backoff | 250→sent; 5.1.x→bounced+suppressed; bare 550/5.7.x→failed; post-DATA drop→uncertain; pending recovered to uncertain; second live process refused; all offline |
| **M4.4** | Caps, window, jitter, allowlist, per-domain/recipient, `sends`, `mark`, `unsuppress` | Fixed-clock cap/window/DST tests; `--force-window` scope; within-run and cross-run uniqueness; uncertain cap counting; mark/unsuppress audited |
| **M4.5** | Docs + live smoke | README/AGENTS/`.env.example` match behavior; approve → dry (inspect `.eml`) → `send --live --yes --limit 1` to an allowlisted self-address → `sends` shows sent → `mark replied`; no uncertain rows; ruff/pytest clean |

Build order: schema/approval → MIME/dry run → live transport/crash → policy/ops → docs/smoke.

---

## 16. Phase 4.5: IMAP ingest (implemented)

AGENTS.md Phase 4 requires approval, SMTP with backoff, randomized delays, suppression
re-checks, and dry-run output — **not** IMAP. Only the README roadmap promised it. The
README row 4 now reads "approval, dry-run `.eml`, SMTP with caps/jitter/backoff, manual
bounce/reply marking" and row 4.5 is the shipped IMAP stage. The full contract lives in
[`phase4.5.md`](phase4.5.md); the design sketch below is kept for history, with the
decisions that changed marked.

- stdlib `imaplib.IMAP4_SSL` in `asyncio.to_thread`; `readonly=True` (EXAMINE); `BODY.PEEK`.
- Header pass (`BODY.PEEK[HEADER]`), then `BODY.PEEK[TEXT]` only for `multipart/report` and
  for messages whose `In-Reply-To`/`References` are present; bodies above
  `IMAP_MAX_BODY_BYTES` stay header-only.
- DSN parse `message/delivery-status` `Final-Recipient`/`Action`/`Status`; match
  `Final-Recipient` → `X-Failed-Recipients` → embedded `message/rfc822` Message-ID.
- Hard 5.x → bounce + email-only suppression, applied automatically. Soft 4.x and `delayed`
  → ledger row only; **no soft-bounce cap** (the sketch's `mail_soft_bounce_cap` was cut:
  a threshold would silently burn a domain on ambiguous evidence).
- Replies: `In-Reply-To`/`References` → `replied` automatically; RFC 3834 auto-replies
  (`Auto-Submitted`) and list/bulk mail are ignored; strip quoted history before scanning
  opt-out phrases (case/diacritic folding incl. Polish `ł`); log the matched phrase.
- **Opt-outs are review-only.** `inbox apply --id N --yes` is the human confirmation and
  suppresses only the sender's email address. No inbound message ever creates a domain
  suppression, and unmatched DSNs/replies land in `needs_review` instead of guessing.
- Dedupe by inbox `Message-ID`, falling back to `hash(from, date, subject, size)`, plus a
  unique `(mailbox, uid_validity, imap_uid)` constraint; UIDVALIDITY changes reset the cursor.
- A separate `data/inbox.lock` (exit 75 when held); correctness rests on the unique
  constraints and the compare-and-swap in `InboxRepo.apply_inbound`, not on the lock.
- `inbox sync --dry-run` writes nothing at all, including the cursor row.


---

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Double send | Run lock + reservation authoritative re-checks; no auto-retry of uncertain; single machine documented (Phase 5 adds a DB constraint) |
| Double send on SQLite | Advisory lock; existing-attempt guard; tests use two sessions to prove reserve-before-send |
| Crash after SMTP accept | Post-DATA errors → uncertain, never `failed`; live `.eml` per `send_id` preserves evidence |
| SQLite naive datetimes | No Python subtraction on read-backs; `ensure_utc()`; all filters bind UTC |
| Warsaw boundary drift | Convert boundary to UTC before SQL; fixed-clock DST tests |
| Missing tzdata | Add dependency; validate zone at startup with actionable error |
| Footer duplicated | Send `body` verbatim; assert exactly one footer |
| Polish text mangled | Force `cte="quoted-printable"`; RFC 2047 subject test |
| Policy 5xx nukes good prospects | Only 5.1.x suppresses; bare 550 → failed; 5.7.x streak aborts |
| Gmail suspension / low caps | Dry default, cap, jitter, allowlist, From-mismatch preflight warning, README warning, dedicated domain recommended |
| Suppression false positive | `unsuppress` ships in the same milestone as auto-suppression |
| Exhausted/uncertain leads invisible | Run preamble prints counts; `sends --status`; `mark` resolves |
| Non-interactive confirmation | `--yes` required when >1 or non-TTY; exit 2 |
| `ensure_schema` drift | Model updated first; idempotent; called from `init_db`; tested twice |
| Postgres branch untested offline | Marked docker-compose smoke; docstring states the gap |
| Timezone/backoff config mistakes | Validate at startup; reject invalid windows; fail fast before reserving |
| Cost | Zero paid services; `tzdata` is free data only |

---

## 18. Definition of done

- [x] `sends.created_at` and three indexes exist on both a fresh DB and the existing dev DB;
      `ensure_schema()` is idempotent and runs from `init_db`.
- [x] `approve`/`unapprove` implement the latest-message approval rule; non-latest, suppressed,
      and stale-footer approvals are refused; bulk skips are reported.
- [x] Dry run writes a valid Polish `.eml` (quoted-printable, footer once), no `sends` row, and
      leaves the lead `queued`; a following live run still selects it.
- [x] Live send reserves before SMTP, never double-sends under a concurrent run, and classifies
      250/5.1.x/other-5xx/4xx/post-DATA-drop correctly.
- [x] Pending rows recover to `uncertain` at startup; uncertain is loud, excluded from
      selection, and resolvable only via `mark`.
- [x] Cap (sent+uncertain), window, jitter, backoff, allowlist, per-domain and per-recipient
      rules are enforced and tested with a fixed clock, including DST.
- [x] `sent` is documented as "accepted by the relay, not inbox delivery".
- [x] `mark` and `unsuppress` are audited; automatic suppression is reversible.
- [x] IMAP is documented as Phase 4.5; README/AGENTS/`.env.example` match reality.
- [x] Offline suite and ruff gates pass (278 tests, offline; no real sockets or sleeps).
- [ ] One allowlisted live smoke still to be run by the operator (needs real SMTP credentials).

---

## 19. Review record

- Round 1 challenged the draft's crash safety, `zoneinfo`/`tzdata`, SQLite datetimes, dry-run
  workflow, `.eml` naming, missing config keys, absent exponential backoff, and IMAP scope.
- Round 2 independently verified every load-bearing claim, corrected three review-1 errors
  (`hooks.prompt_version`, `sendmail` stage blindness, blanket 5xx suppression), and proposed
  the `sends.created_at` completion, `aborted` status, staged SMTP, OS advisory lock, and the
  manual `mark`/`unsuppress` workflow.
- Round 3 converged with four refinements: model-first schema completion, render → reserve →
  write → send ordering for live `.eml`, enhanced-status classification, and the rule that
  `failed` always means a real attempt.
- **Residual choices:** `sends.created_at` is a deliberate one-column exception to the
  no-migration posture, justified by the empty table and the cost of dialect-specific JSON
  clocks; if rejected, fall back to the `send_attempt` event clock (everything else stands).
  IMAP is deferred to 4.5 because the authoritative goal doc does not require it and a manual
  workflow covers ≤20 sends/day.
