"""用户认证模型 — 与现有 models.py 解耦，独立维护。"""

from datetime import datetime

from sqlalchemy import Boolean, Column, Integer, String, DateTime, Index

from database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone", "phone"),
        Index("ix_users_wechat_openid", "wechat_openid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), unique=True, nullable=True)
    name = Column(String(50), nullable=False)
    school = Column(String(100), default="")
    education_stage = Column(String(16), nullable=False, default="", server_default="")
    wechat_openid = Column(String(64), unique=True, nullable=True)
    invite_code = Column(String(32), default="")
    role = Column(String(20), default="teacher")
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=lambda: datetime.now())
    last_login = Column(DateTime, nullable=True)
    password_hash = Column(String(128), nullable=True)
    # 首启密码 / 管理员建号的初始密码：登录后应尽快修改；改密成功即清零。
    must_change_password = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # 当前选中的班级(用于班级切换)
    current_class_id = Column(Integer, nullable=True)  # FK removed to break circular dependency

    def to_dict(self):
        stage = self.education_stage if self.education_stage in ("primary", "junior", "senior") else None
        return {
            "id": self.id,
            "phone": self.phone,
            "name": self.name,
            "school": self.school or "",
            "education_stage": stage,
            "profile_complete": stage is not None,
            "wechat_openid": self.wechat_openid,
            "invite_code": self.invite_code or "",
            "role": self.role,
            "status": self.status,
            "has_password": bool(self.password_hash),
            "must_change_password": bool(self.must_change_password),
            "current_class_id": self.current_class_id,
            "created_at": str(self.created_at) if self.created_at else None,
            "last_login": str(self.last_login) if self.last_login else None,
        }
