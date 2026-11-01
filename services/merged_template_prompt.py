"""合并模板识别提示词（目录 + 统一作答框 + 独立求解 + 作图字段，一次调用）。

状态：历史单次调用合同，仅供显式回放和兼容测试。生产模板使用 Flash 候选清单与两个 Max 并行合同，见 services/template_max_owned.py。
历史接收方式见 services/merged_template_receive.py；下面的实测记录不替代当前版本的端到端验收。

本模块由 2026-09-11 的 v4 实测版提示词固化而来（验证记录：outputs/template-check-20260911/
ab-test/merged-v4-{primary,lishuo}/，小学卷 2.3 双解回归 + 周涛 25 题无回归；四源答案交叉一致）。
2026-09-11 晚 v5：末级节点强制输出 question_text 完整题干（验证记录：
ab-test/merged-v5-primary/；接收侧不得再用 context_summary 摘要替代）。

要点（与旧版差异）：
- 统一作答框 regions：不再输出 exemptions；有手写痕迹时必须完全覆盖（第一原则），
  其次尽量不框印刷题干；
- 同一调用内独立求解：standard_answer + solution_steps；多合法答案必须全部列出；
- 作图/混合题同调用返回 drawing_regions + label_anchor；非作图题返回 []/null；
- 作图/混合题只返回 drawing_regions + label_anchor 和参数化 standard_answer；不要生成或返回 drawing_reference 图片/笔画，参考图由教师在编辑器中按需生成；
- answer_kind=drawing 时 regions 必须为 []，只返回 drawing_regions；answer_kind=mixed 时 regions 只框文字/计算作答处，drawing_regions 只框作图区；
- 输入为同一模板的两个视图（原图 + 坐标网格图），坐标一律 normalized_1000_v1。

历史回放可用 build_merged_template_prompt(...) 生成对应提示词，再交给合并接收器。
生产模板不再自动回退到这条路线。
"""
from __future__ import annotations

from services.question_directory import _dimensions, _stage

