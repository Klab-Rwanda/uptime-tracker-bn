# UpTimeTracker API

FastAPI backend for UpTimeTracker. The current feature branch implements user
authentication only.

## Requirements

- Python 3.12+
- PostgreSQL 16
- Redis 7 (required for login rate limiting)

## Local setup

Create a virtual environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Set `DATABASE_URL` to a PostgreSQL connection string and set `JWT_SECRET` to a
fresh random value of at least 32 bytes. Generate a value with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Never commit `.env` or share its contents in chat. For Docker Compose, use a
database hostname reachable from the API container. For Neon, use the connection
string provided for this project and keep it only in the local `.env`.

Apply the auth migration and start the API:

```sh
docker compose run --rm api alembic upgrade head
docker compose up --build
```

The API is available at `http://localhost:8000`; interactive API docs are at
`http://localhost:8000/docs`.

## Authentication endpoints

All responses use the `{ "data": ... }` success envelope and `{ "error": ... }`
error envelope.

- `POST /v1/auth/register` — create an account; requires a valid email and a
  password of 12–128 characters. An optional `full_name` defaults to the email
  local part.
- `POST /v1/auth/login` — return a 15-minute JWT access token and an opaque
  refresh token.
- `POST /v1/auth/refresh` — exchange a refresh token for a new access token and
  a rotated refresh token. Save the returned refresh token and do not reuse the
  old one; reuse revokes the whole login session.
- `POST /v1/auth/logout` — send the access token as a Bearer token to revoke the
  login session and its refresh tokens. Logout does not issue another token.
- `GET /v1/auth/me` — return the authenticated user profile.

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
Passwords are hashed with Argon2id. Access tokens are signed using HS256 and
include issuer, audience, session, and expiry claims. Revoked sessions are
checked on authenticated requests so logout invalidates access tokens
immediately. Login attempts are limited to 10 per email/IP pair per 15-minute
window; login fails closed with `503` if Redis is unavailable.

## Database migrations and tests

```sh
alembic upgrade head
pytest
```

Migration downgrade deletes authentication data; use it only on a disposable
database or after taking a backup.
