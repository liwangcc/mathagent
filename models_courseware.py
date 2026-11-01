"""课件库持久化模型。

课件按老师的学段隔离；teacher_id 为空的记录是该学段的公共课件。
"""

from datetime import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text

from database import Base


class Courseware(Base):
    __tablename__ = "coursewares"
    __table_args__ = (
        Index("ix_coursewares_stage", "education_stage"),
        Index("ix_coursewares_scope", "education_stage", "teacher_id"),
        Index("ix_coursewares_chapter", "chapter"),
        Index("ix_coursewares_knowledge_point", "knowledge_point"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    education_stage = Column(String(16), nullable=False, default="", server_default="")
    title = Column(String(200), nullable=False)
    chapter = Column(String(120), nullable=False, default="", server_default="")
    knowledge_point = Column(String(160), nullable=False, default="", server_default="")
    summary = Column(String(500), nullable=False, default="", server_default="")
    # 教学场景元数据。JSON 以文本保存，保证 SQLite/离线导出兼容。
    objective = Column(Text, nullable=False, default="", server_default="")
    misconception = Column(Text, nullable=False, default="", server_default="")
    steps = Column(Text, nullable=False, default="[]", server_default="[]")
    interaction_state = Column(Text, nullable=False, default="{}", server_default="{}")
    checkpoints = Column(Text, nullable=False, default="[]", server_default="[]")
    interaction_type = Column(String(64), nullable=False, default="custom", server_default="custom")
    offline_file_name = Column(String(240), nullable=False, default="", server_default="")
    file_name = Column(String(240), nullable=False)
    stored_path = Column(String(500), nullable=False)
    mime = Column(String(100), nullable=False, default="text/html", server_default="text/html")
    is_public = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now())
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(), onupdate=lambda: datetime.now())

    @staticmethod
    def _json_value(value, fallback):
        if isinstance(value, (dict, list)):
            return value
        try:
            parsed = json.loads(value or "")
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    def to_dict(self, *, recommendation: dict | None = None) -> dict:
        stage = self.education_stage or ""
        steps = self._json_value(self.steps, [])
        interaction_state = self._json_value(self.interaction_state, {})
        checkpoints = self._json_value(self.checkpoints, [])
        offline_name = self.offline_file_name or self.file_name
        payload = {
            "id": self.id,
            "education_stage": stage,
            "stage": stage,
            "title": self.title,
            "chapter": self.chapter or "",
            "knowledge_point": self.knowledge_point or "",
            "knowledgePoint": self.knowledge_point or "",
            "summary": self.summary or "",
            "objective": self.objective or "",
            "misconception": self.misconception or "",
            "steps": steps,
            "interactionState": interaction_state,
            "interaction_state": interaction_state,
            # 前端以 interactionType 选择对应的 render(state) 实现。
            "render": {"interactionType": self.interaction_type or "custom"},
            "checkpoints": checkpoints,
            "file_name": self.file_name,
            "offlineFileName": offline_name,
            "offline_file_name": offline_name,
            "mime": self.mime or "text/html",
            "is_public": bool(self.is_public),
            "download_url": f"/api/courseware/{self.id}/download",
            "preview_url": f"/api/courseware/{self.id}/preview",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if recommendation:
            payload["recommendation"] = recommendation
            payload["matched_knowledge_point"] = recommendation.get("matched_knowledge_point", "")
            payload["matched_chapter"] = recommendation.get("matched_chapter", "")
            payload["recommendation_reason"] = recommendation.get("reason", "")
        return payload
