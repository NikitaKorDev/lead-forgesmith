# Project Goals & Roadmap

## 🎯 Primary Objective
To build an end-to-end, asynchronous client acquisition pipeline that automatically identifies target businesses, enriches their contact data, generates AI-personalized pitch emails, and dispatches them via SMTP to secure B2B freelance contracts.

## 💼 Business Value
This system replaces manual prospecting with automated infrastructure. Instead of manually cold-emailing prospects between classes and delivery shifts, this pipeline:
- Scrapes targeted B2B leads at scale.
- Eliminates manual prospecting time.
- Uses LLM context to send hyper-personalized pitches that bypass spam filters and actually get read.

## ⚙️ Technical Goals
- **Target Extraction:** Utilize Python to extract business URLs and basic metadata from local directories (e.g., Google Maps, Panorama Firm, or e-commerce aggregators).
- **Deep Enrichment:** Deploy asynchronous `Playwright` only when HTTP is blocked, and extract observed contact emails, NIP/legal name evidence, and business context (founder/KRS/phone enrichment is deferred).
- **AI Personalization Layer:** Use local Ollama by default to analyze cleaned website text and dynamically generate a grounded, formal Polish cold email (e.g., noting a specific product they sell and pitching a custom scraping solution). Scraped pages stay local unless an external provider is explicitly enabled.
- **Automated Delivery:** Persist leads in `PostgreSQL`-compatible SQLAlchemy models with SQLite as the zero-infrastructure default and the Phase 5 production store on a named Docker volume, then use SMTP to send emails at randomized intervals (Gaussian jitter) to avoid domain blacklisting.
- **Unattended Operation:** Run each pipeline stage as a one-shot Docker container driven by Windows Task Scheduler, with human-only approval, a dry-run kill switch, cooperative SIGTERM handling, run-ledger observability, and nightly SQLite snapshots.

## 🗺️ Roadmap & Future Iterations
- [x] **Phase 1: Extraction:** Build the Shopify/OLX sources that pull business URLs and basic metadata. (Database schema and lead lifecycle shipped with Phase 1.)
- [x] **Phase 2: Storefront Enrichment:** Resolve domains, crawl home/about/contact pages politely, extract role-first emails plus NIP/context, and persist to `enrichments`/`contacts`.
- [x] **Phase 3: LLM Prompting:** Clean and delimit scraped context, generate JSON drafts with local Ollama, enforce grounding/length/plain-text gates, persist immutable `messages`, and expose `compose`/`drafts`/`suppress`. `composed` means awaiting review; Phase 4 approves to `queued`.
- [x] **Phase 4: Mailer Module:** Approval (`composed -> queued`), dry-run `.eml` output, staged SMTP delivery with exponential backoff, Gaussian jitter, daily cap/sending window, suppression re-checks, a single-writer run lock, and manual `mark`/`unsuppress` ops.
- [x] **Phase 4.5: IMAP Ingest:** Read-only IMAP (`EXAMINE`/`BODY.PEEK`) sync with a UID cursor and UIDVALIDITY reset; hard 5.x DSNs auto-suppress the named address and bounce the lead, thread-matched replies auto-mark `replied`, while opt-outs and unmatched mail stay in a review ledger for explicit `inbox apply --yes`. No inbound text ever creates a domain suppression.
- [x] **Phase 5: Containerization:** One-shot slim/browser images, compose profiles that start nothing by default, SQLite + mailer lock on a named volume, WAL/busy-timeout, partial unique send index, run ledger, `status`/`doctor`/`backup`, and Windows Task Scheduler definitions in `ops/`. Live mail remains limited to human-approved leads; `approve` is never scheduled.