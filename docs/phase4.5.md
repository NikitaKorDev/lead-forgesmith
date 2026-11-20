# Forgesmith — Phase 4.5: IMAP Ingest (final plan)

**Status:** implemented.
**Goals source:** `AGENTS.md` (Phase 4.5: "DSN bounce parsing, reply/opt-out
detection, and automatic suppression from the real mailbox"), `README.md`,
`phase4.md` §16.
**Baseline:** Phases 0–4 implemented. `sends` has `smtp_message_id` but no
inbound state; `mark`/`unsuppress` are the manual loop this phase automates.
**Debate record:** three camps (protocol correctness, scope/abuse, data model)
plus two rebuttal rounds and a verification round. Every load-bearing claim was
checked against this repository (SQLite, Windows, Python 3.12, SQLAlchemy 2.1).

---

## 1. Goal and definition of success

Replace the manual "read the mailbox, then run `mark`" loop with a read-only
IMAP pass that correlates inbound mail to the send ledger and applies the two
outcomes that are safe to automate: **hard bounces** and **replies**.

Success means **correct attribution and zero silent writes**, not coverage:

- The mailbox is opened read-only; `\Seen` is never set.
- Exactly one inbound state change is automatic: a hard `5.x` DSN that names an
  address we actually mailed.
- Reply text never creates a suppression. Opt-out phrases become a review row.
- Re-running a sync is a no-op; a crash replays safely.
- A forged DSN can at most stop us contacting an address we already mailed.
- Every action is auditable in `inbound_messages` + `events`.

**Out of scope:** opt-out phrase → suppression, soft-bounce caps, inbound
domain suppressions, new `SendStatus` values, IMAP IDLE/pollers, thread
storage, attachments, HTML rendering, provider rotation, Phase 5 scheduling.

### Why the obvious §16 sketch was cut

`phase4.md` §16 proposed phrase-matching opt-outs and a `mail_soft_bounce_cap`.
Both were rejected after review:

- **Phrase matching is not decidable.** `normalize_for_match`
  (`compose/validate.py:113`) is NFKC + casefold, which does *not* fold Polish
  diacritics (`proszę` ≠ `prosze`), and no quote/history stripper exists. Our
  own footer contains an opt-out phrase, so a reply that quotes history matches
  it. A false positive silently burns a lead; `suppression` is the global send
  gate (`recheck` repo.py:1003, `approve` 761, `select_composable` 169).
- **A soft-bounce cap protects nothing.** No retry can fire on a `sent` send
  (`EXISTING_ATTEMPT`, repo.py:1030), `SendStatus` has no soft state, and a
  4.x "mailbox full" would permanently suppress a good lead. 4.x is an event.

---

## 2. Prerequisites (shipped first)

**P1 — `_suppress` / `unsuppress` lookup bug.** `Suppression` is unique on
`value` (`models.py:179`), but both functions filtered on `(value, kind)`
(`repo.py:1262`, `1404`). An `email` row followed by a `domain` row with the
same string raised `IntegrityError`, which would abort an ingest transaction
mid-loop and lose the cursor advance. Fixed: look up by `value`; `unsuppress`
refuses a `kind` mismatch. Code only, no DDL.

**P2 — status monotonicity.** `finalize` assigned `lead.status = SENT`
unconditionally (`repo.py:1165`), as did `mark_send` (`1372`) and the bounce
branch (`1176`). Phase 4.5 adds a writer whose purpose is `replied`/`bounced`,
so a reply landing between `select_queued` and `finalize` was silently erased.
Fixed: never move a lead out of `replied | bounced | suppressed`; the
previously unused `TERMINAL_LEAD_STATUSES` (`mailer/models.py:43`) is now the
guard.

**P3 — DDL test gap.** `test_model_and_ddl_agree` only asserted a hardcoded
subset of ORM metadata and never read `_INDEXES`, so an index could exist on
old DBs and be missing on fresh ones. Fixed: assert `_INDEXES ⊆ metadata`.

---

## 3. Schema

New tables are created by `create_all` on every `init_db` (`session.py:58`);
no `ensure_schema` DDL is needed for them. The only external DDL is one index
on the pre-existing `sends` table.

### `inbound_messages`

| column | type | note |
|---|---|---|
| `id` | Integer PK | |
| `mailbox` | String(255) | `imap.gmail.com/user/INBOX` |
| `uid_validity`, `imap_uid` | BigInteger | IMAP UIDs are unsigned 32-bit; PG `Integer` overflows |
| `dedupe_hash` | String(64) NOT NULL | sha256; NULLs are distinct in unique indexes |
| `rfc_message_id` | String(512) | informational, not a key |
| `kind` | String(16) | `dsn` / `reply` / `optout` / `arf` / `mdn` / `auto` / `other` |
| `status` | String(16) | `new` / `applied` / `needs_review` / `ignored` / `failed` |
| `from_addr`, `to_addr`, `final_recipient` | String(320) | |
| `subject` | String(512) | |
| `lead_id`, `send_id` | Integer FK nullable | no `messages` FK (reachable via `send.message_id`) |
| `reported_message_id` | String(512) | DSN header, bracket-normalized |
| `enhanced_status`, `smtp_code` | String(16), Integer | |
| `detail` | JSON | parsed fields, matched phrase + span — never the body |
| `received_at`, `seen_at`, `applied_at` | DateTime(tz) | |

Constraints: `(mailbox, dedupe_hash)`, `(mailbox, uid_validity, imap_uid)`;
indexes on `status`, `lead_id`, `send_id`.

`dedupe_hash` = sha256 of the normalized `Message-ID` when present, else of
`From|Date|Subject|Final-Recipient|size` (trace headers stripped). A nullable
key would defeat dedupe on both dialects.

### `mailbox_state`

`mailbox` UNIQUE, `uid_validity`, `last_uid` (BigInteger, default 0),
`last_sync_at`, `last_error`. A table rather than a file because the cursor
advance must commit in the same transaction as the ledger row.

### `sends`

`ix_sends_smtp_message_id`, registered in **both** `models.py` and
`schema.py::_INDEXES` (P3 makes the invariant real).

---

## 4. Architecture

```
imap (read-only) ──► ImapInbox ──► parse() ──► sync_inbox()
                                                   │ one tx per message
                                                   ▼
                                     inbound_messages + mailbox_state
                                                   │
                              ┌────────────────────┴──────────────────┐
                       hard 5.x DSN                             everything else
                              │                                       │
              lead bounced + email suppression                 status = new /
              (Send.status untouched for `sent`)              needs_review / ignored
                                                                       │
                                                              inbox apply (CAS)
                                                                       │
                                                     replied / email suppression
```

```
src/forgesmith/inbox/
  models.py   # ParsedInbound, InboundKind, SyncStats, InboxConfigError
  parse.py    # pure bytes -> ParsedInbound (no I/O)
  source.py   # InboxSource protocol, ImapInbox (client_factory seam), FakeInbox
  runner.py   # sync_inbox(): lock, fetch, parse, one tx per message
db/models.py  # InboundMessage, MailboxState, sends index
db/repo.py    # InboxRepo: record, CAS apply, monotonic cursor
db/schema.py  # ix_sends_smtp_message_id
cli.py        # inbox sync | list | show | apply
config.py     # imap_* keys, inbox_lock_path
```

**Concurrency:** a separate `data/inbox.lock` (`RunLock`, non-blocking) so a
second sync fails loudly instead of hammering the provider. Correctness does
**not** depend on it: the per-message transaction, the two unique constraints
and the CAS do that. The lock is deliberately *not* shared with `mailer.lock`,
which is held across the whole jittered send run (`runner.py:97-142`).

**Dependencies:** none. `imaplib`, `email`, `hashlib` are stdlib.

---

## 5. Sync flow

1. Acquire `data/inbox.lock`. Held → print holder PID, exit 1.
2. `IMAP4_SSL`, login, `select(mailbox, readonly=True)`, read `UIDVALIDITY`.
3. `UIDVALIDITY` changed → event, reset `last_uid`, re-bootstrap from
   `SINCE now - imap_lookback_days`. Never trust stale UIDs.
4. `UID SEARCH UID <last_uid+1>:*`, then **discard UIDs ≤ `last_uid`**: RFC 3501
   collapses `n:*` to the highest UID when `n` exceeds the mailbox.
5. `RFC822.SIZE` + `BODY.PEEK[HEADER.FIELDS (...)]` only. `BODY.PEEK[TEXT]` only
   for `report-type` messages and thread-matched replies, capped by
   `imap_max_body_bytes`; oversized becomes a ledger row, not a crash.
   `BODY[]`/`RFC822` would set `\Seen` in a mailbox a human reads.
6. Parse with `email.message_from_bytes(..., policy=default)`; never a latin-1
   reparse (our product is quoted-printable Polish).
7. **One transaction per message**: insert the ledger row, apply at most one
   transition, advance `last_uid` with `WHERE last_uid < :uid`. `IntegrityError`
   means "already ingested" → still advance. Crash → rollback → refetch.
8. `--dry-run`: fetch, parse, print, **zero DB writes and no cursor movement**.
9. `logout()` in `finally`; socket timeout set because `to_thread` cannot
   cancel a hung `imaplib` call.

---

## 6. Matching ladder

1. **Top-level `report-type`**: `delivery-status` → DSN; `feedback-report`
   (ARF) → event only; `disposition-notification` (MDN) → event only. Nested
   parts do not count (forwarded mail).
2. **DSN correlation**: embedded `message/rfc822` / `text/rfc822-headers`
   Message-ID → `Original-Message-ID` → `In-Reply-To`/`References` →
   `Final-Recipient` → `X-Failed-Recipients` → last completed send to that
   address. Ambiguous → `needs_review`. `Action: delayed` is not a bounce.
3. **Reply**: exact token match of `In-Reply-To`, then `References`
   right-to-left, against `sends.smtp_message_id` (brackets normalized; the
   stored id includes `<>`). Then unique `From` = `contacts.email`, then unique
   `From` domain = `leads.domain`. Thread wins over address.
4. **Exclusions**: `Auto-Submitted != no`, `X-Autoreply`,
   `Precedence: bulk`, `List-Id`.

---

## 7. Effects

| Case | Writes |
|---|---|
| Hard 5.x DSN → `sent` send | `lead.status = bounced` unless already terminal; suppress `final_recipient` (email only, reason `dsn:<code>`); `dsn_bounce` event; ledger `applied`. **`Send.status` untouched** (the relay accepted it; flipping would shrink `count_sent_between` and drop the row from `LIVE_STATUSES` guards). |
| Hard 5.x DSN → `pending`/`uncertain` send | `finalize(status=BOUNCED, stage="dsn", code=…)` — a DSN proves the transaction completed, which beats `recover_pending` guessing `uncertain`. Documented: ≤1/day cap undercount for that day. |
| Soft 4.x / ARF / MDN | ledger row `ignored` |
| Thread-matched reply | `lead.status = replied` (monotonic), ledger `applied` |
| Reply with a strong opt-out phrase | ledger `needs_review` + `detail.optout_phrase`; no suppression; human runs `suppress` |
| Unmatched / ambiguous | ledger `needs_review`, no state change |

No inbound domain suppressions, ever. Suppression reasons are prefixed
`dsn:`/`optout:` and every event carries `inbound_message_id` + `source=imap`,
so inbox-originated suppressions are distinguishable in the audit trail and
reversible with `unsuppress`.

---

## 8. Configuration

```
imap_host=""            imap_port=993          imap_user=""
imap_password=""        # repr=False
imap_ssl=true           imap_starttls=false    imap_timeout=30.0
imap_mailbox="INBOX"    imap_lookback_days=30  imap_fetch_limit=200
imap_max_body_bytes=262144
```

`inbox_lock_path` → `data/inbox.lock`. No `mail_soft_bounce_cap`.
Exit codes mirror Phase 4: `0` completed, `1` fatal (auth/TLS/config/DB/lock),
`2` usage or unconfirmed bulk. Addresses are masked in all output.

---

## 9. Testing (offline)

| File | Cases |
|---|---|
| `test_inbox_parse.py` | hard/soft DSN, multi-recipient, ARF, MDN, delayed, thread match, autoreply exclusion, HTML-only, QP/base64 Polish, quote stripping, opt-out triage, oversized, unknown Message-ID |
| `test_inbox_source.py` | `FakeIMAP` proves EXAMINE + `BODY.PEEK` only, no `STORE`/`EXPUNGE`/`MOVE`; UID `n:*` filtering; UIDVALIDITY change; logout on error |
| `test_inbox_runner.py` | dry run writes nothing; one tx per message; crash replay no-op; dedupe; `IntegrityError` still advances; hard DSN applies; ambiguous → review; `pending` → `finalize` bounced; `sent` untouched; suppressed lead not downgraded; lock held |
| `test_inbox_repo.py` | CAS rowcount; double apply no-op; cursor never rewinds; status ordering |
| `test_inbox_cli.py` | `sync/list/show/apply`, `--all` confirmation, exit codes, masking |
| `test_mailer_repo.py` | P1 email-then-domain; P2 reply-then-finalize interleaving |
| `test_mailer_schema.py` | `_INDEXES ⊆ metadata` and fresh-DB parity |

Fixtures: checked-in `.eml` files (Gmail and Polish hosts deviate from RFC 3464)
plus `make_dsn(status=…)` generators for the combinatorics.

**Unprovable offline (operator smoke):** TLS/app-password auth, whether a given
rejection produces a DSN at all, real UIDVALIDITY resets, provider throttling,
`imaplib`'s missing per-command timeout.

---

## 10. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M4.5.0 | P1, P2, P3 | Failing-then-passing tests; no IMAP code |
| M4.5.1 | models, index, `InboxRepo`, cursor | `db init` upgrades in place; old-shape test passes |
| M4.5.2 | `parse.py` + fixtures | Pure, offline, all DSN/reply cases |
| M4.5.3 | `source.py` + `sync_inbox` + `inbox sync/list` | Dry run writes nothing; replay no-op; lock refused |
| M4.5.4 | `inbox apply/show` | Double apply no-op; `--all` confirms; refusals reported |
| M4.5.5 | Docs + smoke | README/AGENTS/`.env.example`/§16 corrected |

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Misattributed DSN suppresses a live prospect | Strict ladder; ambiguous → review; `unsuppress` documented |
| Cursor rewind/duplicate after partial failure | One tx per message; `WHERE last_uid < :uid`; advance after insert |
| `\Seen` pollution of the operator's mailbox | `EXAMINE` + `BODY.PEEK`, enforced by the fake-IMAP test |
| Forgotten `inbox apply` leaves work invisible | `inbox list` surfaces `new` + `needs_review`; Phase 5 runs sync then apply |
| Tests green while DDL is missing | P3 asserts `_INDEXES` against ORM metadata |
| SQLite naive datetimes | `ensure_utc()` on every read-back |

---

## 12. Review record

- **Round 1** produced three positions: full protocol ladder with opt-out
  triage; a triage-queue-only skeptic; and a two-table schema with a two-phase
  cursor.
- **Round 2** reversed the protocol camp on opt-out writes ("never") and both
  the protocol and data-model camps on cursor semantics (one atomic transaction
  per message). The skeptic's retry-path claim was corrected: a retry path does
  exist (`repo.py:1021`), but it cannot fire for a 4.x on a sent message.
- **Round 3** ruled on the three residuals (separate inbox lock; hard bounces
  apply at sync; `Send.status` untouched for accepted mail), confirmed C1–C5,
  and found three shared blind spots: `BODY.PEEK` size caps, undefined
  `dedupe_hash` inputs, and doc/config drift.
- **Deliberate cuts:** opt-out phrase → suppression, `mail_soft_bounce_cap`,
  inbound domain suppression, IMAP IDLE, new `SendStatus`.
