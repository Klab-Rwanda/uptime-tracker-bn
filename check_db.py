import asyncio
import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

url = os.environ["DATABASE_URL"]
print("Connecting to host:", make_url(url).host)


async def main():
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        result = await conn.execute(text("select current_database()"))
        print("Connected! Database:", result.scalar())
    await engine.dispose()


asyncio.run(main())