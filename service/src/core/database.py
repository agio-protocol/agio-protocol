"""Async SQLAlchemy engine + session factory with connection pooling."""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=30,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a DB session per request."""
    async with async_session() as session:
        yield session
