# ICE CRM backend

FastAPI REST API for the ICE internal CRM. Uses PostgreSQL 16, SQLAlchemy 2 async sessions with asyncpg, Alembic, Argon2 passwords, short-lived JWTs, rotating opaque refresh tokens, server-enforced RBAC, and a persistent campaign queue.

The architecture adapts PRISM's `app/core`, database sessions, authentication dependencies, request/security middleware, and versioned router patterns. PRISM's tenant/insurance models, data, `.env`, credentials, and migrations were not copied. This backend is a single-company CRM with `Super Admin` and `BDO` roles matching the frontend.

## Local setup

Python 3.12+ and PostgreSQL 16 are required.

For the automated development setup, configure `.env` first (copy `.env.example` if needed), create the PostgreSQL database, and run:

```bash
python3 run_backend.py
```

For the included isolated database, run `docker compose up -d db` first. It publishes PostgreSQL on `127.0.0.1:5433` by default, leaving an existing database on port 5432 unchanged.

The script creates or reuses `.venv`, installs requirements, applies Alembic migrations, and starts Uvicorn with reload plus the invitation/campaign mail worker in the same terminal. It works from any current directory, reads APP_HOST and APP_PORT (BACKEND_PORT fallback, default 5002), and stops if a command fails. It does not create a PostgreSQL database or demo data. The API startup creates the explicitly configured initial administrator. Alternatively, create another administrator in another terminal with `.venv/bin/python -m app.cli create-admin`. The runner already starts the mail worker; do not launch a second one for the same local session. On Windows use `.venv\Scripts\python.exe` instead. Reload uses a single API process; WEB_CONCURRENCY applies to the separate `python -m app` startup command.

The equivalent manual setup follows.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put the generated value in `SECRET_KEY`. Set `DATABASE_URL` to your own PostgreSQL database using `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE`. Percent-encode special characters in URL passwords. Create the database first; the API never creates databases or auto-migrates. It creates the explicitly configured initial Super Admin during startup.

```bash
alembic upgrade head
# Set DEFAULT_PLATFORM_ADMIN_EMAIL and DEFAULT_PLATFORM_ADMIN_PASSWORD in .env.
python -m app
# Development with reload: uvicorn app.main:app --reload --host 127.0.0.1 --port 5002
```

When using manual `python -m app`/Uvicorn startup instead of run_backend.py, run the campaign worker in a separate terminal with the same `.env`:

```bash
source .venv/bin/activate
python -m app.services.worker
```

Interactive API documentation: http://127.0.0.1:5002/docs. Liveness: `/health/live`. Readiness: `/health/ready` checks database and migration-table availability. Migrations, rather than readiness alone, establish the expected schema revision.

## Docker

Copy and configure `.env` first. Add a strong, URL-safe `POSTGRES_PASSWORD` to `.env` for Compose. Set `ALLOWED_HOSTS` to include the host you will use, and explicit `CORS_ORIGINS`. Then:

```bash
docker compose up --build -d
# API startup creates the configured DEFAULT_PLATFORM_ADMIN_EMAIL.
```

Compose starts PostgreSQL, applies migrations once, and then starts the API and worker. Database storage persists in `crm_data`. API/database ports bind to loopback. Never remove the data volume unless intentionally discarding the database. Use TLS through your deployment proxy, backups, and a secret manager for production credentials. Proxy forwarding is disabled by default; configure trusted proxy addresses explicitly if enabling it.

## Authentication and access

`POST /api/v1/auth/login` accepts JSON `{ "email": "...", "password": "..." }`. It returns `accessToken`, `refreshToken`, `expiresIn`, `tokenType`, and a safe user object. Send `Authorization: Bearer ACCESS_TOKEN` for protected routes.

`POST /auth/refresh` accepts `{ "refreshToken": "..." }`; each token is single-use and stored as a SHA-256 digest. `POST /auth/logout` revokes all sessions, including current access tokens. `POST /auth/password` accepts `currentPassword` and `password` and revokes all sessions. Sign in again after changing a password. Deactivation, role changes, and assignment changes revoke user sessions. `GET /auth/me` returns current identity and product assignments.

