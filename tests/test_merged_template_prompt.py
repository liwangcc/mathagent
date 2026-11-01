"""合并版提示词（services/merged_template_prompt）的结构与冻结快照测试。

冻结哈希 = 2026-09-11 v4 实测版提示词的字节级快照：
  - primary 1477×2064 → 小学卷验证（2.3 双解回归、作图字段/锚点对照）
  - senior  2064×1564 → 周涛卷验证（25 题无回归、坐标帧不变）
2026-09-11 晚 v5 修订：末级节点新增 question_text（完整题干）字段；快照随本次
有意修改同步更新（验证记录：outputs/template-check-20260911/ab-test/merged-v5-primary/）。
2026-09-13：纯作图只输出 drawing_regions；小学实测记录位于本次验收档案
primary-verified-20260913/round-2（Max 与 Flash 并行，保留实际请求与响应）。
高中快照仅验证学段切换后的提示词契约，不表示本次做过高中上游实测。
提示词正文如有任何有意修改，必须同步更新该快照并重新走验证记录。
"""
import hashlib

import pytest

from services.merged_template_prompt import build_merged_template_prompt

FROZEN = {
    "primary": ("99221d851b364f800775628e8fe9183417f4dadcf4e820c6617808fb44823978", 1477, 2064),
    "senior": ("b4e34f158ca056906e54e177d9230b2ed00736eca55dbbb49a421928de426d19", 2064, 1564),
}


def test_current_prompt_snapshots():
    for stage, (digest, width, height) in FROZEN.items():
        built = build_merged_template_prompt(width, height, education_stage=stage).encode("utf-8")
        assert hashlib.sha256(built).hexdigest() == digest


def test_merged_prompt_contract_phrases():
    p = build_merged_template_prompt(2064, 1564, education_stage="senior")
    assert "统一作答框" in p
    assert "完全覆盖手写痕迹" in p and "第一原则" in p
    assert "exemptions" not in p
    assert '"drawing_regions"' in p and '"label_anchor"' in p
    assert '"question_text"' in p
    assert "不得用 context_summary 的简短摘要代替" in p
    assert "必须逐一列出全部合法答案" in p
    assert "【同一调用内还需完成：独立求解】" in p
    assert "本次学段：高中，学科：数学。" in p
    assert "本次整张输入图片实际尺寸：宽=2064，高=1564。" in p


def test_stage_switch_primary():
    p = build_merged_template_prompt(1477, 2064, education_stage="primary")
    assert "本次学段：小学，学科：数学。" in p
    assert "宽=1477，高=2064" in p


def test_unknown_stage_rejected():
    with pytest.raises(ValueError):
        build_merged_template_prompt(1000, 1000, education_stage="kindergarten")
