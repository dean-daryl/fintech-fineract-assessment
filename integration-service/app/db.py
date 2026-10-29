from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url.replace("postgresql://", "postgresql+psycopg://", 1), pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
