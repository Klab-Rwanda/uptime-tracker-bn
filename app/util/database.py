from sqlalchemy.ext.asyncio import(
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.util.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
)

sessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db():
    async with sessionLocal( ) as db:
        yield db

