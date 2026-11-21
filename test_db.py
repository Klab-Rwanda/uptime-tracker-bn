import asyncio

from sqlalchemy import text

from app.util.database import engine


async def check_database():
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT version()"))
        database_version = result.scalar_one()

        print("Connected to Neon")
        print(database_version)


asyncio.run(check_database())