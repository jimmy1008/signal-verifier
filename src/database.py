"""
資料庫初始化與 Session 管理
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base

_engine = None
_SessionLocal = None


def init_db(db_url: str = "sqlite:///db/signals.db") -> None:
    """初始化資料庫，建立所有表"""
    global _engine, _SessionLocal
    _engine = create_engine(db_url, echo=False)
    _SessionLocal = sessionmaker(bind=_engine)
    Base.metadata.create_all(_engine)


def get_session():
    """取得 DB session（用完記得 close）"""
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()
