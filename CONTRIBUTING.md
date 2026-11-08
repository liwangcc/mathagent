# 贡献指南

MathAgent 以 **AGPL-3.0** 开源发布，并以商业双许可提供商用授权。协作前请确认没有把真实密钥、学生数据、生产日志或内部地址带入补丁。

## 开始

- 先读 README、相关源码和测试；整体能力与已知边界以 README 与代码为准。
- 使用 `.env.example` 准备本地配置；真实 `.env`、数据库、上传文件和导出档案不得提交。
- 测试只能使用合成或已脱敏样本；涉及身份、归属、上传下载、教师确认或评分安全的改动必须补回归测试。

## 开发与验证

1. 保持改动聚焦，不顺手格式化无关文件。
2. 后端运行：
   ```bash
   env -u PYTHONPATH ./venv_local/Scripts/python.exe -m pytest -q
   ```
3. 前端运行：
   ```bash
   cd frontend
   npm run test
   npm run build
   ```
4. 改动页面交互时补充或运行 `frontend/tests/*.browser.mjs`（启动方式见脚本头部注释；只用合成数据）。
5. 提交前运行 `git diff --check`、`git status --short`，确认没有敏感或生成文件。
6. 运行 `python scripts/check_file_sizes.py`，遵守下节的 900/1800 源文件预算。文件缩短后用 `--update` 记录，并与相应源码一起暂存；不得抬高登记额度或使用 `--accept-growth`。
7. 说明行为变化、实际测试命令、退出状态和未覆盖风险。

## 源文件规模：仓库硬性标准

- **新建或未登记为历史大文件的源文件最多 900 行。** 拆出的每个新模块同样适用。
- **本次修改的历史源文件，PR 最终版本最多 1800 行。** 已登记的大文件仍只减不增：例如原来 1700 行，可以保持行数修改，不得借新上限继续长到 1800 行。
- 用户批准保留的六个历史文件保持现状，不作为待拆任务：`experiments/template_ocr_lab/core.py`、`services/question_classification.py`、`desktop/worker/mathagent_worker/handoff.py`、`services/correction_tracking.py`、`services/accelerator_business.py`、`services/model_concurrency.py`。它们符合 1800 上限；保留现状不是增加登记额度的授权。
- 未触碰的其他旧文件不要求顺手重构。分阶段拆分时，本地 `--staged` 允许超过 1800 的旧文件在中间提交中严格缩短，但 PR 最终态仍须降到 1800 以内。
- 基线只允许收紧或删除，不能与源码一起抬高、自行新增超限例外，也不能先缩短后在后续提交中涨回。迁移到 900 时，仅移除已在 900 以内的登记，不抬高保留条目的额度。
- 按职责与单向依赖拆分，保留旧接口和行为测试；不压行、不删注释或放宽测试来凑数字。

统计范围沿用原门禁：Git 跟踪的 Python 源文件，以及 `frontend/src/` 下的 Vue、JavaScript、TypeScript；测试文件不计入。按物理行统计，包含注释、空行和无末尾换行的最后一行；CSS、文档和生成产物不因此新增治理范围。

```bash
# 新克隆启用本地钩子；各 worktree 使用各自检出的钩子与脚本。
git config core.hooksPath .githooks
python scripts/check_file_sizes.py
# 更新缩短后的基线，再与对应源码一起暂存。
python scripts/check_file_sizes.py --update
# 与可信 PR 基点比较最终态，并检查提交历史中的基线收紧。
python scripts/check_file_sizes.py --base-ref <PR基点SHA>
```

本地钩子检查真实候选索引并对照 HEAD；普通提交、`commit -a`、`commit <path>` 使用同一入口。普通 CI 获取完整历史，对照 PR 基点或 push 前的提交检查，显式比较 ref 无效时失败，不退成无约束模式。新建 `ci/**` 分支首次 push 的 before 为全零时，工作流改用与 `origin/main` 的共同祖先。

所有面向 main 的 PR（包括其他人的 fork PR）另受「源文件规模硬门禁」约束，并由分支保护设为必需状态。该检查使用 **main 已有的工作流和基点中的可信检查器**，通过 `--base-ref` / `--head-ref` 只读取 PR 的 Git 对象；不检出、安装或执行 PR 中的 Python、测试、工作流和依赖。它只授予源码读取及发布该提交检查状态所需的权限，不使用仓库密钥，也不读取缓存。因此 PR 同时篡改检查器、删除工作流、抬高基线或跳过本地 hook，都不能自行豁免超限代码。检查结果绑定确切的 PR head SHA，未完成或失败不能合入。仓库管理员仍能主动修改保护设置；这是仓库权限边界，不是贡献者可以使用的放行口。

## 业务底线

- 不绕过鉴权、教师归属、教师确认、模板冻结或模型调用边界。
- 证据不足时返回明确的待确认/失败状态，不用猜测填充正式成绩。
- 新增依赖、接口或配置时说明用途、兼容性和供应链风险。

## 贡献者许可（CLA）

项目同时以 AGPL-3.0 和商业许可分发，因此贡献需要覆盖两种分发方式。提交 Pull Request 即表示你确认并同意：

1. 你有权提交该贡献，且该贡献不侵犯任何第三方权利。
2. 你授予维护者一项永久、全球范围、免费、非独占、不可撤回的许可，允许维护者使用、复制、修改、再许可和分发你的贡献，**包括以商业许可方式分发**。
3. 你保留自己贡献的版权；本条款是许可授予，不是版权转让。
4. 你的贡献按 AGPL-3.0 随项目一并发布。

请在提交时使用 `git commit -s` 附加 DCO 签名（`Signed-off-by`），作为上述确认的轻量凭据。PR 模板中的许可勾选项必须勾选。