There is no public registration or default password. Only an administrator can create staff accounts. Staff accounts are invited by email and choose their password through a one-time link. Self-service forgotten-password recovery is not implemented.

Auth uses explicit bearer headers rather than cookies; CSRF cookie middleware from PRISM is therefore not applicable. Keep access/refresh tokens out of URLs and logs, and use memory or a carefully designed secure session layer in the frontend. Request validation responses omit submitted values. CORS, trusted-host checks, request size limits (2 MB), security headers, correlation IDs, and per-process authentication rate limiting are included. For multiple API replicas, enforce shared rate limits at your gateway; the in-process limiter is not a distributed lockout service. Incoming forwarding headers and request IDs are not trusted.

Super Admin can administer users, assignments, products, contacts, senders, and campaigns. BDOs can manage records only through relationships they own on assigned products. Product assignment alone does not expose another BDO's contacts. Contacts are unique people shared across product relationships, so editing an accessible person's details updates their single shared contact record. Lifecycle stages/owners remain relationship-specific. Unknown and inaccessible records both return 404. Request schemas reject extra fields, preventing role/owner/status mass assignment.

## API surface

All routes below are under `/api/v1`. JSON uses camelCase to match frontend field names; snake_case is accepted on input. IDs are server-generated UUID strings. Full input schemas and response examples can be explored in `/docs`.

| Area | Routes |
| --- | --- |
| Workspace | `GET /workspace` |
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/password`; `GET /auth/me` |
| Users | `GET/POST /users`; `PUT/DELETE /users/{id}` (profile, role, activation, product assignments) |
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

Simulation produces `Simulated` results and sends nothing. Real campaigns use `mode: "email"` and the configured `EMAIL_PROVIDER` (`smtp` or `brevo`). The older `mode: "smtp"` input is accepted as a compatibility alias for live email; it does not override EMAIL_PROVIDER. `Verified` is an administrator assertion that the address/domain is authorised by the selected provider; automated domain verification is not implemented. SMTP passwords and Brevo API keys stay on the server and are never returned by routes. See the provider configuration below.

The PostgreSQL worker uses row locks with `SKIP LOCKED`, so it can run separately from the API. Queued jobs survive API/worker restarts. Provider acceptance is recorded as `Accepted`, not inbox delivery. There are no invented opens/clicks. Dispatching is committed before external delivery, and uncertain results become `Unknown` with no automatic retry, preventing blind duplicate sends after timeouts or crashes. Stale dispatches become Unknown after five minutes. Check provider logs before any manual resend. Cancellation excludes remaining queued recipients; an already-dispatching message may still finish. There is a small unavoidable interval between eligibility validation and external transport acceptance.

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

The React app in `../crm_frontend` uses this backend through `src/services/http.ts`, `api.ts`, and `campaign-service.ts`. There is no runtime mock database or seeded dataset. The Vite development proxy forwards `/api` to `http://127.0.0.1:5002`; production deployments must route `/api` or set `VITE_API_URL` and explicit CORS/host settings.

`GET /api/v1/workspace` supplies a repeatable-read, read-only snapshot of the current user's accessible collections. Administrators receive the staff directory; BDOs receive their own identity only, plus their assigned products and owned relationships. Contacts, campaigns, recipient logs, signatures, senders, segments, follow-ups, lifecycle history and activity are scoped server-side. Password hashes and session metadata are never returned. The frontend preserves genuinely empty collections rather than inserting samples.

The frontend uses dedicated routes for writes, including `PUT /contacts/{id}/notes`, `PUT /follow-ups/{id}/status`, and atomic `POST /contacts/bulk-delete`. Stage updates accept optional `followUp`, `notes`, and `conversionDate`; existing opportunity values are preserved when no value is supplied. The frontend admin screens manage real users, assignments, activation and sender identities. Campaign launch explicitly chooses the configured provider or server simulation, and the frontend reports actual queue/SMTP outcomes. The previous fake test-email action is now a server preview validation call; it sends no email.

