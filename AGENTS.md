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
- **AI Personalization Layer:** Integrate an LLM API (Gemini or OpenAI) to analyze the scraped website text and dynamically generate a tailored cold email in Polish (e.g., noting a specific product they sell and pitching a custom scraping solution).
- **Automated Delivery:** Pipe the finalized leads into a `PostgreSQL` database and utilize SMTP to send the emails at randomized intervals (Gaussian jitter) to avoid domain blacklisting.

## 🗺️ Roadmap & Future Iterations
- [x] **Phase 1: Extraction:** Build the Shopify/OLX sources that pull business URLs and basic metadata. (Database schema and lead lifecycle shipped with Phase 1.)
- [x] **Phase 2: Storefront Enrichment:** Resolve domains, crawl home/about/contact pages politely, extract role-first emails plus NIP/context, and persist to `enrichments`/`contacts`.
- [ ] **Phase 3: LLM Prompting:** Write the prompt pipeline that feeds website text to the LLM to generate the customized pitch.
- [ ] **Phase 4: Mailer Module:** Build the SMTP dispatcher with exponential backoff and randomized delays.
- [ ] **Phase 5: Containerization:** Package the script in Docker to run autonomously in the background.