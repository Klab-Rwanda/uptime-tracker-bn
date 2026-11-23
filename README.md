# UpTimeTracker API

FastAPI backend for UpTimeTracker. The current feature branch implements user
authentication only.

## Requirements

- Python 3.12+
- PostgreSQL 16
- Redis 7 (required for login rate limiting)

## Run with Docker Compose

Create the local environment file and set a random `JWT_SECRET` of at least
32 bytes in `.env`:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the printed value into `JWT_SECRET` in `.env`. Keep the example
`DATABASE_URL` and `REDIS_URL` unchanged when running the API in Docker Compose;
the `postgres` and `redis` hostnames are for container-to-container connections.
Then build the API image, start its dependencies, apply the database migration,
and run the API:

```powershell
docker compose build api
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up api
```

Open `http://localhost:8000/docs` for Swagger UI, or
`http://localhost:8000/redoc` for ReDoc. FastAPI provides these automatically;
no separate Swagger installation is needed.

### Configure Brevo SMTP

Authenticate a sending domain and verify a sender in Brevo. In Brevo's SMTP
settings, get the SMTP login and create an SMTP key; the SMTP login may differ
from your account email. Put the SMTP login, SMTP key, verified sender email,
and sender name in the matching `BREVO_*` variables in `.env`. The default
server is `smtp-relay.brevo.com` on port `587` with STARTTLS. The API builds
OTP, notification, update, and invite messages itself; no Brevo templates or
template IDs are needed. Restart the API after changing `.env`.

### Run the API on your machine instead

Start PostgreSQL and Redis with Compose, then install and run the Python app in
PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
docker compose up -d postgres redis
$env:DATABASE_URL = "postgresql+asyncpg://monitor:monitor@127.0.0.1:5433/monitor"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
$env:JWT_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(48))")
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```

The local connection uses port `5433`, as configured by `POSTGRES_PORT` in
`.env`. Keep `.env` private and never commit or share its contents. For Neon,
put the Neon connection string in `.env` and use it as `DATABASE_URL`.

## Test endpoints in Swagger UI

1. Open `http://localhost:8000/docs`.
2. Register with `POST /v1/auth/register`. Check your inbox for the OTP and
   submit it with the same email to `POST /v1/auth/verify-email`:
   `{"email":"alice@example.com","code":"123456"}`.
3. Use `POST /v1/auth/login` with the same email and password. Copy the
   `access_token` from the response.
4. Select **Authorize** at the top of the page and enter the access token.
   Swagger UI adds the `Bearer` prefix for the protected requests.
5. Try `GET /v1/auth/me` and `POST /v1/auth/logout`. Use the login response's
   `refresh_token` in `POST /v1/auth/refresh`; save each rotated token and do
   not reuse an old one.
6. To test recovery, call `POST /v1/auth/forgot-password`, then submit the
   received code and a new password to `POST /v1/auth/reset-password`.

## Authentication endpoints

All responses use the `{ "data": ... }` success envelope and `{ "error": ... }`
error envelope.

- `POST /v1/auth/register` — create an unverified account and send a six-digit
  email verification code. Requires a valid email and a password of 12–128
  characters. An optional `full_name` defaults to the email local part.
- `POST /v1/auth/verify-email` — verify an email using `{ "email": "...",
  "code": "123456" }`. Codes expire after 10 minutes by default and allow at
  most five attempts.
- `POST /v1/auth/resend-verification` — request another verification code.
- `POST /v1/auth/forgot-password` — send a password-reset code when the account
  exists; the response does not reveal whether the email is registered.
- `POST /v1/auth/reset-password` — submit email, six-digit code, and
  `new_password` (12–128 characters). Successfully resetting a password revokes
  that user's active sessions.
- `POST /v1/auth/login` — return a 15-minute JWT access token and an opaque
  refresh token, only after email verification.
- `POST /v1/auth/refresh` — exchange a refresh token for a new access token and
  a rotated refresh token. Save the returned refresh token and do not reuse the
  old one; reuse revokes the whole login session.
