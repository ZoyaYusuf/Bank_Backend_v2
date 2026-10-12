# Backend setup

Set `DATABASE_URL`, `JWT_SECRET_KEY` (or `JWT_SECRET`), and `CORS_ORIGINS` in the hosting environment. `CORS_ORIGINS` is a comma-separated list of exact frontend origins; it defaults to localhost and the existing Vercel frontend origin. Add the deployed frontend's origin in the Render service environment settings.

## Organization verification migration

Apply `migrations/002_organization_verification.sql` to the PostgreSQL database after the existing schema and migrations. It adds the company and bank profile tables, private verification-document storage, and account review fields. Existing accounts are marked approved to preserve their access; new company and bank signups are created as pending.

Run the migration from this directory with:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_organization_verification.sql
```

New organization signups cannot create agent accounts. An existing, trusted `AGENT` account is retained as an approved agent after the migration and can review pending signups in the agent dashboard. Provision the initial agent account through a trusted database-administration process.

Uploaded verification files (PDF, PNG, or JPEG, up to 10 MB) are stored in PostgreSQL and are available only to authenticated agents through the verification dashboard.
