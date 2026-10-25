from contextlib import contextmanager
import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from dotenv import load_dotenv


load_dotenv()


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be set before opening a database session")
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    """Provide one transaction-scoped session, committing or rolling back atomically."""
    factory = create_session_factory(database_url)
    with factory() as session:
        with session.begin():
            yield session