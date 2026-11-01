"""Persisted stage labels; empty is deliberately unclassified legacy data."""
from sqlalchemy import Column, String


class TeachingStageMixin:
    education_stage = Column(String(16), nullable=False, default="", server_default="")
    catalog_version = Column(String(96), nullable=False, default="", server_default="")

    def teaching_fields(self) -> dict:
        return {
            "education_stage": self.education_stage or "",
            "catalog_version": self.catalog_version or "",
        }
