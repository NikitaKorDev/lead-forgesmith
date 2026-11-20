# Forgesmith — Phase 2: Enrichment

**Status:** implemented. This file is the historical design record from before
implementation; the README is the current source of truth.
**Goals source:** `AGENTS.md` (there is no `goals.md` on disk) + `README.md`
**Baseline:** 51 leads — 37 Shopify (all have a domain), 14 OLX (all have `domain = NULL`;
0/14 expose `external_url`; 5/14 expose a shop subdomain). 0 contacts, 0 events.

> **Phase-numbering note.** `AGENTS.md` labels its roadmap differently ("Phase 1: Extraction &
> Enrichment", "Phase 2: Database Integration"). Database integration already shipped in Phase 0
> (`leads`, `contacts`, `messages`, `sends`, `suppressions`, `events`). This plan follows the
> README's numbering, where **Phase 2 = enrichment**.

---

## 1. Goal and definition of success

Resolve each lead to a real storefront, visit it politely, and persist:

1. **Observed company email(s)** — never pattern-guessed.
2. **Business identity** — legal name and NIP only when explicitly published.
3. **Business context** — ~3000 chars of main content, headings, product signals, platform.
4. **Provenance** — which page produced each fact, and which engine fetched it.

Success is **deterministic, precise enrichment**, not a high skip count:

- Every processed lead ends in a terminal, explainable outcome.
- Every stored address appears verbatim on a page of the lead's own site.
- Reruns perform no fetches and never downgrade a previously enriched lead.
- The suite stays offline; the first real run covers 5 Shopify + the 5 resolvable OLX leads.

**Out of scope:** sending mail, LLM calls, SMTP verification, contact forms, search-engine
scraping, login-gated content, founder/person extraction, KRS/REGON, socials, phone numbers,
Postgres-specific migrations.

---

## 2. Current state and constraints

| Piece | Where | Consequence for Phase 2 |
|---|---|---|
| `PoliteClient` | `http.py:63-89` | Returns `None` for every error; no status/headers. Needs an additive `fetch()` for enrichment; `get()` stays for sources. |
| `BrowserFetcher` | `browser.py:88-113` | Stops at `domcontentloaded`, snapshots immediately → JS emails missed. Returns `None` on errors. Launches in `__aenter__`. |
| `Fetcher` protocol | `sources/base.py:18-23` | Returns `Any`; no normalized result type. |
| `Lead.raw` refresh | `repo.py:128-140` | Replaces `raw` wholesale → enrichment must **not** live in `Lead.raw`. |
| `Lead.domain` refresh | `repo.py:14` | Not in `_MUTABLE_FIELDS` → a promoted domain is not clobbered by source upserts. |
| `Lead.platform` | `shopify.py:140`, `olx.py:205` | Means *source* platform. The detected storefront technology needs its own column. |
| `olx.py` raw | `olx.py:191-201` | `externalUrl` is already normalized into `Lead.domain`; `raw.ad` only has `external_url` for evidence. |
| `OLX Lead.name` | `olx.py:175-177` | Often a person's first name (`Maciej`, `Łukasz`) → never use as legal name. |
| `Contact.kind` | `models.py:82` | Defaults to `"company"`; new writes use `role|personal|other`. |
| SQLite FKs | `session.py:33-39` | `PRAGMA foreign_keys` is off; do not rely on DB cascades in dev. |
| `utils.strip_tags` | `utils.py:24-25` | Leaves script/style text → use an `HTMLParser` for enrichment. |

---

## 3. Architecture

```
pending leads (status=new, attempts < max)
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│ EnrichmentRunner — sequential leads, sequential pages      │
│  claim attempt → resolve domain → crawl → extract → save   │
└────────┬───────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐   no domain   ┌─────────────────────────┐
│ SiteFetcher      │──────────────►│ Domain resolver         │
│ HTTP first,      │               │ lead.domain → overrides │
│ sticky engine    │               │ → shop subdomain →      │
│ per host         │               │ (--deep-resolve) offer  │
└────────┬─────────┘               └────────────┬────────────┘
         │ pages                                │ domain evidence
         ▼                                      ▼
┌───────────────────┬───────────────────┬─────────────────────┐
│ Email extractor   │ Identity           │ Context             │
│ visible text only │ NIP + legal name   │ 3k main text,       │
│ eligibility+rank  │ explicit evidence  │ headings, products  │
└─────────┬─────────┴─────────┬─────────┴──────────┬──────────┘
          ▼                   ▼                    ▼
    contacts table      enrichments table     enrichments.context
          └──────────────► status + event ◄─────────┘
```

**Package layout**

```
src/forgesmith/
  db/models.py                 # + Enrichment ORM (imported before create_all)
  db/repo.py                   # + EnrichmentRepo, LeadRepo.select_pending()
  domain.py                    # + registrable_domain(), same_site()
  enrichment/
    __init__.py                # enrich_pending(), EnrichmentResult
    models.py                  # FetchResult, PageSnapshot, EmailHit, EnrichmentResult
    fetch.py                   # SiteFetcher: HTTP-first, sticky engine, robots, SSRF
    resolve.py                 # domain resolution + overrides
    site.py                    # visible text, excerpt, link ranking, platform, NIP/legal
    emails.py                  # extraction, eligibility, ranking
    runner.py                  # orchestration + persistence
```

**CLI:** `forgesmith enrich`, `forgesmith enrich --resolve-only`, `forgesmith contacts`.

**Concurrency:** 1. Per-host pacing (`request_delay=1.5`) serializes same-host requests anyway;
intra-site or per-lead concurrency buys no throughput and adds transaction/locking bugs.
Sequential is a deliberate Phase 2 decision; revisit only if real runs prove slow.

---

## 4. Data model

### 4.1 `Enrichment` (1:1 with `leads`) — defined in `db/models.py`

```python
class EnrichmentStatusDB(StrEnum):
    OK = "ok"                    # site crawled, >=1 contact stored
    NO_SITE = "no_site"          # domain unresolved or site unreachable
    NO_CONTACT = "no_contact"    # site crawled, nothing eligible observed
    BLOCKED = "blocked"          # challenge/403 after allowed attempts
    ERROR = "error"              # transient failure; lead stays new until max attempts

class Enrichment(Base):
    __tablename__ = "enrichments"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_enrichments_lead_id"),)

    id: int
    lead_id: FK leads.id, unique
    status: str
    domain: str | None               # resolved storefront domain
    domain_source: str               # source | override | shop_page | offer_page | none
    site_url: str | None             # final homepage actually crawled
    storefront_platform: str | None  # woocommerce | shopify | shoper | wix | ...
    legal_name: str | None
    nip: str | None                  # 10 digits, checksum-validated
    description: str | None          # meta/og description, site's own wording
    context: JSON                    # text_excerpt, headings, products, pages
    evidence: JSON                   # emails, rejection reasons, snippets, resolution method
    engine: str | None               # http | browser | mixed
    attempts: int = 0
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    created_at / updated_at
```

Rules:

- **Must be in `db/models.py`**, not `enrichment/models.py`, and imported by the time
  `init_db()` runs. `create_all` only creates models registered on `Base.metadata`;
  a model hidden in the enrichment package would silently never be created.
- No changes to existing tables → the current SQLite DB gets the new table without Alembic.
- No `run_id` (no `runs` table; write-only UUID). No `final_url`/`score` unless consumed.
- `--resolve-only` writes `enrichments.domain`/`domain_source` only; it does **not** promote to
  `Lead.domain` and does not change lead status.
- `enrich` promotes `Lead.domain` only after the homepage passes a business check (HTTP 200,
  non-parked, some content). A human override may be promoted immediately.
- Duplicate resolved domain: record `evidence.reason = "duplicate_domain"`, leave
  `Lead.domain = NULL`, status `no_site`; do not create a second outbound target.

### 4.2 Contacts

Reuse `contacts`:

- `kind`: `role`, `personal`, `other`. Free-provider status is **not** a kind; it is a scoring
  signal in `evidence`.
- `confidence` is a coarse ordinal, not a probability:
  `1.0` role on the lead's domain, `0.8` role on another/free domain, `0.5` personal fallback.
- `source_url` = page where the address was observed verbatim.
- `verified_at = NULL` means **unverified**, not verified. Phase 4 must not treat NULL as valid.
- Cap 3 stored contacts per lead.
- Check the `suppressions` table for email **and** domain before storing.
- `--force`: upsert observed contacts (update confidence/source_url), keep previously observed
  addresses. Do not delete/reinsert — `messages.contact_id` can be orphaned and there is no
  `active`/`updated_at` column. Document that stale addresses may remain until Phase 4.

### 4.3 Events

One `events` row per processed lead: `entity="lead"`, `action="enriched"|"skipped"`,
`detail={"emails": n, "platform": ..., "reason": ..., "engine": ..., "domain_source": ...}`.
Never log email values at info level. Failures log the exception class, not a traceback.

### 4.4 Lead status semantics

| Outcome | Lead status | Enrichment status |
|---|---|---|
| ≥1 contact stored | `enriched` | `ok` |
| Site crawled, nothing eligible | `skipped` | `no_contact` |
| No domain / duplicate / unreachable | `skipped` | `no_site` |
| Challenge/403 after attempts | `skipped` | `blocked` |
| Transient timeout/5xx | stays `new` until `attempts >= 3`, then `skipped` | `error` |
| `--force` on a previously enriched lead, transient failure | **stays `enriched`**, contacts kept | previous status kept |

`--force` selector whitelist: `{new, enriched, skipped}`. Never touch
`composed|queued|sent|replied|bounced|suppressed` — re-enriching a sent lead would send it twice
in Phase 4.

---

## 5. Domain resolution (`resolve.py`)

Order of evidence, cheapest first:

1. **`lead.domain` already set** — this includes OLX `externalUrl`, which `olx.py` normalized
   into `LeadCandidate.domain`. Do not read `raw.ad.external_url` as a primary branch; it is
   evidence/provenance, and on this dataset it is empty.
2. **Manual overrides** — `data/domain_overrides.csv` with `lead_id,domain,note`. Checked into
   git (no secrets). Human-asserted domains may commit immediately.
3. **Shop subdomain** — `raw.ad.shop_subdomain` → `https://<sub>.olx.pl` → extract a website
   link from the shop page. This is the real path for 5/14 current leads.
4. **Offer page** — behind `--deep-resolve`, default **off**. The remaining 9 leads have no
   `external_url` and no `shop_subdomain`; expected yield is near zero and it costs one browser
   page per lead.

Validation of a candidate domain:

- Reject social/marketplace/CDN/shortener hosts (`facebook.com`, `instagram.com`,
  `youtube.com`, `tiktok.com`, `allegro.pl`, `olx.pl`, link shorteners).
- Do **not** pre-validate with a separate homepage fetch; the crawler's homepage fetch is the
  validation. `--resolve-only` therefore reports candidates, not confirmed sites.
- If the domain equals another tracked lead's domain → `duplicate_domain`, skip.
- No search-engine fallback. For 14 leads, a human override file is cheaper and safer than
  scraping search results and risking a confident pitch to the wrong company.

---

## 6. Fetching (`fetch.py`)

### 6.1 Additive API

Do not change `PoliteClient.get` / `BrowserFetcher.get` — Shopify and OLX depend on them.
Add:

```python
@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int | None
    html: str
    headers: dict[str, str]
    engine: str            # http | browser
    error: str | None = None
    bytes: int = 0
    elapsed_ms: int = 0
```

- `PoliteClient.fetch()`: `follow_redirects=False`, follow ≤3 hops manually, validate each hop,
  stream with a 2 MB cap, return status/headers/error instead of `None`.
- `BrowserFetcher.fetch()`: `goto(wait_until="domcontentloaded")`, then
  `wait_for_load_state("load", timeout=3000)` swallowed on timeout; no `networkidle` (30s stalls
  on ad-heavy shops).
- `BrowserFetcher.ensure_started()`: lazy, lock-protected start; one browser per run. Phase 2 is
  sequential, so this is a 5-line guard, not a scheduler.

### 6.2 SiteFetcher behavior

- **Sticky engine per host for the run**: if the homepage needs Chromium, every later page on
  that host uses Chromium. Per-page escalation would re-hit a host that already said no.
- **Escalate only on**: (a) homepage HTTP failure/403/429/503, or (b) challenge/JS-shell HTML.
  Do **not** escalate merely because no email was found. Drop cfemail decoding from MVP.
- **robots.txt**: `enrich_respect_robots=true`; fetch with `PoliteClient`, parse with
  `RobotFileParser.parse()` (not blocking `read()`); unreachable robots → allow.
- **Timeout math**: with `request_timeout=20` and `max_retries=2`, one dead URL can burn ~65s.
  Inside enrichment set `max_retries=0`, `enrich_page_timeout=10`, and pass a per-lead deadline
  down so each fetch checks remaining budget. Default lead budget 60s.
- **SSRF**: reject non-http(s), `localhost`, literal private/loopback/link-local IPs, and
  validate every redirect hop. Without this, a scraped URL can hit the Docker/cloud metadata
  endpoint. If deferred, the overrides file must be the only source of domains.
- **Payload**: 2 MB cap, content-type check, strip `<script>/<style>/<noscript>` before parsing.
- **Politeness**: extract a shared `HostPacer` (from `http.py:55-61` / `browser.py:80-86`) so
  HTTP and browser requests to the same host share one delay. No form posts, no auth, GET only.

---

## 7. Extraction

### 7.1 Page selection (`site.py`)

Budget `enrich_max_pages=5`, homepage first, then ranked same-site links:

| Priority | Href/anchor match |
|---|---|
| 1 | `kontakt`, `contact`, `napisz`, `skontaktuj` |
| 2 | `o-nas`, `o firmie`, `about`, `firma`, `historia` |
| 3 | `regulamin`, `polityka-prywatnosci`, `rodo` (NIP/legal name only) |
| 4 | `sklep`, `produkty`, `oferta` (context only) |

Exclusions: cart/checkout/account/login, blog, pagination/facets, files, external hosts,
`mailto:`/`tel:`. Deduplicate by path. Use `registrable_domain` (built-in suffix tuple for
`com.pl`, `net.pl`, `org.pl`, `shop.pl`, etc.; no `tldextract` — it downloads a suffix list and
breaks offline tests).

### 7.2 Email extraction (`emails.py`)

Extract from **visible text only**, using an `HTMLParser` subclass that:

- skips `script/style/noscript` and elements with `hidden`, `aria-hidden=true`,
  `display:none`, or `type="hidden"`;
- inserts separators at block-level boundaries (prevents `STORE@MISBH.COM` + `WHOLESALE`
  concatenation);
- decodes HTML entities before matching.

Sources: visible text, `mailto:` (split multiple addresses, strip `?subject=`), and JSON-LD
`Organization.email` / `ContactPoint.email`. Skip Cloudflare `data-cfemail` in MVP.

Regex with a trailing boundary is mandatory:

```
(?<![\w.%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?
(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+(?![\w.\-])
```

Plus structural validation: labels non-empty, no `..`, TLD 2–24 alpha/punycode, reject file
extensions. Do **not** whitelist TLDs: the dataset contains `.shop`, `.ma`, `.co`, and a
whitelist silently drops real leads. The trailing boundary — not the TLD check — is what kills
`STORE@MISBH.COMWHOLESALE`.

**Eligibility rules (gate), before scoring:**

- observed verbatim on a page of the lead's site;
- not vendor/infra (`sentry.io`, `wixpress.com`, `myshopify.com`, `shopify.com`, `shoper.pl`,
  `cloudflare.com`, `example.com`) and not a file name;
- not forbidden role (`no-reply`, `noreply`, `postmaster`, `abuse`, `webmaster`, `hostmaster`);
- local part non-empty, not a hash/UUID;
- either same registrable domain **or** a recognized role local part on another domain;
- not in `suppressions` (email or domain).

**Ranking (ordering only, never a pass/fail threshold):**

1. Primary roles: `sales`, `kontakt`, `info`, `biuro`, `sklep`, `sprzedaz`, `zamowienia`,
   `office`, `hello`, `sekretariat`.
2. Secondary roles: `bok`, `obsluga`, `reklamacje`, `ksiegowosc`, `press`, `marketing`.
3. Personal address (only if no role address exists).

Store at most 3 primary roles; if none, 1 secondary; if none, 1 personal fallback. Free providers
(`gmail`, `onet`, `wp`, `interia`, `o2`) remain eligible with `confidence=0.8` when the local
part is a role — small Polish firms commonly use `kontakt@gmail.com`. This is the single biggest
correction to the original weighted-threshold design, which dropped that address while keeping
a personal address on the company domain.

Record rejected candidates with a reason in `evidence` (not in `contacts`).

### 7.3 Business identity (`site.py` / `identity.py`)

- **NIP**: optional `PL` prefix, regex `\b(?:PL)?\d{3}[- ]?\d{3}[- ]?\d{2}[- ]?\d{2}\b`,
  checksum weights `6,5,7,2,3,4,5,6,7`. Accept only checksum-valid values with an explicit
  "NIP" label nearby. This is a cheap identity check, not a Phase 3 requirement; ship it last.
- **Legal name**: only from JSON-LD `Organization.legalName`, or a legal form
  (`Sp. z o.o.`, `sp.k.`, `S.A.`, `sp. j.`) adjacent (≤80 chars) to a valid, labelled NIP.
  **Never fall back to `Lead.name`** — 6/14 OLX names are first names. Keep `Lead.name` for
  Phase 3 salutation only.
- **Removed from scope:** founder, KRS, REGON, phone, socials, opening hours, categories,
  language detection, cfemail, `--resolve-search`.

### 7.4 Context (`site.py` → `enrichments.context`)

- `text_excerpt`: ≤3000 chars of cleaned **main content** from homepage + about page only
  (not `/kontakt` or `/regulamin` — those are addresses and legal boilerplate). Strip
  `nav/header/footer/aside/script/style/noscript` and obvious cookie/consent blocks; insert
  paragraph breaks; deduplicate.
- `headings`: up to 20 `h1`/`h2` strings.
- `products`: reuse Shopify `raw.products` (already in the DB); otherwise product titles/links
  found on the homepage, capped at 12.
- `description`: `meta description` / `og:description`.
- `pages`: URLs fetched, status, engine, byte count — provenance only.
- Never store raw HTML.

---

## 8. Orchestration (`runner.py`)

```
EnrichmentRunner(fetcher, repo, settings)
  async run(limit, source?, lead_id?, force?, resolve_only?, deep_resolve?)

    leads = repo.select_pending(...)          # status new, attempts < max
    for lead in leads:                        # sequential
        repo.claim_attempt(lead.id)           # short transaction, before I/O
        result = await self.enrich_one(lead)  # no DB session held open
        repo.finish(lead.id, result)          # one session_scope per lead
```

- **Selection order**: `ORDER BY attempts, id`. Do not order by `last_attempt_at` (NULL ordering
  differs between SQLite and Postgres).
- **Claim before network I/O** (`attempts += 1`, `last_attempt_at`), so Ctrl-C still advances
  attempts and a poison lead eventually reaches `skipped`.
- **Domain-claim set** in memory for the run: skip a second lead resolving to the same domain.
- **`--limit` starvation** is accepted for 51 leads; the alternation of attempts/id means failed
  leads do not permanently occupy the front of the queue.
- **Failure isolation**: catch per lead, truncate `last_error`, never abort the run.
- **Per-lead deadline** passed into `SiteFetcher`; pages stop when budget is exhausted, and any
  emails already found are kept (partial success, not `error`).
- **No run lock in MVP** (single operator, single process). Phase 5's scheduler owns locking.
- **Progress**: Rich status line + final summary table; non-zero exit only for config/DB errors.
- **RunStats**: `scanned, resolved, enriched, skipped, no_site, no_contact, blocked, failed,
  emails, duration`.

`--resolve-only` flow: resolve domain (lead.domain → overrides → shop page → optional offer
page), write `enrichments.domain/domain_source`, keep lead status `new`. Report
`resolved / overridden / unresolved / duplicate` counts.

---

## 9. Configuration (new keys in `config.py` + `.env.example`)

```
FORGESMITH_ENRICH_MAX_PAGES=5
FORGESMITH_ENRICH_MAX_ATTEMPTS=3
FORGESMITH_ENRICH_LEAD_TIMEOUT=60
FORGESMITH_ENRICH_PAGE_TIMEOUT=10
FORGESMITH_ENRICH_MAX_BYTES=2000000
FORGESMITH_ENRICH_TEXT_CHARS=3000
FORGESMITH_ENRICH_EMAIL_LIMIT=3
FORGESMITH_ENRICH_BROWSER_SETTLE_MS=3000
FORGESMITH_ENRICH_RESPECT_ROBOTS=true
FORGESMITH_ENRICH_DOMAIN_OVERRIDES=data/domain_overrides.csv
```

Reuse `request_delay`, `request_timeout`, `user_agent`. Add pydantic bounds
(`max_pages >= 1`, `0 <= text_chars`, positive timeouts). Engines: `auto|http|browser`
(default `auto`). No `enrich_extract_people`.

---

## 10. CLI surface

```
forgesmith enrich [--limit N] [--source shopify|olx] [--lead-id N] [--force]
                  [--engine auto|http|browser] [--resolve-only] [--deep-resolve]
forgesmith contacts [--lead-id N] [--limit N]
forgesmith leads --status enriched
forgesmith stats
```

- `contacts` columns: `lead_id, email, kind, confidence, source_url`.
- `enrich` summary columns: `resolved, enriched, skipped, no_site, no_contact, blocked, failed`.
- `--resolve-only` before any full crawl: `forgesmith enrich --source olx --limit 14 --resolve-only`.

---

## 11. Testing strategy (offline, no network)

| File | Cases |
|---|---|
| `tests/test_enrich_emails.py` | `STORE@MISBH.COMWHOLESALE` regression, free-provider role kept, personal-only fallback, vendor/forbidden roles, hidden honeypot, script/style text, multiple `mailto:`, `[at]/[dot]`, `&#64;`, trailing punctuation, cap 3, rejects recorded |
| `tests/test_enrich_site.py` | `HTMLParser` visible text, nav/footer stripping, 3000-char cap, link ranking, exclusions, `com.pl`/subdomain matching, platform markers (Woo/Shopify/Presta/Shoper/Wix) |
| `tests/test_enrich_identity.py` | NIP checksum with `PL` prefix, accept/reject, legal name only near valid NIP, **no `Lead.name` fallback**, label required |
| `tests/test_enrich_fetch.py` | `FetchResult` status/headers/final_url, 403 vs timeout vs 200, sticky engine per host, robots allow/disallow, 2 MB cap, SSRF literal IP + redirect hop, browser settle fallback |
| `tests/test_enrich_resolve.py` | `lead.domain` shortcut, override CSV, shop subdomain, offer page only under `--deep-resolve`, social/marketplace reject, duplicate domain, unresolved → `no_site` |
| `tests/test_enrich_runner.py` | happy path (fake fetcher → contact + enriched + event), no contact → skipped, blocked → attempts/max → skipped, transient stays new, partial timeout keeps found emails, `--force` keeps enriched on simulated failure, rerun zero fetches, duplicate domain in one run, schema creation |
| `tests/test_enrich_repo.py` | `claim_attempt`, `finish`, contact upsert, suppression check, `Lead.domain` promotion, `select_pending` ordering/filters |
| Playwright smoke | `skipif` no browser; not a gate |

Extend `tests/conftest.py` with a `FakeSiteFetcher` that records calls, returns per-URL
`FetchResult`s, models failures/engine escalation and robots — a URL→Page dict is not enough.
DB tests reuse the `db` fixture. No `respx` required for enrichment units, but `polite.fetch()`
gets respx tests.

**Quality gates:** `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` clean;
no network in the suite.

---

## 12. Milestones (vertical slices, each testable)

| # | Deliverable | Acceptance |
|---|---|---|
| M2.1 | `db/models.py` + `db/__init__.py` + `EnrichmentRepo` + config | `db init` creates `enrichments`; claim-before-I/O, force-keeps-enriched, contact upsert tested |
| M2.2 | `domain.registrable_domain`, `HostPacer`, `PoliteClient.fetch` | respx tests distinguish 403/timeout/200 and `final_url`; `com.pl` suffix cases pass |
| M2.3 | `BrowserFetcher.fetch` + lazy `ensure_started` + `SiteFetcher` | sticky engine, robots, SSRF, byte cap, settle wait; smoke test skipped by default |
| M2.4 | `site.py` + `emails.py` | MISBHV fixture: no `.COMWHOLESALE`; role-first, cap 3; platform/platform detection; 3000-char excerpt |
| M2.5 | `runner.py` + CLI + events | `enrich --limit 10` terminal rows, per-lead commits, rerun zero fetches, force guard |
| M2.6 | `resolve.py` + overrides + OLX fixtures | 5 shop-subdomain leads (ids 38, 40, 42, 43, 46) resolved or explicit `no_site`; no guessed domains |
| M2.7 | `identity.py` (NIP only) + hardening + docs | NIP checksum tests; README/AGENTS updated; first real 10-lead run reviewed |

**Build order:** schema → fetch result/pacer → fetch layer → site/emails → runner/CLI →
resolution → identity last. Identity ships **empty** if it delays the slice.

**First real run cohort:** 5 Shopify including `misbhv.pl` (large brand, likely Cloudflare) and
`nago.com` (non-`.pl`) plus 3 ordinary `.pl` shops, and the 5 OLX leads with `shop_subdomain`.

**Pass criteria for that run:**

- 10/10 terminal rows, 0 unhandled exceptions.
- ≥3 Shopify leads with ≥1 contact; all 5 OLX leads resolved with evidence or explicit `no_site`.
- Every stored address appears verbatim on the lead's own site (manual review).
- Rerun performs 0 fetches.
- `--force` with a simulated timeout keeps an enriched lead and its contacts.
- Offline suite and ruff gates green.

Yield is **reported, not gated**: a 60% Shopify yield is a property of the data and Cloudflare,
and gating it pressures the email rules to loosen. Precision and determinism are the gates.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Email regex false positives (`STORE@MISBH.COMWHOLESALE`) | visible text + block separators + trailing boundary + structural validation |
| `kontakt@gmail.com` dropped by score | eligibility gate + role-first ordering; free provider only affects confidence |
| Wrong company → confident wrong pitch | strict evidence + manual overrides; no search fallback; verbatim-on-own-site review |
| OLX no-domain leads (9/14) | explicit `no_site`; `--deep-resolve` opt-in; overrides file |
| JS-hydrated emails missed | sticky browser escalation only for challenge/JS shell; capped `load` wait |
| Timeout math blows the lead budget | `max_retries=0`, 10s/page, per-fetch deadline, partial results kept |
| Playwright resource use on Windows | one lazy browser per run, sequential pages, close in `finally` |
| SSRF via scraped hosts/redirects | scheme/host/IP validation per hop; no metadata endpoints |
| Re-enriching a sent lead | `--force` whitelist `{new, enriched, skipped}` |
| Stale contacts after `--force` | keep previously observed addresses, upsert confidence; no delete (avoids orphaned messages); Phase 4 adds `active` |
| SQLite single writer | sequential leads, one `session_scope` per lead, network outside transactions |
| `create_all` silently skips the table | model lives in `db/models.py`; test on a fresh DB |
| Lead name used as company name | hard rule: never derive legal name from `Lead.name` |

---

## 14. Architecture decisions (resolved after the debate)

| # | Decision | Verdict | Why |
|---|---|---|---|
| 1 | Enrichment storage | **Separate `enrichments` table**, ORM in `db/models.py` | `Lead.raw` is replaced wholesale on source refresh; `create_all` needs the model imported; no Alembic |
| 2 | Fetch strategy | **HTTP-first, sticky engine per host**, browser only on failure/challenge | most sites are server-rendered; per-page escalation re-hits blocked hosts |
| 3 | Domain resolution | **strict evidence + manual overrides**, no search | 14 leads; wrong-domain risk is asymmetric; `externalUrl` already lives in `Lead.domain` |
| 4 | Context size | **3000 chars of main content**, homepage + about | 6000 chars of menus is worse signal than 3000 of product text |
| 5 | Founder/person data | **cut** | no Phase 2 consumer; heuristic names are worse than none; RODO surface |
| 6 | `--resolve-only` persistence | `enrichments` only; promote to `Lead.domain` after business check | `Lead.domain` is the dedupe key; poisoning it with parked pages breaks future leads |
| 7 | Concurrency | **sequential leads, sequential pages** | `request_delay=1.5` serializes per host anyway; concurrency adds session/locking bugs |
| 8 | Contact selection | **eligibility gate + role-first ordering**, cap 3, one personal fallback | a score should rank, not decide existence; protects small-shop free-provider addresses |
| 9 | Status model | **5 crawl statuses + `domain_source`** | `resolved`/`duplicate`/`low_confidence` are reasons, not crawl states |
| 10 | Yield metrics | **report yield, gate precision/determinism** | yield is a property of data/Cloudflare; gating it pressures rules to loosen |

**Deferred:** founder/people, KRS/REGON, phone, socials, opening hours, language, cfemail
decoding, search fallback, product-page crawling, Postgres migrations, run locking (Phase 5),
contact `active` flag (Phase 4).

---

## 15. Definition of done

- [ ] `db init` creates `enrichments` on the existing DB; no migration needed.
- [ ] `forgesmith enrich --limit 10` processes 5 Shopify + 5 OLX; every lead is terminal and
      explainable.
- [ ] Emails are observed verbatim, role-first, capped at 3, never synthesized.
- [ ] `forgesmith enrich --resolve-only --source olx --limit 14` reports resolved/overridden/
      unresolved/duplicate counts without changing lead status or `Lead.domain`.
- [ ] `forgesmith contacts` shows exactly what Phase 3 will use, with `source_url`.
- [ ] Suppressed emails/domains are never stored.
- [ ] Rerun performs zero fetches; `--force` is idempotent and never downgrades an enriched lead.
- [ ] 41 existing tests still pass; new enrichment tests are offline and green.
- [ ] `ruff check` / `format --check` clean.
- [ ] README + AGENTS updated; Phase 2 marked done, DB integration noted as already shipped.
