import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import app
from app.organizations.models import OrganizationInvitation
from app.util.config import get_settings
from app.util.database import Base, get_db_session
from app.util.redis import get_redis_client

pytestmark = pytest.mark.asyncio
sent_emails: list[tuple[str, str, dict[str, str]]] = []


class FakeRedis:
    def __init__(self, attempts: int = 1) -> None:
        self.attempts = attempts

    async def eval(self, *_: object) -> int:
        return self.attempts


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("APP_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(
        kind: str,
        *,
        recipient: str,
        parameters: dict[str, str],
    ) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()
    get_redis_client.cache_clear()


async def create_account(
    client: AsyncClient,
    email: str,
    password: str = "StrongPassword123!",
    verify: bool = True,
) -> tuple[dict[str, str], str]:
    registered = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert registered.status_code == 201

    if not verify:
        return {}, ""

    _, _, parameters = next(
        msg
        for msg in reversed(sent_emails)
        if msg[1] == email.lower() and msg[0] == "otp_code"
    )
    code = parameters["otp_code"]
    verified = await client.post(
        "/v1/auth/verify-email",
        json={"email": email, "code": code},
    )
    assert verified.status_code == 200

    login_resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    tokens = login_resp.json()["data"]
    return tokens, code


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_owner_can_invite_existing_verified_account_and_member_accepts(
    client: AsyncClient,
) -> None:
    owner_tokens, _ = await create_account(client, "owner@example.com")
    member_tokens, _ = await create_account(client, "member@example.com")

    # Create organization
    created = await client.post(
        "/v1/organizations",
        json={"name": "  Uptime Team  ", "slug": "uptime-team"},
        headers=bearer(owner_tokens),
    )
    assert created.status_code == 201
    org_data = created.json()["data"]
    assert org_data["name"] == "Uptime Team"
    assert org_data["role"] == "owner"
    org_id = org_data["id"]

    # Owner invites collaborator
    invitation_resp = await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "MEMBER@example.com", "role": "editor"},
        headers=bearer(owner_tokens),
    )
    assert invitation_resp.status_code == 201
    invitation = invitation_resp.json()["data"]
    assert invitation["email"] == "member@example.com"
    assert invitation["status"] == "pending"

    # Verify invitation lasts 48 hours
    expires_at = datetime.fromisoformat(invitation["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    expected_expiry = datetime.now(UTC) + timedelta(hours=48)
    assert abs(expires_at - expected_expiry) < timedelta(seconds=10)

    # Check invitation email was sent with token
    invite_email = next(
        msg
        for msg in reversed(sent_emails)
        if msg[0] == "invite" and msg[1] == "member@example.com"
    )
    invite_url = invite_email[2]["invite_url"]
    token = invite_url.split("#token=")[1]
    assert token

    # Collaborator accepts invitation
    accepted = await client.post(
        "/v1/organizations/invitations/accept",
        json={"token": token},
        headers=bearer(member_tokens),
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["organization_id"] == org_id
    assert accepted.json()["data"]["role"] == "editor"

    # List members
    members_resp = await client.get(
        f"/v1/organizations/{org_id}/members",
        headers=bearer(owner_tokens),
    )
    assert members_resp.status_code == 200
    member_emails = [m["email"] for m in members_resp.json()["data"]]
    assert "owner@example.com" in member_emails
    assert "member@example.com" in member_emails

    # Token replay fails
    replay = await client.post(
        "/v1/organizations/invitations/accept",
        json={"token": token},
        headers=bearer(member_tokens),
    )
    assert replay.status_code == 400

    # Also test PDF contract endpoint POST /v1/organizations/{id}/members invites a third member
    third_tokens, _ = await create_account(client, "third_contract@example.com")
    contract_invite = await client.post(
        f"/v1/organizations/{org_id}/members",
        json={"email": "third_contract@example.com", "role": "viewer"},
        headers=bearer(owner_tokens),
    )
    assert contract_invite.status_code == 201
    assert contract_invite.json()["data"]["role"] == "viewer"
    assert contract_invite.json()["data"]["email"] == "third_contract@example.com"


async def test_invitation_requires_existing_active_verified_account(
    client: AsyncClient,
) -> None:
    owner_tokens, _ = await create_account(client, "owner2@example.com")

    created = await client.post(
        "/v1/organizations",
        json={"name": "Test Org"},
        headers=bearer(owner_tokens),
    )
    org_id = created.json()["data"]["id"]

    # Inviting an email that doesn't exist returns 404 verified_account_required
    missing = await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "nonexistent@example.com"},
        headers=bearer(owner_tokens),
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "verified_account_required"

    # Inviting an unverified account also returns 404 verified_account_required
    await create_account(client, "unverified@example.com", verify=False)
    unverified_resp = await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "unverified@example.com"},
        headers=bearer(owner_tokens),
    )
    assert unverified_resp.status_code == 404
    assert unverified_resp.json()["error"]["code"] == "verified_account_required"