_MERGED_PROMPT_BODY = """你负责建立数学试卷的题目层级目录、定位各末级作答项的统一作答框，并独立求解标准答案。不要把手写答案当印刷题干。

这份目录将成为另一模型解题和学生答案识别的共同依据。保留原卷结构，不根据惯用试卷格式重编或补造题号：
1. 先区分标题与题目。“一、选择题”等只说明分类且统领多道题的标题为 section；带共同材料、图形和下属小问的节点为 group；实际题目为 question；一道题中分别填写独立答案的空格为 answer_item。不要根据数字写法判断层级，1、(1)、① 都必须结合上下文。
2. printed_label 只抄该节点实际印刷的编号，如“一”“1”“(1)”“①”。允许不同父节点下重复编号。只有在确实没有任何适用的印刷编号时才返回 null，并列入 needs_review。
3. 若一个带编号的题目标题下面仅有一道未另行编号的综合题，应把标题、编号与实际题干作为同一个 question 节点。不要制造“有编号的section/group + 唯一无编号question”这一空层级。例如分类性修饰文字、题材标签、难度说明并不必然意味着还应建立一个独立子题。只有明确存在多个独立子题时才建立 children；不能为了凑层级产生无编号节点。
4. 下属明确编号的小问保留为 children；一个小问内有多个独立填写位置，再用 answer_item 分开。一份坐标答案 (x,y) 是一个答案，不把其中两个数字拆开。图内的点号、物体编号、选项 A/B/C/D 不是新题，不能把在图中重复出现的编号重复建节点。
5. 没有明确子题编号的一道综合题，即使要求画图、计算、描述多个内容，仍保留为一个 question，不自行按动作拆题。其多处作答位置放在同一个 regions 数组中。
6. section/group/question 可以有 children；只有没有 children 的 question 或 answer_item 才是可独立转录与判断的末级作答项。有 children 的节点只组织共同题干，不再重复输出一份末级答案。
7. context_summary 用简短文字说明本节点印刷题干或共同材料，保留父子关系。共享图形可以被多个小问引用；图的位置可以早于小问，不能据此把图归给最近的问。不要把学生姓名写入目录。
8. 每个末级作答项还要输出 question_text：该题完整印刷题干原文（含全部条件、数值与单位），供解题、判分与学生卷核对使用。question_text 必须是完整题干，不得用 context_summary 的简短摘要代替，也不得省略条件；确实无法辨认完整题干时，照抄可辨认部分并列入 needs_review。
9. 每个末级作答项输出 answer_kind：普通文字/选择/填空为 non_graphic；纯作图为 drawing；作图并要求文字或计算的为 mixed。requires_drawing=true 时 answer_kind 不得为 non_graphic。

位置合同：
- 每个节点可返回 number_bbox，框住该节点实际印刷编号；找不到则 null。
- 每个末级作答项返回 question_type=choice|fill|solve 与统一作答框 regions（不再区分作答区与豁免框）：框选作答区域；模板上有手写痕迹时，作答框必须完全覆盖手写痕迹——这是第一原则；在满足第一原则的前提下，尽量不要框选到印刷的文字题干。不能为了凑数量创造框。
- 纯作图 answer_kind=drawing 只框 drawing_regions，regions 必须为 []，不要给同一作图区再套作答框。图文混合 answer_kind=mixed 的 regions 只框文字/计算，drawing_regions 框作图及必需参照；两者不能重复。学生手画的单位长度/比例尺也是作图内容，即使画在主图外也必须保留在 drawing_regions 中。
- choice_mode 从该题及所属分类说明中判断为 single（单选）、multiple（多选）或 unknown（无法确定）；非选择题为 unknown。无法确定选择模式时列入 needs_review，不默认所有选择题都是单选。此字段属于目录，不交给解题模型重新决定。
- 选择题定位答案括号或涂写处，填空定位填写槽，图形作答包含作图痕迹；覆盖手写痕迹优先的统一作答框第一原则对所有题型一致适用。普通文字简答题的 Q 标记之后由程序固定放在 number_bbox；作图/混合题的实际作图区域与 Q 锚点按下面规则一并返回，不改变题目层级。
- 每个末级作答项判断 requires_drawing（true|false）：题干或图例带插图、出现网格坐标图，都不等于要求作图；只有题目要求在图上描点、标记、连线、画路线、补全图形或按比例作图时才为 true。确实无法判断时返回 null 并列入 needs_review，此时程序会再作一次作图定位，不要猜测。
- 所有框为整张当前输入图片的 normalized_1000_v1 坐标 [左,上,右,下]，x除以整图宽、y除以整图高再乘1000。红色坐标网格不是试卷内容。
- needs_review 只记录确实无法确定、需要教师作选择的问题；正常处理过程、已经确定的分组解释不要放入 needs_review。无法确定时仍必须如实报告，不能为了通过校验隐瞒。

只返回 JSON。顶层 nodes 按阅读顺序，children 也按阅读顺序。所有节点均使用以下字段：
{"coordinate_space":"normalized_1000_v1","nodes":[{"printed_label":"一","kind":"section","context_summary":"选择题","number_bbox":[10,10,30,30],"question_type":null,"choice_mode":"unknown","regions":[],"children":[{"printed_label":"1","kind":"question","context_summary":"该题印刷条件的简短摘要","question_text":"该题完整印刷题干原文","number_bbox":[20,40,35,60],"question_type":"choice","choice_mode":"single","requires_drawing":false,"answer_kind":"non_graphic","regions":[[300,40,360,80]],"drawing_regions":[],"label_anchor":null,"children":[]}]}],"needs_review":[]}
- 作图/混合题定位：额外返回 drawing_regions（只列实际作图位置，含判读所需网格、刻度、点号、方向标识与图例，不把同题的文字解答或计算区并进来；第一个框为主作图区）与 label_anchor（Q 标签右缘中点：放在主作图区外的左侧并贴近它，纵向取图左缘合适位置；标签约占100×40像素，请预留左侧空间）。确实无法确定归属或没有合适标注位置时列入 needs_review。非作图题返回 drawing_regions=[]、label_anchor=null。

【同一调用内还需完成：独立求解】
本次两张图片是同一张模板的两个视图：第一张是不带坐标网格的原图（读题与求解依据）；第二张是叠印红色坐标网格的整页（网格仅坐标参照，不是试卷内容；坐标一律按整张模板 normalized_1000_v1 报告）。
除目录外，每个末级作答项在同一 JSON 内同时给出：standard_answer（choice 用选项字母；fill 用完整填写结果；solve 用简洁完整答案；作图题用坐标/路线/顶点序列等参数化描述，不生成图片）与 solution_steps（1~3 条核验步骤）。模板上的手写答案、作图、演算不是标准答案；必须依据印刷条件独立求解，不能照抄。若同一要求存在多个合法答案或合法位置（例如作图点可以标在多个不同格点、答案有多个可行值），standard_answer 必须逐一列出全部合法答案（如“④可标在(6,5)处或(0,7)处”；此例仅示格式），不得只给其中一个。
示例（片段）：{"printed_label":"1","kind":"question","context_summary":"…","question_text":"完整题干原文","number_bbox":[20,40,35,60],"question_type":"choice","choice_mode":"single","requires_drawing":false,"regions":[[300,40,360,80]],"standard_answer":"B","solution_steps":["按印刷条件核验符号与取值"],"children":[]}
"""


def build_merged_template_prompt(image_width: int, image_height: int, *, education_stage: str = "senior") -> str:
    """合并版提示词：目录+统一作答框+解题+作图字段（一次调用）。"""
    parts = (_MERGED_PROMPT_BODY, _stage(education_stage), _dimensions(image_width, image_height))
    return chr(10).join(parts)
