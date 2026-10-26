# Forgesmith — Project Goals & Roadmap

## Objective

Build a self-hosted B2B lead research and outreach-operations pipeline that:

- discovers businesses from public sources;
- enriches them with observed contact data and business context;
- generates grounded, formal Polish drafts with a local model;
- sends only messages a human approved and the per-country policy gate permits.

The project is also an engineering portfolio piece. It is not a bulk-mail
product, it does not promise delivery-rate tricks, and it does not claim to
automate client acquisition.

## Product principles

- **Human approval is a feature.** `composed` is not `queued`; a person approves.
- **Dry-run is the default.** `FORGESMITH_MAIL_DRY_RUN=true` is a kill switch.
- **Consent before contact.** Countries whose law requires prior consent are
  refused in code, even when listed in configuration. Poland is one of them.
- **Observed data only.** Store what a page or an open dataset actually shows;
  never invent a contact, a NIP or a product.
- **No evasion.** Do not bypass robots.txt, access controls, WAFs or rate limits.

## Technical goals

- **Target extraction:** Python sources for Shopify catalogues, OLX listings
  (terms permitting) and Overture Maps local extracts, all producing the same
  `LeadCandidate` shape.
- **Deep enrichment:** async HTTP first; `Playwright` only when a host blocks
  plain HTTP. Extract observed role-first emails, NIP/legal-name evidence and
  business context. Scraped pages stay local unless an external provider is
  explicitly enabled.
- **AI personalization:** local Ollama by default. Clean and delimit scraped
  text, generate JSON drafts, and enforce grounding, length and plain-text
  gates before a draft is persisted.
- **Delivery with guardrails:** SQLAlchemy models with SQLite as the default and
  optional PostgreSQL; approval-gated SMTP; Gaussian jitter; daily cap; sending
  window; suppression re-checks; one live attempt per recipient and registrable
  domain; an immutable outbox and attempt ledger. `sent` means "accepted by the
  relay", not "delivered to the inbox".
- **Unattended operation:** one-shot Docker stages driven by Windows Task
  Scheduler, with human-only approval, cooperative SIGTERM, a run ledger and
  nightly SQLite snapshots.

## Legal & ethical constraints

- **Poland:** Article 398 of the *Prawo komunikacji elektronicznej* (PKE, in
  force 10 November 2024) requires prior consent before commercial information
  is sent by electronic means. It covers companies, named employees and sole
  traders; a published address is not consent and there is no soft opt-in. The
  code marks `PL` as `consent_required` and refuses it. Do not add a bypass.
- **Other countries:** the `forgesmith.outreach` table is a starting point, not
  legal advice. Unknown countries are refused rather than defaulted to allowed.
  Verify the current rules before enabling one.
- **GDPR:** contact data can still be personal data. Keep provenance, minimize
  storage, honour objections and maintain suppressions. Never publish real lead
  data, outboxes or the SQLite database.
- **Scraping:** respect robots.txt, terms of service, rate limits and access
  controls. OLX restricts automated access; keep that adapter opt-in and prefer
  official APIs or open data such as Overture. Never bypass a block or a login.
- **Portfolio use:** publish the engineering, not a "spam machine". Do not claim
  clients, revenue or delivery rates the project has not measured.

## Roadmap & future iterations

- [x] **Phase 1: Extraction:** Shopify, OLX (terms permitting) and Overture
  local extracts. Database schema and lead lifecycle shipped with Phase 1.
- [x] **Phase 2: Storefront Enrichment:** Resolve domains, crawl
  home/about/contact pages politely, extract role-first emails plus NIP/context,
  and persist to `enrichments`/`contacts`.
- [x] **Phase 3: LLM Prompting:** Clean and delimit scraped context, generate
  JSON drafts with local Ollama, enforce grounding/length/plain-text gates,
  persist immutable `messages`, and expose `compose`/`drafts`/`suppress`.
  `composed` means awaiting review; Phase 4 approves to `queued`.
- [x] **Phase 4: Mailer Module:** Approval (`composed -> queued`), dry-run
  `.eml` output, staged SMTP delivery with exponential backoff, Gaussian jitter,
  daily cap/sending window, suppression re-checks, a single-writer run lock, and
  manual `mark`/`unsuppress` ops.
- [x] **Phase 4.5: IMAP Ingest:** Read-only IMAP (`EXAMINE`/`BODY.PEEK`) sync
  with a UID cursor and UIDVALIDITY reset; hard 5.x DSNs auto-suppress the named
  address and bounce the lead, thread-matched replies auto-mark `replied`, while
  opt-outs and unmatched mail stay in a review ledger for explicit
  `inbox apply --yes`. No inbound text ever creates a domain suppression.
- [x] **Phase 5: Containerization:** One-shot slim/browser images, compose
  profiles that start nothing by default, SQLite + mailer lock on a named
  volume, WAL/busy-timeout, partial unique send index, run ledger,
  `status`/`doctor`/`backup`, and Windows Task Scheduler definitions in `ops/`.
  Live mail remains limited to human-approved leads; `approve` is never
  scheduled.
- [x] **Phase 6: Desktop console:** A PyQt5 front end (`uv sync --extra gui`,
  `uv run forgesmith-gui`) that renders every stage, ledger and status in one
  window. It builds the same argv and runs one-shot `python -m forgesmith`
  subprocesses, so approvals, the kill switch, dry-run gating, locks and the run
  ledger are unchanged. Read-only commands expose `--json` for the tables;
  `approve` stays a human click. The window is laid out as the pipeline itself:
  a stage strip with live per-stage counts, one "next step" card, and per-page
  blockers. Each lead row exposes exactly one next action, taken from the shared
  `forgesmith.eligibility` rule, and every stage has its one-click bulk run
  (Enrich 25/50/200/all, Retry parked, Draft N, Approve all, Dry run / Send live
  behind a recipient list).

## Quality bar

- `uv run pytest` stays green (605 offline tests at the time of writing).
- `uv run ruff check src tests` stays clean.
- Tests never touch the network; live Ollama generation, live SMTP and
  `docker build` are manual and not part of the default gate.
- New behaviour gets a test, and a renamed CLI flag cannot leave a GUI button
  dead behind it.
