"""Persistent protocol rows for opportunistic preprocessing workers."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from database import Base


class AcceleratorNode(Base):
    __tablename__ = "accelerator_nodes"
    __table_args__ = (
        Index("ix_accelerator_node_last_seen", "last_seen_at"),
        Index("ix_accelerator_node_enabled", "enabled"),
        Index("ix_accelerator_node_owner_lane", "owner_user_id", "lane"),
        UniqueConstraint(
            "owner_user_id", "device_id", "lane",
            name="uq_accelerator_node_owner_device_lane",
        ),
    )

    id = Column(String(64), primary_key=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Tenant-bound nodes are the only supported registration mode for now.
    # A future platform pool must use a separate audited scope, not bypass this
    # owner check implicitly.
    access_scope = Column(String(16), nullable=False, default="tenant")
    lane = Column(String(32), nullable=False, default="web_gpu")
    device_id = Column(String(64), nullable=True)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    enabled = Column(Integer, nullable=False, default=1)
    capabilities_json = Column(Text, nullable=False, default="{}")
    algorithm_version = Column(String(64), nullable=False, default="")
    free_slots = Column(Integer, nullable=False, default=0)
    queue_depth = Column(Integer, nullable=False, default=0)
    estimated_finish_ms = Column(Integer, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    token_rotated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AcceleratorJob(Base):
    __tablename__ = "accelerator_jobs"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id",
            "paper_id",
            "source_sha256",
            "template_sha256",
            "layout_snapshot_sha256",
            "stage",
            "lane",
            "algorithm_version",
            name="uq_accelerator_job_contract",
        ),
        Index("ix_accelerator_job_status_created", "status", "created_at"),
        Index("ix_accelerator_job_lease_expires", "lease_expires_at"),
        Index("ix_accelerator_job_deadline", "deadline_at"),
        Index("ix_accelerator_job_teacher", "teacher_id"),
    )

    id = Column(String(64), primary_key=True)
    batch_id = Column(String(64), nullable=True)
    paper_id = Column(String(64), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_path = Column(Text, nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    template_id = Column(String(64), nullable=False)
    template_path = Column(Text, nullable=False)
    template_sha256 = Column(String(64), nullable=False)
    layout_snapshot_json = Column(Text, nullable=False)
    layout_snapshot_sha256 = Column(String(64), nullable=False)
    stage = Column(String(32), nullable=False, default="template_registration")
    lane = Column(String(32), nullable=False, default="web_gpu")
    algorithm_version = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    attempt_count = Column(Integer, nullable=False, default=0)
    assigned_node_id = Column(String(64), ForeignKey("accelerator_nodes.id"), nullable=True)
    lease_id_hash = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    deadline_at = Column(DateTime, nullable=False)
    candidate_json = Column(Text, nullable=True)
    candidate_sha256 = Column(String(64), nullable=True)
    server_validation_status = Column(String(32), nullable=False, default="not_started")
    server_validation_json = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    candidate_received_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    fallback_at = Column(DateTime, nullable=True)
    # Server-owned evidence is written only after independent submitted-transform
    # validation. The path is never accepted from a worker and is removed after
    # business use.
    server_artifact_path = Column(Text, nullable=True)
    server_artifact_sha256 = Column(String(64), nullable=True)
    consumed_at = Column(DateTime, nullable=True)
