# ICE CRM backend

FastAPI REST API for the ICE internal CRM. Uses PostgreSQL 16, SQLAlchemy 2 async sessions with asyncpg, Alembic, Argon2 passwords, short-lived JWTs, rotating opaque refresh tokens, server-enforced RBAC, and a persistent campaign queue.

The architecture adapts PRISM's `app/core`, database sessions, authentication dependencies, request/security middleware, and versioned router patterns. PRISM's tenant/insurance models, data, `.env`, credentials, and migrations were not copied. This backend is a single-company CRM with `Super Admin` and `BDO` roles matching the frontend.

## Local setup

Python 3.12+ and PostgreSQL 16 are required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put the generated value in `SECRET_KEY`. Set `DATABASE_URL` to your own PostgreSQL database using `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE`. Percent-encode special characters in URL passwords. Create the database first; the API never creates databases, auto-migrates, or creates default accounts.

```bash
alembic upgrade head
python -m app.cli create-admin
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Run the campaign worker in a separate terminal, with the same `.env`:

```bash
source .venv/bin/activate
python -m app.services.worker
```

Interactive API documentation: http://127.0.0.1:8000/docs. Liveness: `/health/live`. Readiness: `/health/ready` checks database and migration-table availability. Migrations, rather than readiness alone, establish the expected schema revision.

## Docker

Copy and configure `.env` first. Add a strong, URL-safe `POSTGRES_PASSWORD` to `.env` for Compose. Set `ALLOWED_HOSTS` to include the host you will use, and explicit `CORS_ORIGINS`. Then:

```bash
docker compose up --build -d
docker compose exec api python -m app.cli create-admin
```

Compose starts PostgreSQL, applies migrations once, and then starts the API and worker. Database storage persists in `crm_data`. API/database ports bind to loopback. Never remove the data volume unless intentionally discarding the database. Use TLS through your deployment proxy, backups, and a secret manager for production credentials. Proxy forwarding is disabled by default; configure trusted proxy addresses explicitly if enabling it.

## Authentication and access

`POST /api/v1/auth/login` accepts JSON `{ "email": "...", "password": "..." }`. It returns `accessToken`, `refreshToken`, `expiresIn`, `tokenType`, and a safe user object. Send `Authorization: Bearer ACCESS_TOKEN` for protected routes.

`POST /auth/refresh` accepts `{ "refreshToken": "..." }`; each token is single-use and stored as a SHA-256 digest. `POST /auth/logout` revokes all sessions, including current access tokens. `POST /auth/password` accepts `currentPassword` and `password` and revokes all sessions. Sign in again after changing a password. Deactivation, role changes, and assignment changes revoke user sessions. `GET /auth/me` returns current identity and product assignments.

There is no public registration or default password. Only an administrator can create staff accounts. There is no self-service password recovery/email invitation flow in this version.

Auth uses explicit bearer headers rather than cookies; CSRF cookie middleware from PRISM is therefore not applicable. Keep access/refresh tokens out of URLs and logs, and use memory or a carefully designed secure session layer in the frontend. Request validation responses omit submitted values. CORS, trusted-host checks, request size limits (2 MB), security headers, correlation IDs, and per-process authentication rate limiting are included. For multiple API replicas, enforce shared rate limits at your gateway; the in-process limiter is not a distributed lockout service. Incoming forwarding headers and request IDs are not trusted.

Super Admin can administer users, assignments, products, contacts, senders, and campaigns. BDOs can manage records only through relationships they own on assigned products. Product assignment alone does not expose another BDO's contacts. Contacts are unique people shared across product relationships, so editing an accessible person's details updates their single shared contact record. Lifecycle stages/owners remain relationship-specific. Unknown and inaccessible records both return 404. Request schemas reject extra fields, preventing role/owner/status mass assignment.

## API surface

All routes below are under `/api/v1`. JSON uses camelCase to match frontend field names; snake_case is accepted on input. IDs are server-generated UUID strings. Full input schemas and response examples can be explored in `/docs`.

| Area | Routes |
| --- | --- |
| Workspace | `GET /workspace` |
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/password`; `GET /auth/me` |
| Users | `GET/POST /users`; `PUT /users/{id}` (profile, role, activation, product assignments) |
| Products | `GET/POST /products`; `PUT/DELETE /products/{id}` |
| Contacts | `GET/POST /contacts`; `GET/PUT/DELETE /contacts/{id}` |
| Import/export | `POST /contacts/import`, `/contacts/bulk-tags`; `GET /contacts/export` |
| Relationships | `GET /associations`; `POST /contacts/{id}/associations`; `PUT /associations/{id}/owner`; `DELETE /associations/{id}` |
| Lifecycle | `POST /associations/{id}/stage`; `GET /leads`, `/clients` |
| Follow-ups | `GET /follow-ups`; `POST /associations/{id}/follow-ups`; `PUT /follow-ups/{id}` |
| Reporting | `GET /dashboard`, `/activities` |
| Senders | `GET/POST /senders`; `PUT /senders/{id}` |
| Signatures | `GET/POST /signatures`; `DELETE /signatures/{id}` |
| Audiences | `GET/POST /audience-segments`; `DELETE /audience-segments/{id}` |
| Campaigns | `GET/POST /campaigns`; `GET/PUT/DELETE /campaigns/{id}` |
| Campaign actions | `POST /campaigns/{id}/duplicate`, `/launch`, `/cancel`, `/preview/{contactId}` |
| Campaign results | `GET /campaigns/{id}/recipients`, `/analytics` |

