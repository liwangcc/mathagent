from services.paper_cache import PaperCache


def _cache() -> PaperCache:
    return PaperCache.from_confirmed_template_pages([
        {
            "page_index": 0,
            "questions": [
                {
                    "question_number": "1",
                    "question_text": "first",
                    "question_type": "choice",
                },
                {
                    "question_number": "2",
                    "question_text": "second",
                    "question_type": "fill",
                },
                {
                    "question_number": "3",
                    "question_text": "third",
                    "question_type": "solve",
                },
            ],
        }
    ])


def test_anchor_chunk_prompt_requires_dynamic_order_and_local_ownership():
    prompt = _cache()._build_evidence_only_prompt(
        "page_1",
        evidence_strategy="continuous_question_crop_plan",
    )

    assert '题号清单=["1","2","3"]' in prompt
    assert '"1":"choice:unknown"' in prompt
    assert "中国高中数学试卷 OCR" in prompt
    assert "严格只返回 JSON" in prompt
    assert "按图原样转录" in prompt
    assert "模板差分后的学生卷图" in prompt
    assert "残留印刷内容" in prompt
    assert "按图原样转录" in prompt
    assert "蓝色 Q 题号" in prompt
    assert "MiMo" not in prompt
    assert "Qwen" not in prompt
    assert "DeepSeek" not in prompt
    assert "严格只返回 JSON" in prompt


def test_legacy_answer_box_prompt_remains_available_and_unknown_strategy_fails_closed():
    cache = _cache()

    legacy = cache._build_evidence_only_prompt(
        "page_1",
        evidence_strategy="confirmed_bbox_neighbor_clamped",
    )

    assert "student_transcription_only" in legacy
    assert "student_answer" in legacy
    assert "只抄可见笔迹" in legacy
    assert "同一页" in legacy
    assert "严格只返回 JSON" in legacy
    assert cache._build_evidence_only_prompt(
        "page_1",
        evidence_strategy="unsupported-shape",
    ) == ""
