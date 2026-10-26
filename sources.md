# Business Data Sources by Country

Sources for building a list of businesses (for freelance outreach). Always check each source's current terms, licence and rate limits before building on it, and verify the outreach rules for the target country (see the bottom section).

## Global / multi-country sources

| Source | What it gives you | Cost / licence | Notes |
|---|---|---|---|
| Overture Maps (places theme) | Business names, categories, addresses, websites, some phones | Free, open data (check licence per theme) | Best Google Maps replacement; downloadable and storable |
| Foursquare Open Source Places | Large global POI dataset | Free, permissive licence | Good for category filtering |
| OpenStreetMap (Overpass API) | POIs by tag and bounding box | Free, ODbL (attribution required) | Coverage varies by country; rarely has emails |
| OpenCorporates | Company register data from many countries | Free tier for some uses, paid otherwise | Legal entities, not places |
| Google Places API | Rich place data (ratings, hours) | Paid with free allowance | ToS restricts building lists/databases and limits caching; not recommended as the core of an outreach pipeline |
| Apollo / Hunter / Lusha | Emails and decision-makers | Paid, free tiers | Convenient; you stay legally responsible for use of the data |

## Country registers

| Country | Source | Data | Cost |
|---|---|---|---|
| Poland | CEIDG API | Sole proprietors, PKD codes, addresses | Free |
| Poland | KRS / REGON (GUS) | Companies, legal data, PKD | Free |
| United Kingdom | Companies House API | Companies, addresses, SIC codes, officers | Free |
| France | Sirene (INSEE) API | All businesses, NAF codes, addresses | Free |
| Czechia | ARES | Companies and sole traders | Free |
| Estonia | e-Business Register open data | Companies | Free / open |
| Denmark | CVR | Companies | Free / open |
| Norway | Brønnøysund Register Centre | Companies | Free / open |
| Finland | PRH / YTJ open data | Companies | Free / open |
| Germany | Handelsregister / Unternehmensregister | Companies | Mostly paid or limited |
| Austria | Firmenbuch | Companies | Paid / limited |
| Netherlands | KvK (Chamber of Commerce) | Companies | Partly paid |
| United States | State Secretary of State databases (per state) | Registered entities | Varies; no national register |
| Canada | Corporations Canada + provincial registers | Registered entities | Varies |

## Outreach rules, quick view (not legal advice)

| Region | Cold B2B email |
|---|---|
| Germany, Austria | Generally needs prior consent, even B2B; phone also restricted |
| France, Netherlands, Nordics | Generally allowed if relevant to the recipient's role and with opt-out |
| United Kingdom | OK to limited companies; sole traders/partnerships treated like consumers (consent needed) |
| Poland | Prior consent required since 10 Nov 2024 (art. 398 PKE), including B2B and sole traders; a published address is not consent |
| United States | CAN-SPAM: honest sender details and opt-out |
| Canada | CASL: consent required in most cases |
| EU/UK overall | GDPR applies: tell people where you got their data, offer opt-out, keep a suppression list |

> Poland is no longer a "gray area". Older guides predate the *Prawo komunikacji
> elektronicznej*, which took effect on 10 November 2024. This project's code
> refuses Poland even when it is listed in `MAIL_COUNTRIES`.

## Suggested pipeline schema

Use one common record format so each country is just a new fetcher:

```
source, source_id, name, country, city, address, category/industry_code,
website, public_contact, date_collected, date_contacted, status, opt_out
```

Keep `source`, `date_collected` and `opt_out` for every record: they cover your GDPR disclosure and suppression needs.

## Suggested starting point

Pick one or two markets (for example UK via Companies House, or any market via Overture Maps), build the first fetcher, check each company's website for a public contact, and contact only where you have a lawful basis. In Poland that means prior consent; a published address or a public register is not consent.