Contacts support `q`, `productId`, `stage`, `offset`, and `limit`; the response is `{items,total,offset,limit}`. Campaigns support `status`, `offset`, `limit`; the response is `{items,total}`. Recipients support pagination. Contact exports cap results at 10,000 and protect spreadsheet formula cells. Import accepts `{rows: ContactCreate[]}` from the frontend's CSV column mapper, at most 1,000 rows within the request size limit. Duplicate rows are skipped with row numbers; validation/access failures roll back the request. Imports never overwrite existing people.

Unused products can be deleted. Products with CRM history must be archived. A contact's final relationship and relationships with lifecycle/follow-up history cannot be removed. Only administrators can delete contacts; dependent lifecycle records cascade, while audit descriptions and recipient snapshots remain. Lead conversions preserve lead history and add one linked client. Converted client stages cannot be reverted. Follow-up updates synchronize the lead's nearest open follow-up date. Audit changes are committed in the same transaction as mutations.

## HTML signatures

`POST /signatures` accepts `{name,html,sourceFileName}` from the frontend import dialog. Full HTML documents are converted to fragments; body inline typography, colors, safe spacing, tables, links and images are retained. Bleach sanitizes HTML and inline CSS server-side. Scripts, events, unsafe URLs, positioning, forms, and external styles are removed. Personalised previews are sanitized again after substitution.

The HTML limit is 900,000 characters within the 2 MB request limit. Images may be HTTPS or PNG/JPEG/GIF/WebP base64 data URLs with recognizable file headers, at most 250 KB each. Local/relative/`cid:` images are rejected: the frontend must replace them with uploaded images or omit them explicitly. SMTP delivery converts embedded images to related CID MIME attachments. HTTPS images remain external; the server does not fetch them. Signature uploads are account-owned, and referenced signatures cannot be deleted. Signatures are snapshotted when a campaign is submitted.

## Campaign execution

Drafts can be edited, duplicated and deleted. Launch requires a verified sender, active product/owner, subject, body, eligible recipients and explicit confirmation:

```json
{ "confirm": true, "mode": "simulation", "scheduledAt": "2026-12-01T10:00:00+02:00" }
```

Omit `scheduledAt` for immediate processing. Schedules must contain a timezone and are stored as PostgreSQL timezone-aware timestamps. Launch is protected by row locking; simultaneous submissions cannot enqueue duplicates. Recipients must have consent with provenance, no do-not-email suppression, and an accessible relationship. Consent, suppression, current address, owner/product assignment, and sender authorization are rechecked by the worker immediately before processing.

