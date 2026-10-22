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
- **Deep Enrichment:** Deploy asynchronous `Playwright` to visit each target website, bypass basic bot protections, and extract verified contact emails, founder names, and business context.
- **AI Personalization Layer:** Integrate an LLM API (Gemini or OpenAI) to analyze the scraped website text and dynamically generate a tailored cold email in Polish (e.g., noting a specific product they sell and pitching a custom scraping solution).
- **Automated Delivery:** Pipe the finalized leads into a `PostgreSQL` database and utilize SMTP to send the emails at randomized intervals (Gaussian jitter) to avoid domain blacklisting.

## 🗺️ Roadmap & Future Iterations
- [ ] **Phase 1: Extraction & Enrichment:** Build the asynchronous crawler to pull business URLs and scrape contact emails.
- [ ] **Phase 2: Database Integration:** Set up the PostgreSQL schema to track lead status (e.g., `contacted`, `replied`, `bounced`).
- [ ] **Phase 3: LLM Prompting:** Write the prompt pipeline that feeds website text to the LLM to generate the customized pitch.
- [ ] **Phase 4: Mailer Module:** Build the SMTP dispatcher with exponential backoff and randomized delays.
- [ ] **Phase 5: Containerization:** Package the script in Docker to run autonomously in the background.