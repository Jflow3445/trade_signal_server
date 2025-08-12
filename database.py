import os
from urllib.parse import urlparse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

url = os.getenv("DATABASE_URL")
if not url:
    raise RuntimeError("DATABASE_URL is required")

scheme = urlparse(url).scheme.lower()
# acceptable: postgresql, postgresql+psycopg2, postgresql+psycopg
if not (scheme.startswith("postgresql")):
    raise RuntimeError(f"SQLite (or non-Postgres) URLs are forbidden here: got '{scheme}'")

engine = create_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()