The initial administrator is created at API startup from DEFAULT_PLATFORM_ADMIN_EMAIL and DEFAULT_PLATFORM_ADMIN_PASSWORD; the CLI remains available for manual account creation. Start with your own products, accounts and contacts. No browser demo records are migrated to PostgreSQL. Full snapshots suit the current internal workspace; larger deployments should switch individual screens to the existing paginated list APIs.

An optional frontend integration test uses native fetch against a running disposable API, PostgreSQL database and worker. See the frontend README for `CRM_LIVE_API_URL`, `CRM_TEST_EMAIL` and `CRM_TEST_PASSWORD`. It creates test records and a server simulation campaign, without a browser, Playwright, or real SMTP sending. Never run it against production.

## Remaining provider integrations

General campaign file attachments/object storage, provider delivery/bounce/open/click webhooks, OAuth, real campaign test-email delivery, self-service password recovery, configurable pipeline stages, and expanded chart aggregation endpoints are not implemented. Unsupported attachment controls are removed from the frontend. Uploaded HTML and embedded signature images are supported. PostgreSQL is the only supported database; tests exercise its real schema rather than a SQLite approximation.

## Environment configuration notes

The application loads `.env` from the backend working directory. `.env.example` is a template and is not loaded automatically. Copy it to `.env`, replace the placeholder `SECRET_KEY` with a unique generated value, and set exactly one `DATABASE_URL` for the intended PostgreSQL database. The database must exist and be reachable before applying migrations. Never commit real credentials.

`python -m app` reads `APP_HOST` (default `0.0.0.0`), `APP_PORT` (default `5002`) and `WEB_CONCURRENCY` (default `1`). `BACKEND_PORT` is a compatibility fallback if `APP_PORT` is absent; `APP_PORT` takes precedence. Direct `uvicorn` commands use their explicit `--port`; they do not read `APP_PORT` automatically. Compose exposes the same APP_PORT on the host and inside the API container. The Vite proxy expects port 5002.

Compose creates the database/user named by POSTGRES_DB and POSTGRES_USER, and overrides DATABASE_URL with DOCKER_DATABASE_URL (the same credentials/database, host db and port 5432). The local runner uses DATABASE_URL with host 127.0.0.1 and POSTGRES_PORT (default 5433). Running Python directly uses your `.env` DATABASE_URL. If you intend to connect to an existing external database, use the direct startup path or adjust the Compose database override deliberately.

Extra variables from PRISM are ignored unless declared in `app/core/config.py`. `DEFAULT_PLATFORM_ADMIN_EMAIL` and `DEFAULT_PLATFORM_ADMIN_PASSWORD` create the initial Super Admin on API startup. DEFAULT_PLATFORM_ADMIN_USERNAME supplies its display name. BOOTSTRAP_SECRET is not required or consumed. `AUTO_CREATE_TABLES` does not apply migrations; run `alembic upgrade head`. `PGADMIN_*` and `MAILPIT_*` do not start services; those services are not defined in this Compose file. `LOG_DIR`, `LOG_LEVEL` and `API_V1_PREFIX` are not currently consumed; API routes remain `/api/v1`. Provider credentials are only required when real delivery is enabled.

## Switching between SMTP and Brevo

The transport follows PRISM's EMAIL_PROVIDER dispatch pattern. Set these values in the backend **`.env`**, not only `.env.example`. Do not copy PRISM credentials into this project or commit API keys. Restart both `python -m app` and `python -m app.services.worker` after changing configuration (or recreate both Compose services). The selected provider processes all queued live campaigns; there is no automatic failover to another provider.

For SMTP:

```dotenv
EMAIL_PROVIDER=smtp
EMAIL_DELIVERY_ENABLED=true
SMTP_HOST=your-smtp-host
SMTP_PORT=587
SMTP_USERNAME=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_STARTTLS=true
SMTP_SSL=false
```

For Brevo's transactional API:

```dotenv
EMAIL_PROVIDER=brevo
EMAIL_DELIVERY_ENABLED=true
BREVO_API_KEY=your-brevo-api-key
BREVO_FROM_EMAIL=your-verified-sender@your-domain.com
BREVO_FROM_NAME=ICE Business Development
```

