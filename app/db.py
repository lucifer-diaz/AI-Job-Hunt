from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.settings import get_settings


class Base(DeclarativeBase):
    pass


class AppSession(Session):
    """A session whose get() never returns another user's row when the session belongs to a user."""

    def get(self, entity, ident, **kwargs):
        obj = super().get(entity, ident, **kwargs)
        user_id = self.info.get("user_id")
        if obj is not None and user_id is not None:
            from app.tenancy import owned_by

            if not owned_by(obj, user_id):
                return None
        return obj


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, class_=AppSession, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
