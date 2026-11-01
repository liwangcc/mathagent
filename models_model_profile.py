"""模型密钥的职责组配置表。

一个组一行，`group` 唯一。空字段表示「继承下一层」（环境变量 → config 默认值），
因此这个表只存差异，不需要把整个默认配置抄一遍。
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from database import Base


class ModelProfile(Base):
    __tablename__ = "model_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # group 是 SQL 保留字，列名保持原样（SQLAlchemy 会加引号），属性名避开关键字。
    group = Column("group", String(40), unique=True, nullable=False)
    base_url = Column(String(300), default="", server_default="")
    api_key = Column(String(300), default="", server_default="")
    model = Column(String(100), default="", server_default="")
    concurrency = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "base_url": self.base_url or "",
            "model": self.model or "",
            "concurrency": self.concurrency,
            "has_api_key": bool(self.api_key),
            "updated_at": str(self.updated_at) if self.updated_at else None,
        }