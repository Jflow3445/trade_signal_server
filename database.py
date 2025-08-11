import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

url = os.getenv("DATABASE_URL")
if not url:
    raise RuntimeError("DATABASE_URL is required")

engine = create_engine(url)  # psycopg2 via URL
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

print(f"[boot] Using DB: {engine.url.render_as_string(hide_password=True)}")