Simulation produces `Simulated` results and sends nothing. For real SMTP, configure `SMTP_HOST`, port, TLS mode and credentials, set `EMAIL_DELIVERY_ENABLED=true`, configure a sender through the admin API, and launch with `mode: "smtp"`. `Verified` is an administrator assertion that the address/domain is authorized by your SMTP provider; this version does not automate provider domain verification. SMTP credentials are environment secrets and never returned by sender routes. This version uses one deployment SMTP transport with multiple authorized sender identities; OAuth/API providers and separate transport credentials per sender remain future integrations.

The PostgreSQL worker uses row locks with `SKIP LOCKED`, so it can run separately from the API. Queued jobs survive API/worker restarts. SMTP acceptance is recorded as `Accepted`, not inbox delivery. There are no invented opens/clicks. Dispatching is committed before SMTP, and uncertain results become `Unknown` with no automatic retry, preventing blind duplicate sends after timeouts or crashes. Stale dispatches become Unknown after five minutes. Check provider logs before any manual resend. Cancellation excludes remaining queued recipients; an already-dispatching message may still finish. There is a small unavoidable interval between eligibility validation and external transport acceptance.

## Testing

Tests require a disposable PostgreSQL database **named `crm_test`**. They truncate application tables. Never point tests at a real CRM database.

```bash
createdb crm_test
SECRET_KEY=test-only-secret-key-with-more-than-32-characters \
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/crm_test alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/crm_test pytest -q
```

The suite covers JWT/password validation, refresh rotation/revocation, RBAC and ownership isolation, duplicates, consent, lifecycle/follow-up history, assignment revocation, HTML sanitization, import/export, schedules/cancellation, concurrent launch, and simulation/SMTP outcomes. SMTP is mocked; no real email is sent. No Playwright is needed or run.

## Frontend integration

The React app in `../crm_frontend` uses this backend through `src/services/http.ts`, `api.ts`, and `campaign-service.ts`. There is no runtime mock database or seeded dataset. The Vite development proxy forwards `/api` to `http://127.0.0.1:8000`; production deployments must route `/api` or set `VITE_API_URL` and explicit CORS/host settings.

`GET /api/v1/workspace` supplies a repeatable-read, read-only snapshot of the current user's accessible collections. Administrators receive the staff directory; BDOs receive their own identity only, plus their assigned products and owned relationships. Contacts, campaigns, recipient logs, signatures, senders, segments, follow-ups, lifecycle history and activity are scoped server-side. Password hashes and session metadata are never returned. The frontend preserves genuinely empty collections rather than inserting samples.

The frontend uses dedicated routes for writes, including `PUT /contacts/{id}/notes`, `PUT /follow-ups/{id}/status`, and atomic `POST /contacts/bulk-delete`. Stage updates accept optional `followUp`, `notes`, and `conversionDate`; existing opportunity values are preserved when no value is supplied. The frontend admin screens manage real users, assignments, activation and sender identities. Campaign launch explicitly chooses SMTP or server simulation, and the frontend reports actual queue/SMTP outcomes. The previous fake test-email action is now a server preview validation call; it sends no email.

The initial administrator must be created with the CLI. Start with your own products, accounts and contacts. No browser demo records are migrated to PostgreSQL. Full snapshots suit the current internal workspace; larger deployments should switch individual screens to the existing paginated list APIs.

An optional frontend integration test uses native fetch against a running disposable API, PostgreSQL database and worker. See the frontend README for `CRM_LIVE_API_URL`, `CRM_TEST_EMAIL` and `CRM_TEST_PASSWORD`. It creates test records and a server simulation campaign, without a browser, Playwright, or real SMTP sending. Never run it against production.

## Remaining provider integrations

General campaign file attachments/object storage, provider delivery/bounce/open/click webhooks, OAuth, real campaign test-email delivery, self-service recovery/invitations, configurable pipeline stages, and expanded chart aggregation endpoints are not implemented. Unsupported attachment controls are removed from the frontend. Uploaded HTML and embedded signature images are supported. PostgreSQL is the only supported database; tests exercise its real schema rather than a SQLite approximation.
