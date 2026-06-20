# Timber — San Diego Retail Permit Tracker

Maps consumer-facing **retail / restaurant / gas / car-wash / commercial** building
permits from the City of San Diego, focused on new ground-up developments,
redevelopments, and (flagged separately) tenant improvements.

Live: https://timber.gentz.co

## How it works
1. `scripts/build_data.py` downloads the City of San Diego Open Data permit feeds
   (current + prior year), filters to consumer-facing retail/commercial, classifies
   each project (New Construction / Redevelopment / Tenant Improvement / Demolition),
   detects status (Active / In Review / On Hold / Cancelled / Completed), and
   best-effort identifies the tenant (national-brand scan) — writing `data/permits.json`.
2. `index.html` is a static map + filterable list reading that JSON.
3. GitHub Actions (`.github/workflows/refresh.yml`) re-runs the pipeline **every Monday**,
   commits fresh data, and deploys to Cloudflare Pages. Fully automatic.

## Data source
City of San Diego Open Data Portal — "Approvals for development projects"
(updated daily): https://data.sandiego.gov/datasets/development-permits/

## Known limitations
- **Landlord** is not published in permit data. The "Permit Holder" field (applicant/
  contractor/owner-of-record) is shown instead. True ownership requires an APN→assessor lookup.
- **Tenant** is only reliable when a national brand is named in the permit; small/local
  tenants are often blank.
- **"On Hold"** depends on the city flagging a hold; shelved projects more often appear
  as Cancelled/Expired.
- City of San Diego only (county + other SoCal cities = future expansion).