- `POST /v1/auth/logout` — send the access token as a Bearer token to revoke the
  login session and its refresh tokens. Logout does not issue another token.
- `GET /v1/auth/me` — return the authenticated user profile.

## Organization & Collaborator endpoints

- `POST /v1/organizations` — create an organization. The creator automatically receives the `owner` role.
- `GET /v1/organizations` — list organizations the authenticated user belongs to.
- `GET /v1/organizations/{organization_id}` — get organization details.
- `PATCH /v1/organizations/{organization_id}` — update organization name (owner or admin role required).
- `POST /v1/organizations/{organization_id}/transfer-ownership` — transfer organization ownership to an existing member (owner only).
- `GET /v1/organizations/{organization_id}/members` — list organization members.
- `DELETE /v1/organizations/{organization_id}/members/me` — leave organization (owner must transfer ownership first).
- `DELETE /v1/organizations/{organization_id}/members/{member_user_id}` — remove a member (owner or admin role required; owner cannot be removed).
- `POST /v1/organizations/{organization_id}/invitations` (or `POST /v1/organizations/{organization_id}/members`) — invite a collaborator.
  - Recipient **must** have an active, verified account in the system prior to receiving an invitation (`404` error if non-existent or unverified).
  - Invitations expire strictly after **48 hours**.
  - Generates a high-entropy cryptographically secure token (only SHA-256 hash stored in DB).
  - Sends a secure Brevo email with the invitation link.
- `GET /v1/organizations/{organization_id}/invitations` — list pending/historical invitations for the organization (owner or admin).
- `DELETE /v1/organizations/{organization_id}/invitations/{invitation_id}` — revoke a pending invitation (owner or admin).
- `POST /v1/organizations/invitations/accept` (or `POST /v1/invitations/accept`) — accept an invitation by submitting `{"token": "..."}`.
  - Requires the user to be authenticated with the exact matching email address the invite was issued to.
  - Enforces active status and verification, rejects expired invitations (`410`).

## Project endpoints

- `POST /v1/projects` — create a project within an organization (`{"organization_id": "...", "name": "..."}`). Requires editor role or higher.
- `GET /v1/projects?organization_id=...` — list projects in the organization. Requires organization membership.
- `GET /v1/projects/{project_id}` — get project details. Requires organization membership.
- `PATCH /v1/projects/{project_id}` — update project name (`{"name": "..."}`). Requires editor role or higher.
- `DELETE /v1/projects/{project_id}` — delete project. Requires owner or admin role.

Thunder Client request bodies:

```json
POST /v1/auth/register
{
  "email": "alice@example.com",
  "password": "StrongPassword123!",
  "full_name": "Alice"
}
```

```json
POST /v1/auth/login
{
  "email": "alice@example.com",
  "password": "StrongPassword123!"
}
```

```json
POST /v1/auth/refresh
{
  "refresh_token": "<refresh_token_from_login>"
}
```

For `/v1/auth/logout` and `/v1/auth/me`, set
`Authorization: Bearer <access_token>`. Logout revokes the whole login session;
it does not return a replacement refresh token.

Refresh tokens are high-entropy opaque values; only SHA-256 hashes are stored.
Passwords are hashed with Argon2id. Email verification and reset codes are
stored as keyed hashes, expire, are single-use, and have issuance cooldowns.
Brevo SMTP sends the app-authored verification/reset, notification, update, and
invite messages. The notification, update, and invite message kinds are ready
for their corresponding application flows.
Access tokens are signed using HS256 and include issuer, audience, session, and
expiry claims. Revoked sessions are checked on authenticated requests so logout
invalidates access tokens immediately. Login attempts are limited to 10 per
email/IP pair per 15-minute window; login fails closed with `503` if Redis is
unavailable.

## Database migrations and tests

```sh
alembic upgrade head
pytest
```

Migration downgrade deletes authentication data; use it only on a disposable
database or after taking a backup.
