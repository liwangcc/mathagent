import logging
logger = logging.getLogger(__name__)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=False)

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """每个连接都需显式开启：

    - journal_mode=WAL：提升并发读写。
    - foreign_keys=ON：让 ForeignKey 真正生效（H7）。
    - busy_timeout=30000：长事务（批改一份卷可能几十秒）期间，其他写入最多
      等待 30s 而非立即报 "database is locked"（H8，原 5s 不够）。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



def _verify_admin_password():
    """首次启动写入管理员初始密码；已有密码时**绝不覆盖**。

    历史行为：每次启动只要 .env 的 ADMIN_PASSWORD 与数据库不一致就强行覆盖，
    导致老师在界面上改完密码、服务一重启就被打回旧密码。

    现在的规则：
    - 管理员已存在且有 password_hash → 只记一条 info 日志，不改任何数据；
    - 没有管理员（全新数据库）或管理员 password_hash 为空 → 写入 ADMIN_PASSWORD
      作为初始密码；没有管理员时按 ADMIN_PHONE / ADMIN_NAME 建立第一个管理员。

    忘记密码请显式运行 ``scripts/set_admin_pwd.py``，不要改 .env 等它自动覆盖。
    """
    import os
    import secrets
    import string

    admin_pwd = os.environ.get("ADMIN_PASSWORD", "")
    admin_phone = os.environ.get("ADMIN_PHONE", "").strip()
    admin_name = os.environ.get("ADMIN_NAME", "").strip() or "管理员"
    result = {"created": False, "generated_password": None}
    try:
        from services.auth_service import hash_password
        from models_user import User

        db = next(get_db())
        try:
            user = (
                db.query(User)
                .filter(User.role == "admin")
                .order_by(User.id)
                .first()
            )
            if user is not None and user.password_hash:
                logger.info(
                    "管理员密码已存在，启动流程不覆盖（忘记密码请运行 "
                    "scripts/set_admin_pwd.py）"
                )
                return result

            generated = False
            if not admin_pwd:
                if user is not None:
                    # 已有管理员但没密码：不猜、不覆盖，交给显式命令处理。
                    logger.warning(
                        "管理员账号 %s 尚未设置密码：请运行 scripts/set_admin_pwd.py",
                        user.id,
                    )
                    return result
                # 全新安装且 .env 没给密码：生成一次首启密码，打到 stdout（
                # docker logs 可见）并写进持久卷，供管理员首次登录后立刻改掉。
                alphabet = string.ascii_letters + string.digits
                admin_pwd = "".join(secrets.choice(alphabet) for _ in range(16))
                generated = True
                _write_first_run_password(admin_pwd)
                print(
                    "================== MathAgent 首次启动 ==================\n"
                    f" 管理员登录账号：{admin_phone or '13800000000'}\n"
                    f" 管理员初始密码：{admin_pwd}\n"
                    " 登录后请立即修改密码（此密码只在首次初始化时生成一次）\n"
                    "========================================================",
                    flush=True,
                )

            if not admin_phone:
                admin_phone = (user.phone if user is not None else "") or "13800000000"

            if user is None:
                existing = db.query(User).filter(User.phone == admin_phone).first()
                if existing is not None:
                    existing.role = "admin"
                    existing.password_hash = hash_password(admin_pwd)
                    existing.must_change_password = 1
                    db.commit()
                    logger.info("已把账号 %s 提升为管理员并写入初始密码", existing.id)
                    result["created"] = True
                    result["generated_password"] = admin_pwd if generated else None
                    return result
                user = User(
                    phone=admin_phone,
                    name=admin_name,
                    role="admin",
                    status="active",
                    school="",
                    must_change_password=1,
                )
                db.add(user)

            user.password_hash = hash_password(admin_pwd)
            user.must_change_password = 1
            db.commit()
            logger.info(
                "已写入管理员初始密码（phone=%s）；此后重启不会覆盖", admin_phone
            )
            result["created"] = True
            result["generated_password"] = admin_pwd if generated else None
            return result
        finally:
            db.close()
    except Exception as e:
        logger.warning("管理员密码初始化跳过: %s", e)
        return result


def _write_first_run_password(password: str) -> None:
    """首启密码写入持久卷（0600），供 `docker compose logs` 之外的二次查看。"""
    import os

    try:
        from config import DATA_DIR

        path = os.path.join(DATA_DIR, ".first-run-password")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, password.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:  # pragma: no cover - 只影响便利性，不影响登录
        logger.warning("首启密码文件写入失败: %s", exc)


def init_db():
    import models  # noqa: F401
    import models_correction  # noqa: F401
    import models_accelerator  # noqa: F401
    import models_user  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_courseware  # noqa: F401
    import models_submission  # noqa: F401
    import models_model_profile  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate()
    _verify_admin_password()


def _migrate():
    """Add missing columns to existing tables (safe to run on every startup)."""
    from sqlite3 import OperationalError

    conn = None
    try:
        conn = engine.raw_connection()
        cur = conn.cursor()
        from services.school_stage_migration import migrate_school_stage_schema
        migrate_school_stage_schema(conn)
        # Add needs_review_count to grading_results
        cur.execute("PRAGMA table_info(grading_results)")
        cols = [row[1] for row in cur.fetchall()]
        if "needs_review_count" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN needs_review_count INTEGER DEFAULT 0")
        if "category" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN category VARCHAR(20) DEFAULT 'daily' NOT NULL")
        if "image_content_hash" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN image_content_hash VARCHAR(64) DEFAULT '' NOT NULL")
        if "ocr_raw_name" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN ocr_raw_name VARCHAR(100) DEFAULT ''")
        if "ocr_confidence" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN ocr_confidence FLOAT DEFAULT 0.0")
        if "ocr_raw_json" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN ocr_raw_json TEXT DEFAULT '{}'")
        if "deleted_at" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN deleted_at DATETIME")
        if "max_score" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN max_score FLOAT")
        if "correct_rate" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN correct_rate FLOAT")
        if "scoring_version" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN scoring_version INTEGER DEFAULT 1 NOT NULL")
        if "grading_contract_hash" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN grading_contract_hash VARCHAR(64) DEFAULT '' NOT NULL")
        if "analytics_included" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN analytics_included BOOLEAN DEFAULT 1 NOT NULL")
        if "inclusion_reason" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN inclusion_reason VARCHAR(64) DEFAULT 'included' NOT NULL")
        if "observation_period_id" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN observation_period_id INTEGER")
        if "assessment_purpose" not in cols:
            cur.execute("ALTER TABLE grading_results ADD COLUMN assessment_purpose VARCHAR(32) DEFAULT 'standard' NOT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_grading_results_deleted_at ON grading_results(deleted_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_grading_image_hash ON grading_results(image_content_hash)")
        # Active rows alone participate in idempotency.  The frozen v2 scoring
        # contract is part of the identity: regrading an identical image after a
        # teacher changes marks or answers must not replay the old result.  Create
        # the new guard before removing the legacy guard; if historical duplicates
        # prevent it, fail closed rather than accepting future duplicate archives.
        try:
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_grading_student_image_category_exam_contract "
                "ON grading_results(student_id, image_content_hash, category, exam_name, grading_contract_hash) "
                "WHERE image_content_hash IS NOT NULL AND image_content_hash != '' AND deleted_at IS NULL"
            )
        except Exception as exc:
            raise RuntimeError(
                "Cannot create grading idempotency index; resolve duplicate active grading results before startup"
            ) from exc
        cur.execute("DROP INDEX IF EXISTS uq_grading_student_image_category_exam")
        # Add needs_review_count to student_profiles
        cur.execute("PRAGMA table_info(student_profiles)")
        cols = [row[1] for row in cur.fetchall()]
        if "needs_review_count" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN needs_review_count INTEGER DEFAULT 0")
        if "image_content_hash" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN image_content_hash VARCHAR(64) DEFAULT ''")
        if "max_score" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN max_score FLOAT")
        if "correct_rate" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN correct_rate FLOAT")
        if "scoring_version" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN scoring_version INTEGER DEFAULT 1 NOT NULL")
        if "analytics_included" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN analytics_included BOOLEAN DEFAULT 1 NOT NULL")
        if "inclusion_reason" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN inclusion_reason VARCHAR(64) DEFAULT 'included' NOT NULL")
        if "observation_period_id" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN observation_period_id INTEGER")
        if "assessment_purpose" not in cols:
            cur.execute("ALTER TABLE student_profiles ADD COLUMN assessment_purpose VARCHAR(32) DEFAULT 'standard' NOT NULL")
        cur.execute("PRAGMA table_info(knowledge_events)")
        knowledge_cols = [row[1] for row in cur.fetchall()]
        if knowledge_cols and "analytics_included" not in knowledge_cols:
            cur.execute("ALTER TABLE knowledge_events ADD COLUMN analytics_included BOOLEAN DEFAULT 1 NOT NULL")
        cur.execute("PRAGMA table_info(exam_scores)")
        exam_cols = [row[1] for row in cur.fetchall()]
        if exam_cols and "analytics_included" not in exam_cols:
            cur.execute("ALTER TABLE exam_scores ADD COLUMN analytics_included BOOLEAN DEFAULT 1 NOT NULL")
        if exam_cols and "inclusion_reason" not in exam_cols:
            cur.execute("ALTER TABLE exam_scores ADD COLUMN inclusion_reason VARCHAR(64) DEFAULT 'included' NOT NULL")
        if exam_cols and "observation_period_id" not in exam_cols:
            cur.execute("ALTER TABLE exam_scores ADD COLUMN observation_period_id INTEGER")
        # Add password_hash to users
        cur.execute("PRAGMA table_info(users)")
        cols = [row[1] for row in cur.fetchall()]
        if "password_hash" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR(128)")
        # Add current_class_id to users (for class switching)
        if "current_class_id" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN current_class_id INTEGER")
        # 首启/管理员建号的初始密码必须改：登录响应带上该标记，改密后清零。
        if "must_change_password" not in cols:
            cur.execute(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0 NOT NULL"
            )
        # Add teacher_id to students (multi-tenancy)
        cur.execute("PRAGMA table_info(students)")
        cols = [row[1] for row in cur.fetchall()]
        if "teacher_id" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN teacher_id INTEGER")
            # Assign existing students to first user (admin/test account)
            cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row:
                first_user_id = row[0]
                cur.execute("UPDATE students SET teacher_id = ? WHERE teacher_id IS NULL", (first_user_id,))
        # Fix empty student_no violating UNIQUE constraint
        cur.execute("UPDATE students SET student_no = NULL WHERE student_no = ''")
        # Add class_id to students (班级隔离方案B)
        cur.execute("PRAGMA table_info(students)")
        cols = [row[1] for row in cur.fetchall()]
        if "class_id" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN class_id INTEGER")
        # Add phone to students (群发家长信：手机后4位校验)
        if "phone" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN phone VARCHAR(20)")
        # Independent exam admission number used by answer-sheet identity matching.
        if "exam_no" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN exam_no VARCHAR(64)")
        cur.execute("UPDATE students SET exam_no = NULL WHERE TRIM(COALESCE(exam_no, '')) = ''")
        # Add deleted_at to teacher_classes (软删除+冷却期)
        cur.execute("PRAGMA table_info(teacher_classes)")
        cols = [row[1] for row in cur.fetchall()]
        if "deleted_at" not in cols:
            cur.execute("ALTER TABLE teacher_classes ADD COLUMN deleted_at DATETIME")

        # Add an idempotent event ledger. Existing usage_records remain the
        # compatibility baseline, so no synthetic migration events are needed.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS usage_events ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "teacher_id INTEGER NOT NULL, "
            "batch_id VARCHAR(64) NOT NULL, "
            "record_date DATE NOT NULL, "
            "count INTEGER NOT NULL, "
            "source VARCHAR(32) NOT NULL DEFAULT 'recognize', "
            "created_at DATETIME, "
            "UNIQUE(teacher_id, batch_id), "
            "FOREIGN KEY(teacher_id) REFERENCES users(id)"
            ")"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_usage_event_teacher_date "
            "ON usage_events(teacher_id, record_date)"
        )

        _migrate_student_no_unique(conn, cur)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_teacher_class_exam_no "
            "ON students(teacher_id, class_id, exam_no) "
            "WHERE exam_no IS NOT NULL AND exam_no != ''"
        )
        _migrate_accelerator_tables(cur)
        _migrate_question_bank_ownership(cur)
        type_counts = _migrate_question_bank_types(cur)
        if any(type_counts.values()):
            logger.info("Question bank type backfill: %s", type_counts)
        _migrate_question_bank_classification(cur)

        # Courseware teaching-scene metadata (introduced after the initial
        # courseware table).  Keep startup compatible with existing SQLite DBs.
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='coursewares'")
        if cur.fetchone():
            cur.execute("PRAGMA table_info(coursewares)")
            courseware_cols = {row[1] for row in cur.fetchall()}
            additions = {
                "objective": "TEXT DEFAULT '' NOT NULL",
                "misconception": "TEXT DEFAULT '' NOT NULL",
                "steps": "TEXT DEFAULT '[]' NOT NULL",
                "interaction_state": "TEXT DEFAULT '{}' NOT NULL",
                "checkpoints": "TEXT DEFAULT '[]' NOT NULL",
                "interaction_type": "VARCHAR(64) DEFAULT 'custom' NOT NULL",
                "offline_file_name": "VARCHAR(240) DEFAULT '' NOT NULL",
            }
            for name, ddl in additions.items():
                if name not in courseware_cols:
                    cur.execute(f"ALTER TABLE coursewares ADD COLUMN {name} {ddl}")

        conn.commit()
    except OperationalError as e:
        if conn is not None:
            conn.rollback()
        msg = str(e).lower()
        if "no such table" in msg or "duplicate column name" in msg:
            logger.info("Migration skipped: %s", e)
            return
        logger.warning("Migration failed: %s", e)
        raise
    except Exception as e:
        if conn is not None:
            conn.rollback()
        logger.warning("Migration failed: %s", e)
        raise
    finally:
        if conn is not None:
            conn.close()


def _migrate_accelerator_tables(cur):
    """Expand pre-release accelerator tables without touching production data."""
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accelerator_nodes'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(accelerator_nodes)")
        cols = {row[1] for row in cur.fetchall()}
        additions = {
            "access_scope": "VARCHAR(16) DEFAULT 'tenant' NOT NULL",
            "lane": "VARCHAR(32) DEFAULT 'web_gpu' NOT NULL",
            "device_id": "VARCHAR(64)",
            "queue_depth": "INTEGER DEFAULT 0 NOT NULL",
            "estimated_finish_ms": "INTEGER",
            "last_error_code": "VARCHAR(64)",
            "revoked_at": "DATETIME",
            "token_expires_at": "DATETIME",
            "token_rotated_at": "DATETIME",
        }
        for name, ddl in additions.items():
            if name not in cols:
                cur.execute(f"ALTER TABLE accelerator_nodes ADD COLUMN {name} {ddl}")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_accelerator_node_owner_lane "
            "ON accelerator_nodes(owner_user_id, lane)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_accelerator_node_owner_device_lane "
            "ON accelerator_nodes(owner_user_id, device_id, lane) "
            "WHERE device_id IS NOT NULL"
        )

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accelerator_jobs'")
    if not cur.fetchone():
        return
    cur.execute("PRAGMA table_info(accelerator_jobs)")
    cols = {row[1] for row in cur.fetchall()}
    additions = {
        "batch_id": "VARCHAR(64)",
        "paper_id": "VARCHAR(64) DEFAULT '' NOT NULL",
        "template_id": "VARCHAR(64) DEFAULT '' NOT NULL",
        "layout_snapshot_json": "TEXT DEFAULT '{}' NOT NULL",
        "layout_snapshot_sha256": "VARCHAR(64) DEFAULT '' NOT NULL",
        "lane": "VARCHAR(32) DEFAULT 'web_gpu' NOT NULL",
        "deadline_at": "DATETIME",
        "candidate_json": "TEXT",
        "candidate_sha256": "VARCHAR(64)",
        "server_validation_status": "VARCHAR(32) DEFAULT 'not_started' NOT NULL",
        "server_validation_json": "TEXT",
        "candidate_received_at": "DATETIME",
        "validated_at": "DATETIME",
        "fallback_at": "DATETIME",
        "server_artifact_path": "TEXT",
        "server_artifact_sha256": "VARCHAR(64)",
        "consumed_at": "DATETIME",
    }
    for name, ddl in additions.items():
        if name not in cols:
            cur.execute(f"ALTER TABLE accelerator_jobs ADD COLUMN {name} {ddl}")
    if "result_json" in cols:
        cur.execute("UPDATE accelerator_jobs SET candidate_json = result_json WHERE candidate_json IS NULL AND result_json IS NOT NULL")
    cur.execute("UPDATE accelerator_jobs SET status = 'candidate_received' WHERE status = 'completed'")
    cur.execute("UPDATE accelerator_jobs SET server_validation_status = 'pending' WHERE status = 'candidate_received' AND server_validation_status = 'not_started'")
    cur.execute("UPDATE accelerator_jobs SET deadline_at = datetime(COALESCE(created_at, CURRENT_TIMESTAMP), '+10 minutes') WHERE deadline_at IS NULL")
    cur.execute("DROP INDEX IF EXISTS uq_accelerator_job_source_stage_version")
    cur.execute("DROP INDEX IF EXISTS uq_accelerator_job_contract")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_accelerator_job_contract "
        "ON accelerator_jobs(teacher_id, paper_id, source_sha256, template_sha256, "
        "layout_snapshot_sha256, stage, lane, algorithm_version)"
    )


def _migrate_question_bank_ownership(cur):
    """P0-4: QuestionBank 加 teacher_id（NULL=公库）+ is_public。

    存量题（迁移前已存在的）按决策全部归入公库：teacher_id=NULL, is_public=1。
    新加的列默认 is_public=0，但存量行需显式回填为公库。
    """
    cur.execute("PRAGMA table_info(question_bank)")
    cols = [row[1] for row in cur.fetchall()]
    added_owner = False
    if "teacher_id" not in cols:
        cur.execute("ALTER TABLE question_bank ADD COLUMN teacher_id INTEGER")
        added_owner = True
    if "is_public" not in cols:
        cur.execute("ALTER TABLE question_bank ADD COLUMN is_public BOOLEAN DEFAULT 0 NOT NULL")
    if added_owner:
        # 首次引入归属：存量题统一设为公库（teacher_id 保持 NULL，is_public=1）
        cur.execute("UPDATE question_bank SET is_public = 1 WHERE teacher_id IS NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_question_bank_owner ON question_bank(teacher_id)")


def _migrate_question_bank_types(cur) -> dict[str, int]:
    """Add and strictly backfill the persisted exercise question type.

    Ambiguous or malformed legacy rows remain ``unknown`` and are excluded by
    the code-only paper selector.  Existing non-empty labels are never rewritten.
    """
    from services.exercise_bank_selector import infer_question_type

    cur.execute("PRAGMA table_info(question_bank)")
    cols = [row[1] for row in cur.fetchall()]
    added_column = "question_type" not in cols
    if added_column:
        cur.execute(
            "ALTER TABLE question_bank "
            "ADD COLUMN question_type VARCHAR(16) DEFAULT 'unknown' NOT NULL"
        )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_question_bank_type "
        "ON question_bank(question_type)"
    )

    # Some legacy/scoring-only fixtures contain only ownership columns.  They
    # still need the schema addition, but there is no content from which a
    # question type can be inferred.
    if not {"question", "answer"}.issubset(cols):
        return {"choice": 0, "fill": 0, "solution": 0, "unknown": 0}

    if added_column:
        cur.execute("SELECT id, question, answer FROM question_bank")
    else:
        cur.execute(
            "SELECT id, question, answer FROM question_bank "
            "WHERE question_type IS NULL OR TRIM(question_type) = ''"
        )
    rows = cur.fetchall()
    counts = {"choice": 0, "fill": 0, "solution": 0, "unknown": 0}
    for row_id, question, answer in rows:
        question_type = infer_question_type(question or "", answer or "") or "unknown"
        cur.execute(
            "UPDATE question_bank SET question_type = ? WHERE id = ?",
            (question_type, row_id),
        )
        counts[question_type] += 1
    return counts


def _migrate_question_bank_classification(cur) -> None:
    """Add durable classification state without blocking legacy question rows."""
    cur.execute("PRAGMA table_info(question_bank)")
    cols = {row[1] for row in cur.fetchall()}
    additions = {
        "classification_status":
            "VARCHAR(20) DEFAULT 'pending' NOT NULL",
        "classification_source":
            "VARCHAR(20) DEFAULT '' NOT NULL",
        "classification_confidence":
            "VARCHAR(10) DEFAULT '' NOT NULL",
        "classification_version":
            "VARCHAR(64) DEFAULT '' NOT NULL",
        "classification_error":
            "VARCHAR(500) DEFAULT '' NOT NULL",
        "classified_at": "DATETIME",
    }
    for name, ddl in additions.items():
        if name not in cols:
            cur.execute(f"ALTER TABLE question_bank ADD COLUMN {name} {ddl}")

    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_question_bank_classification_status "
        "ON question_bank(classification_status)"
    )

    # Existing rows with both fields populated are treated as legacy-confirmed;
    # blank rows remain pending and will be discovered by the background worker.
    # A few old import/fixture databases have only ownership columns and no
    # chapter/topic text.  They still need the new columns, but cannot be
    # inferred; leave every row pending instead of aborting startup.
    has_chapter_topic = {"chapter", "topic"}.issubset(cols)
    if has_chapter_topic:
        # Backfill both a newly added status column and partially migrated
        # installations. A populated pair is an explicit legacy/import
        # decision; only blank pairs remain eligible for the worker.
        cur.execute(
            "UPDATE question_bank SET classification_status = 'confirmed' "
            "WHERE (classification_status IS NULL OR TRIM(classification_status) "
            "IN ('', 'pending')) AND TRIM(COALESCE(chapter, '')) != '' "
            "AND TRIM(COALESCE(topic, '')) != '' "
            "AND TRIM(COALESCE(chapter, '')) NOT IN "
            "('未知', '未指定', '未标注', '未分类', '综合') "
            "AND TRIM(COALESCE(topic, '')) NOT IN "
            "('未知', '未指定', '未标注', '未分类', '综合')"
        )
        cur.execute(
            "UPDATE question_bank SET classification_status = 'pending' "
            "WHERE classification_status IS NULL OR TRIM(classification_status) = ''"
        )
    else:
        cur.execute(
            "UPDATE question_bank SET classification_status = 'pending' "
            "WHERE classification_status IS NULL OR TRIM(classification_status) = ''"
        )
    cur.execute(
        "UPDATE question_bank SET classification_source = 'legacy' "
        "WHERE classification_status = 'confirmed' "
        "AND (classification_source IS NULL OR TRIM(classification_source) = '')"
    )
    cur.execute(
        "UPDATE question_bank SET classification_source = '' "
        "WHERE classification_source IS NULL"
    )


def _migrate_student_no_unique(conn, cur):
    """P0-3: 把 student_no 的全局唯一约束改为 (teacher_id, student_no) 复合唯一.

    旧 schema 用列级 unique=True → SQLite 内联 `UNIQUE (student_no)`，生成无法
    DROP 的自动唯一索引（origin='u'）。该约束导致不同老师无法各自用相同学号。
    要去掉它只能重建表：建无 UNIQUE 的新表 → 拷数据 → 替换 → 重建索引，
    最后加 (teacher_id, student_no) 的部分唯一索引（仅非 NULL 生效）。
    """
    # 先清掉空串学号，避免被当作可重复的非 NULL 值
    cur.execute("UPDATE students SET student_no = NULL WHERE student_no = ''")

    # 检测是否仍存在 student_no 单列的自动唯一索引（origin='u'）
    cur.execute("PRAGMA index_list(students)")
    needs_rebuild = False
    for row in cur.fetchall():
        idx_name, is_unique, origin = row[1], row[2], row[3]
        if not is_unique:
            continue
        cur.execute(f"PRAGMA index_info('{idx_name}')")
        idx_cols = [r[2] for r in cur.fetchall()]
        if idx_cols == ["student_no"] and origin == "u":
            needs_rebuild = True
            break

    if needs_rebuild:
        # 标准 SQLite 表重建：保留现有列，去掉内联 UNIQUE(student_no)。
        # 重建期间必须临时关闭外键强制（H7 开启了 foreign_keys=ON），否则
        # DROP 会触发子表（weaknesses 等）的 FK 检查而失败。
        # PRAGMA foreign_keys 在事务内是 no-op，必须先 commit 关闭当前事务。
        conn.commit()
        cur.execute("PRAGMA foreign_keys=OFF")
        # legacy_alter_table=ON：让 RENAME TABLE 不把子表 FK 定义里的引用一起改名
        # （现代 SQLite 默认会传播，会导致 weaknesses 等的 FK 指向 _students_old）。
        # 这是 SQLite 官方表重建流程（含 FK 时）的标准要求。
        cur.execute("PRAGMA legacy_alter_table=ON")
        try:
            cur.execute("BEGIN")
            cur.execute("PRAGMA table_info(students)")
            col_names = [r[1] for r in cur.fetchall()]
            col_csv = ", ".join(col_names)
            cur.execute("ALTER TABLE students RENAME TO _students_old")
            cur.execute(
                "CREATE TABLE students ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "teacher_id INTEGER, "
                "class_id INTEGER, "
                "name VARCHAR(50) NOT NULL, "
                "student_no VARCHAR(50), "
                "exam_no VARCHAR(64), "
                "phone VARCHAR(20), "
                "class_name VARCHAR(50), "
                "enrolled DATE, "
                "created_at DATETIME)"
            )
            cur.execute(f"INSERT INTO students ({col_csv}) SELECT {col_csv} FROM _students_old")
            cur.execute("DROP TABLE _students_old")
            # 重建原有的普通索引
            cur.execute("CREATE INDEX IF NOT EXISTS ix_students_name ON students(name)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_students_teacher ON students(teacher_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_students_class ON students(class_id)")
            conn.commit()
            # 校验 FK 完整性（应无孤儿行）
            cur.execute("PRAGMA foreign_key_check")
            problems = cur.fetchall()
            if problems:
                import logging
                logging.getLogger(__name__).warning("FK check after students rebuild: %s", problems)
        finally:
            cur.execute("PRAGMA legacy_alter_table=OFF")
            cur.execute("PRAGMA foreign_keys=ON")

    # 建复合（部分）唯一索引：仅对非 NULL 学号生效
    cur.execute("PRAGMA index_list(students)")
    existing = [r[1] for r in cur.fetchall()]
    if "uq_students_teacher_student_no" not in existing:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_students_teacher_student_no "
            "ON students(teacher_id, student_no) WHERE student_no IS NOT NULL"
        )