SMTP configuration is not required when Brevo is selected. Keep `EMAIL_DELIVERY_ENABLED=false` for setup or server simulations. Unsupported provider names are rejected; provider names are case-insensitive and trimmed. When live delivery is enabled, startup validates the credentials required by the selected provider.

Add or edit a sender under **Email configurations** using any address authorised in your Brevo account, mark it verified in the CRM, and assign its products/users. Campaigns use the selected sender's email, display name, and Reply-To; BREVO_FROM_EMAIL/BREVO_FROM_NAME are the defaults for invitation emails. The frontend shows the selected provider and master delivery setting without exposing secrets.

Brevo requests use `POST https://api.brevo.com/v3/smtp/email` with the `api-key` header and individual recipient payloads, following [Brevo's transactional API reference](https://developers.brevo.com/reference/send-transac-email). A successful response's messageId is retained in the recipient log. Known HTTP 4xx rejections become Failed; timeouts, HTTP 408/5xx and responses without a confirmed messageId become Unknown. Neither outcome is automatically retried. Provider response bodies and API keys are not included in recipient errors or logs.

This Brevo API integration requires HTTPS image URLs in email/signature HTML. Embedded data/CID signature images require SMTP; the API rejects unsupported images before a Brevo launch rather than silently sending broken images. Preview and server simulations still support embedded images. This does not add delivery/open/click tracking webhooks or general file attachments.

## Local startup connection errors

If run_backend.py reports that `postgres` or `db` cannot be resolved, DATABASE_URL is using a Docker-only service name. A host-side Python runner must use PostgreSQL's published host address/port. For local PostgreSQL this is typically `127.0.0.1:5432`; for Docker, inspect the database container's published ports. A container listing only `5432/tcp` has no published host port. Do not assume another project's PostgreSQL instance uses the same credentials.

The runner performs a read-only connection check before migrations and reports DNS, authentication, missing-database and connection failures without dumping credentials. A valid connection still requires the target database to exist and the configured role to have migration privileges. It also checks that the API port is available; it never stops other applications or rewrites credentials automatically.

For this repository's Docker database, keep `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` consistent with both URLs. `DATABASE_URL` is the host-side address; `DOCKER_DATABASE_URL` is the container-side address with host `db` and port `5432`. Percent-encode special characters in URL usernames/passwords; POSTGRES_PASSWORD itself stays unencoded. Existing PostgreSQL volumes are never reset by startup.

If API port 5002 is occupied, stop the application that owns it yourself before launching CRM, or explicitly choose a different APP_PORT and update the frontend proxy. The runner refuses to take over an occupied port.

CRM uses API port 5002 so the existing Pulse service can keep port 5000. The frontend development proxy uses http://127.0.0.1:5002.

## Initial Super Admin at startup

Configure the following in the backend `.env` before starting the API:

```dotenv
DEFAULT_PLATFORM_ADMIN_EMAIL=your-admin@your-domain.com
DEFAULT_PLATFORM_ADMIN_PASSWORD=your-unique-password-at-least-12-characters
DEFAULT_PLATFORM_ADMIN_USERNAME=Platform Admin
```

After migrations, FastAPI startup creates that email as an active **Super Admin**. Passwords are Argon2-hashed off the event loop. The username supplies display names; login uses the email address. A PostgreSQL transaction lock and unique-email conflict handling prevent duplicate accounts across workers or reloads. Creation is audit-logged without logging credentials.

Existing accounts are never promoted, reactivated, renamed, or given a replacement password by startup. Changing the configured password does not reset the saved account password; use normal account/password management. If the configured email already belongs to a BDO or inactive account, manage that existing account explicitly rather than using startup to overwrite it. If the email does not exist and no bootstrap password is configured, startup stops with a clear message. Passwords, when provided, must be 12–128 characters. Remove the bootstrap email/password variables after initial setup if you no longer want startup to ensure the initial identity exists.

No automatic bootstrap runs in the campaign worker. It runs in the API lifespan for both run_backend.py and python -m app (and Docker API startup). Starting the script still requires an available database and API port.

## Staff invitation and first-login onboarding

`POST /api/v1/users` accepts firstName, lastName, email, role and productIds; it no longer accepts an initial password. Only a Super Admin can invite staff. Real mail delivery must be enabled and configured, otherwise creation returns a clear 409 without creating a user. The created account has passwordSet=false and cannot log in until the invitation is accepted. Existing users keep passwordSet=true and onboardingCompleted=true when the new migration is applied.

Creation and invitation queueing commit together. The existing worker processes invitation email through EMAIL_PROVIDER (SMTP or Brevo), separately from campaign simulation. run_backend.py starts this mail worker automatically; when using manual API startup instead, run `python -m app.services.worker` to deliver queued invitations. SMTP invitations use SMTP_FROM_EMAIL/SMTP_FROM_NAME, or an existing verified sender as a fallback. Brevo uses BREVO_FROM_EMAIL/BREVO_FROM_NAME. FRONTEND_BASE_URL must be the actual, recipient-accessible frontend origin (development default http://localhost:5173), not the backend API URL. The SPA host must serve the frontend entry page for `/activate`.

The email link opens `/activate#token=...`. The frontend reads and removes the fragment from browser history; tokens are not placed in request URLs or browser storage. The page has exactly New password and Confirm password fields. Verification is non-consuming, so email link scanners cannot activate an account merely by checking the link. Acceptance requires matching passwords with 12–128 characters, consumes the token under database locks, sets an Argon2 password, revokes old sessions/tokens, and returns a fresh authenticated session. Pending, disabled, expired, revoked and already-used invitations cannot be claimed.

Routes:

- `POST /auth/invitations/verify` with `{token}` checks the link without consuming it.
- `POST /auth/invitations/accept` with `{token,password,confirmPassword}` sets the password and signs in.
- `POST /users/{id}/invitation` queues a replacement for an active user still awaiting setup; all previous links are invalidated. It is not a password-reset endpoint for activated users.
- `POST /auth/onboarding/complete` records the authenticated user's completion or skipping of the short module tour.

Invitations expire after INVITATION_EXPIRY_HOURS (default 24, configurable 1–168). Verification stores SHA-256 token digests; the durable email outbox keeps the raw token only as Fernet-encrypted ciphertext derived from SECRET_KEY, and erases that ciphertext after dispatch or cancellation. Rotating SECRET_KEY before dispatch requires resending affected invitations. Invitation tokens/ciphertext and provider credentials are never returned in the workspace snapshot. Admins see Queued, Dispatching, Accepted, Failed, Unknown or Cancelled email status and an expiry timestamp. Provider acceptance is not a delivery guarantee. Uncertain sends are not automatically retried; inspect provider logs before resending. A resend creates a new token; it never reuses the previous link.

After acceptance, the frontend shows a welcome popup and short popups beside the relevant modules. The tour follows available navigation, includes administrator screens only for Super Admins, and adapts to narrow screens. Completing or skipping persists onboardingCompleted=true on the account, so it does not restart on each login. Existing accounts and the bootstrap administrator are not forced through this new-user tour.

Run `alembic upgrade head` before restarting API/worker after this change, or use run_backend.py (which applies migrations before starting the API). No real emails are sent by the isolated tests:

```bash
python -m unittest discover -s tests/unit -v
alembic upgrade head --sql
```

Frontend VITE_FRONTEND_BASE_URL does not set backend FRONTEND_BASE_URL automatically. Configure the backend value explicitly to generate invitation links for the correct deployment.

## One-terminal development startup

```bash
python3 run_backend.py
```

The runner installs dependencies, checks the database and port, applies migrations, then starts the API. Once API readiness succeeds, it starts the invitation/campaign mail worker. Both share the terminal's output. Ctrl+C or SIGTERM shuts down both processes and the API reload children. If either service exits, its companion is stopped and the runner reports the failure; it does not silently leave a half-running stack. It waits briefly for graceful shutdown before force-stopping remaining child processes.

Database startup remains separate (`docker compose up -d db` if needed); stopping the runner does not stop PostgreSQL or other applications. Do not also start a worker manually for the same local run. Restart the runner after changing provider/environment settings or worker code so both processes use the updated configuration. `python -m app` and Docker Compose keep their existing separate-service behavior.

## Editing and deleting staff accounts

Administrators can edit staff email addresses in User management. Addresses are validated and stored lowercase; duplicates are rejected. Active users keep their existing password and use the new email to log in. Saving account changes revokes sessions. When a pending account's email changes, old invitation links are revoked and a fresh invitation is queued for the corrected address (mail delivery must be enabled). Disabling an account revokes pending invitations as well.

`DELETE /api/v1/users/{id}` accepts an optional `{replacementOwnerId}` JSON body (defaults to the acting administrator). The confirmation screen lets you choose a replacement. Self-deletion and deletion of the last active Super Admin are blocked. The replacement must be active, have completed setup, and already have access to all affected products; a Super Admin can take over any product.

Deletion removes the login account from User management, invalidates passwords/sessions/invitations, removes product and sender access, and frees the old email for a new account with a distinct ID. Owned products, contact relationships and open follow-ups are reassigned. Draft campaigns are reassigned with sender/signature cleared for review. Scheduled/sending campaigns are cancelled and remaining queued recipients excluded; already-dispatching messages may finish. Private saved audiences are removed. Contacts, leads, clients, completed follow-ups, submitted campaign history, signatures and audit attribution are retained.

A non-login historical user identity (marked deleted_at, with an internal anonymised email) remains for foreign-key integrity and name attribution. This is account deletion, not a purge of historical business records. Deleted accounts cannot be restored through account editing or invitation resend endpoints. The workspace includes these identities only so history can resolve names; the staff management list filters them out. Bootstrap does not recreate an old configured email while another usable Super Admin exists, so editing or deleting the original admin does not silently undo the action on restart.

Apply the new migration with `alembic upgrade head` before API restart, or run `python3 run_backend.py` to migrate and start API/worker together.

Brevo campaign senders are not restricted to BREVO_FROM_EMAIL. The selected CRM sender is sent in the transactional API sender object, as supported by [Brevo's reference](https://developers.brevo.com/reference/send-transac-email). Verification status, product assignments and BDO user assignments are still enforced. A provider-side verification badge does not automatically import a sender into this CRM or grant product/user access. Environment sender defaults continue to apply to system invitations.

## Preparing uploaded signature images for Brevo

In Compose or the launch confirmation, use **Prepare images for Brevo**. Download each detected image, upload it to your Brevo Content library or website, and paste its direct public HTTPS image URL. The CRM replaces only the image sources, preserves inline formatting, updates the saved signature through `PUT /signatures/{id}`, and saves the campaign draft. Previously submitted campaign snapshots remain unchanged. The final launch is still a separate confirmed action.

The API does not pretend an ordinary attachment is an inline CID image. Brevo's [image gallery API](https://developers.brevo.com/reference/upload-image-to-gallery) takes an existing public image URL, not a local/base64 file. In local development, the CRM cannot create a public HTTPS URL for an uploaded image automatically. Use the provider's [Content library upload](https://help.brevo.com/hc/en-us/articles/4504463584786-Add-and-manage-images-in-the-Content-library) or your own image hosting, then supply the direct URL. URL format checks do not verify that a remote file is reachable. The server never fetches arbitrary image URLs.


## Invitation email design

Account invitations use the branded HTML template in `app/services/email_templates.py` for both Brevo and SMTP. The template includes a dark header, an account setup button (with an Outlook fallback), a copyable activation link, and single-use/expiry information. Layout tables and inline styles support email clients; narrow screens use smaller spacing.

Set `FRONTEND_BASE_URL` to the public HTTPS frontend address. The header loads the original white company logo from `/brand/ice-logo.png` on that frontend, without embedded images or attachments. Local HTTP development omits the remote logo and retains the CRM text header. Email clients may block remote images; the setup instructions and button remain usable. Deploy the backend change and restart the email worker to use the new template for subsequent invitations. Campaign content and saved signatures are unchanged.