async def test_invitation_expires_after_48_hours(
    client: AsyncClient,
) -> None:
    owner_tokens, _ = await create_account(client, "owner_exp@example.com")
    collab_tokens, _ = await create_account(client, "collab_exp@example.com")

    created = await client.post(
        "/v1/organizations",
        json={"name": "Expiry Org"},
        headers=bearer(owner_tokens),
    )
    org_id = created.json()["data"]["id"]

    invite_resp = await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "collab_exp@example.com"},
        headers=bearer(owner_tokens),
    )
    assert invite_resp.status_code == 201

    invite_email = next(
        msg
        for msg in reversed(sent_emails)
        if msg[0] == "invite" and msg[1] == "collab_exp@example.com"
    )
    token = invite_email[2]["invite_url"].split("#token=")[1]

    # Force expiration in database (sent 50 hours ago, expired 2 hours ago)
    override_session_gen = app.dependency_overrides[get_db_session]()
    session = await anext(override_session_gen)
    try:
        past_created = datetime.now(UTC) - timedelta(hours=50)
        past_expired = datetime.now(UTC) - timedelta(hours=2)
        await session.execute(
            update(OrganizationInvitation)
            .where(OrganizationInvitation.email == "collab_exp@example.com")
            .values(created_at=past_created, expires_at=past_expired)
        )
        await session.commit()
    finally:
        await session.close()

    # Attempt to accept expired invitation -> 410 invitation_expired
    expired_resp = await client.post(
        "/v1/organizations/invitations/accept",
        json={"token": token},
        headers=bearer(collab_tokens),
    )
    assert expired_resp.status_code == 410
    assert expired_resp.json()["error"]["code"] == "invitation_expired"


async def test_invitation_only_works_for_matching_signed_in_account(
    client: AsyncClient,
) -> None:
    owner_tokens, _ = await create_account(client, "owner_match@example.com")
    recipient_tokens, _ = await create_account(client, "recipient@example.com")
    other_tokens, _ = await create_account(client, "other@example.com")

    created = await client.post(
        "/v1/organizations",
        json={"name": "Match Team"},
        headers=bearer(owner_tokens),
    )
    org_id = created.json()["data"]["id"]

    await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "recipient@example.com"},
        headers=bearer(owner_tokens),
    )
    invite_email = next(
        msg
        for msg in reversed(sent_emails)
        if msg[0] == "invite" and msg[1] == "recipient@example.com"
    )
    token = invite_email[2]["invite_url"].split("#token=")[1]

    # Other user tries to accept
    mismatch = await client.post(
        "/v1/organizations/invitations/accept",
        json={"token": token},
        headers=bearer(other_tokens),
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == "invitation_email_mismatch"

    # Intended recipient accepts via direct /v1/invitations/accept alias
    accepted = await client.post(
        "/v1/invitations/accept",
        json={"token": token},
        headers=bearer(recipient_tokens),
    )
    assert accepted.status_code == 200


async def test_only_owner_can_manage_collaborators_and_members_can_leave(
    client: AsyncClient,
) -> None:
    owner_tokens, _ = await create_account(client, "owner_perm@example.com")
    member_tokens, _ = await create_account(client, "member_perm@example.com")
    third_tokens, _ = await create_account(client, "third@example.com")

    created = await client.post(
        "/v1/organizations",
        json={"name": "Team Perm"},
        headers=bearer(owner_tokens),
    )
    org_id = created.json()["data"]["id"]

    # Invite and accept
    await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "member_perm@example.com", "role": "member"},
        headers=bearer(owner_tokens),
    )
    invite_email = next(
        msg
        for msg in reversed(sent_emails)
        if msg[0] == "invite" and msg[1] == "member_perm@example.com"
    )
    token = invite_email[2]["invite_url"].split("#token=")[1]
    await client.post(
        "/v1/organizations/invitations/accept",
        json={"token": token},
        headers=bearer(member_tokens),
    )

    # Member cannot rename org
    denied_rename = await client.patch(
        f"/v1/organizations/{org_id}",
        json={"name": "Changed"},
        headers=bearer(member_tokens),
    )
    assert denied_rename.status_code == 403

    # Member cannot invite others
    denied_invite = await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "third@example.com"},
        headers=bearer(member_tokens),
    )
    assert denied_invite.status_code == 403

    # Owner cannot leave without transferring ownership
    owner_leave = await client.delete(
        f"/v1/organizations/{org_id}/members/me",
        headers=bearer(owner_tokens),
    )
    assert owner_leave.status_code == 409

    # Transfer ownership to member
    members = await client.get(
        f"/v1/organizations/{org_id}/members",
        headers=bearer(owner_tokens),
    )
    member_user_id = next(
        m["user_id"] for m in members.json()["data"] if m["email"] == "member_perm@example.com"
    )

    transferred = await client.post(
        f"/v1/organizations/{org_id}/transfer-ownership",
        json={"user_id": member_user_id},
        headers=bearer(owner_tokens),
    )
    assert transferred.status_code == 200

    # Former owner can now leave
    former_owner_leave = await client.delete(
        f"/v1/organizations/{org_id}/members/me",
        headers=bearer(owner_tokens),
    )
    assert former_owner_leave.status_code == 204

    # Former owner is no longer a member
    get_org = await client.get(
        f"/v1/organizations/{org_id}",
        headers=bearer(owner_tokens),
    )
    assert get_org.status_code == 404
