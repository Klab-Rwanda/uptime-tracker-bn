import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import WebSocket

from app.util.redis import get_redis_client

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[UUID, set[WebSocket]] = {}

    async def connect(self, organization_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        if organization_id not in self.active_connections:
            self.active_connections[organization_id] = set()
        self.active_connections[organization_id].add(websocket)

    def disconnect(self, organization_id: UUID, websocket: WebSocket) -> None:
        if organization_id in self.active_connections:
            self.active_connections[organization_id].discard(websocket)
            if not self.active_connections[organization_id]:
                del self.active_connections[organization_id]

    async def broadcast_to_org(
        self, organization_id: UUID, message: dict
    ) -> None:
        sockets = self.active_connections.get(organization_id, set()).copy()
        for websocket in sockets:
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(organization_id, websocket)

    async def broadcast_org_event(
        self,
        organization_id: UUID,
        event_type: str,
        data: dict,
    ) -> None:
        payload = {
            "type": event_type,
            "organization_id": str(organization_id),
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.broadcast_to_org(organization_id, payload)

        try:
            redis = get_redis_client()
            channel = f"org:{organization_id}:events"
            await redis.publish(channel, json.dumps(payload))
        except Exception as error:
            logger.debug("Redis publish not available: %s", error)


manager = ConnectionManager()
