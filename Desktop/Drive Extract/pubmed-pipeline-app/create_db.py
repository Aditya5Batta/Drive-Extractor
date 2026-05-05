import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    eng = create_async_engine(
        "postgresql+asyncpg://postgres:12345@localhost:5432/postgres",
        isolation_level="AUTOCOMMIT"
    )
    async with eng.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname='europepmc_pipeline'")
        )
        exists = result.fetchone()
        if not exists:
            await conn.execute(text("CREATE DATABASE europepmc_pipeline"))
            print("Created database: europepmc_pipeline")
        else:
            print("Database europepmc_pipeline already exists")
    await eng.dispose()

    # Now create tables
    import sys
    sys.path.insert(0, "backend")
    sys.path.insert(0, "backend/mcp")
    from db import engine, Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created in europepmc_pipeline: runs, papers")

asyncio.run(main())
