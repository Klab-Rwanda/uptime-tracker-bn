import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.api_keys.service import verify_api_key
from app.auth.models import AuthSession, User
from app.auth.security import decode_access_token
from app.organizations.service import _get_org_membership
from app.util.database import sessionLocal
from app.websocket.manager import manager

router = APIRouter(tags=["websocket"])


async def _authenticate_ws(
    token: str, organization_id: UUID
) -> User:
    async with sessionLocal() as session:
        # Check API key
        if token.startswith("sk_live_"):
            api_key = await verify_api_key(session, token)
            if api_key is None or api_key.organization_id != organization_id:
                raise ValueError("Invalid API key")
            user = await session.get(User, api_key.created_by_user_id)
            if user is None or not user.is_active:
                raise ValueError("Inactive user")
            return user

        # Check JWT token
        try:
            user_id, auth_session_id = decode_access_token(token)
        except (jwt.InvalidTokenError, ValueError) as err:
            raise ValueError("Invalid JWT token") from err

        now = datetime.now(UTC)
        auth_session = await session.scalar(
            select(AuthSession).where(
                AuthSession.id == auth_session_id,
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        if auth_session is None:
            raise ValueError("Session revoked or expired")

        user = await session.scalar(
            select(User).where(User.id == user_id, User.is_active.is_(True))
        )
        if user is None:
            raise ValueError("User not found or inactive")

        # Verify organization membership
        await _get_org_membership(
            session,
            organization_id=organization_id,
            user_id=user.id,
        )
        return user


@router.websocket("/v1/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    organization_id: UUID = Query(...),
) -> None:
    try:
        user = await _authenticate_ws(token, organization_id)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(organization_id, websocket)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "organization_id": str(organization_id),
                "user_id": str(user.id),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        while True:
            data = await websocket.receive_text()
            try:
                parsed = json.loads(data)
                if parsed.get("type") == "ping":
                    await websocket.send_json(
                        {
                            "type": "pong",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(organization_id, websocket)
