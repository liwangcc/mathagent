import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import QuestionBank
from services.answer_import import import_from_json


def test_json_import_persists_explicit_and_inferred_question_types(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'answer-import.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    source = tmp_path / "questions.json"
    source.write_text(
        json.dumps(
            [
                {
                    "question": "函数值为（ ） A. 1 B. 2 C. 3 D. 4",
                    "answer": "A",
                    "question_type": "选择题",
                },
                {
                    "question": "函数值为________________。",
                    "answer": "3",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = import_from_json(session, source, teacher_id=9)

    assert result == {"imported": 2, "skipped": 0}
    rows = session.query(QuestionBank).order_by(QuestionBank.id).all()
    assert [row.question_type for row in rows] == ["choice", "fill"]
    session.close()
