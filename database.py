import os
from urllib.parse import urlparse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# --- DB URL (Postgres required) ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

scheme = urlparse(DATABASE_URL).scheme.lower()
if not scheme.startswith("postgresql"):
    raise RuntimeError(f"Only Postgres is supported here. Got scheme '{scheme}'")

# --- Engine / Session / Base ---
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
