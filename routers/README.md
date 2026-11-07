# 批改接口阅读地图（routers/grade*.py）

所有 `/api/batch-grade/...` 接口都在这一组文件里。这份说明讲三件事：

- 一次批改按时间顺序经过哪些接口；
- 每个文件管什么；
- 读代码时从哪里看起。

其他 routers 不在本文范围内。

## 一次批改的完整过程

| 步骤 | 老师在做什么 | 接口 | 文件 |
|---|---|---|---|
| 0 | 打开批改页，看链路是否可用 | `GET /batch-grade/availability`，`GET /batch-grade/template/availability` | grade_availability，grade_template_availability |
| 1 | 导入模板：上传并识别模板页（SSE 流），断线后读取结果 | `POST /batch-grade/template/recognize`，`GET .../template/{template_id}/recognition` | grade_template_import |
| 1.1 | 看分栏候选和页面图，必要时重新分栏 | `GET` / `POST .../template/{template_id}/cut-candidates`，`GET .../pages/{page_index}/image` | grade_template_cut |
| 1.2 | 改定位（分栏、题号框、作答区、作图），或增删一个题号 | `PUT .../layout-overrides`，`PUT .../questions` | grade_template_layout_overrides，grade_template_questions |
| 1.3 | 确认模板（可先看能纠正哪些历史错题），再入私有题库 | `POST .../template/correction-candidates`，`POST /batch-grade/template/confirm`，`POST .../bank-import` | grade_template_corrections，grade_template_confirm，grade_template_bank |
| 2 | 上传学生卷并识别（SSE 流），断线后续识别 | `POST /batch-grade/recognize`，`POST /batch-grade/{batch_id}/recognize/resume` | grade_recognition |
| 2′ | 识别过程中，每张卷一识别完就开始批阅 | （没有单独的接口，由识别流启动） | grade_pipeline |
| 3 | 确认前逐张核对：身份候选、方向、排除失败卷、看原卷 | `GET .../identity-options`，`GET .../orientation`，`POST .../paper/{paper_id}/exclude`，`POST .../paper/{paper_id}/orientation-confirm`，`GET .../paper/{paper_id}/image` | grade_paper_checks |
| 4 | 确认批阅：剩下的卷按冻结的合同批完（SSE 流） | `POST /batch-grade/confirm` | grade_pipeline |
| 5 | 批阅后复核：人工判定、作废、入档、导出 | `/batch-grade/review/...` | grade_review |
| 6 | 看批次报告（后台生成） | `GET /batch-grade/report/{batch_id}` | grade_report |

## 文件分层

每个文件只导入在它上面的文件，不会反过来导入。`tests/test_grade_layers.py` 会检查这一点。

```mermaid
graph TD
    common[grade_common<br/>SSE、收尾、落盘、按教师加载]
    uploads[grade_uploads<br/>上传落盘、JPEG 合同、每日额度]
    availability[grade_availability<br/>链路可用性]
    report[grade_report<br/>批次报告]
    contract[grade_contract<br/>模板 → 批次的答案权威与批阅合同]
    papers[grade_papers<br/>一张卷写入批次、身份方案、原卷图片]
    pipeline[grade_pipeline<br/>逐卷批阅、确认批阅]
    checks[grade_paper_checks<br/>确认前核对]
    recognition[grade_recognition<br/>学生卷识别]
    grade[grade.py<br/>只做路由组装]
    template[grade_template.py<br/>模板路由组装，见下一节]
    review[grade_review<br/>复核]

    contract --> common
    report --> common
    papers --> common & uploads
    pipeline --> common & availability & report & contract & papers
    checks --> common & uploads & contract & papers
    recognition --> common & uploads & availability & contract & papers & pipeline
    grade --> availability & recognition & checks & pipeline & report
    template --> common & uploads & availability & contract
    review --> common & report & papers
```

grade.py 为了兼容旧名字，还从其余各层导入，图中省略了这些线。图中 `template` 一格代表下一节的整组模板文件，它们只用到 common、uploads、availability、contract 这四层。

| 文件 | 行数 | 负责什么 | 从哪里读起 |
|---|---:|---|---|
| grade_common.py | 231 | 各流程共用的底层：`_sse` 生成一条 SSE 事件；`_settle_template_component` 等后台任务收尾，不怕取消；`_save_quick_batch_meta` 在事件循环外写批次快照；`_load_owned_*` / `_locked_owned_*` 只给模板或批次的主人加载，带锁的版本在锁内重新读取 | 文件内按四段排列 |
| grade_uploads.py | 348 | `_save_batch_uploads` 在大小和数量限制内把上传写到磁盘；浏览器 `.ocr-ready-v1.jpg` 合同的检查只读文件头；`_check_limits_or_raise` 查每日图片额度 | `_save_batch_uploads` |
| grade_availability.py | 335 | 现在能不能接活：存储、视觉识别、数学批改、识别配额，需要时加上加速节点。`_require_*` 在创建或改动批改任务前做关口，不可用就返回 503 | `_require_batch_grade_chain` |
| grade_report.py | 263 | 批次报告：后台先整理知识点，再请模型写报告；批阅数据在此期间变过就重新排队 | `_start_batch_report_generation` |
| grade_contract.py | 454 | 本批次按什么答案批：模板是否仍适用、模板证据怎样变成识别缓存、批次答案表、钉住答案的批阅合同 | 文件内三段：模板 / 批次答案表 / 批阅合同 |
| grade_papers.py | 805 | 一页识别结果怎样变成一张试卷：身份、方向、题目，或待复核的占位；批阅前的检查与身份方案；原卷图片 | `_apply_ocr_outcome` → `_ingest_recognized_paper` |
| grade_pipeline.py | 1267 | 批阅只有一条路径 `_grade_paper_core`。逐卷流水线 `_PerPaperGradingPipeline` 在识别过程中每完成一张就批一张；`_grading_events` 是确认批阅的 SSE 流，把剩下的批完 | 文件开头的说明，然后 `batch_grade_confirm` |
| grade_paper_checks.py | 784 | 确认前核对试卷的 5 个接口；方向确认可以带一张转正后的图，触发对这一张的重识别 | `_confirm_orientation_locked` |
| grade_recognition.py | 1423 | 两条识别流（首次与续识别）共用一套实现：`_RecognitionLease` 管资源，`_RecognitionRun` 管进度，`_RecognitionFlavor` 记下两条流故意不同的地方 | 文件开头的说明，然后 `_initial_recognition_events` |
| grade.py | 249 | 只把上面 5 个子路由按原顺序装起来；为旧代码和测试保留从 `routers.grade` 导入旧名字的入口 | 文件开头的说明 |
| grade_template.py | 319 | 只把下一节的 8 个模板子路由按原顺序装起来；为旧代码和测试保留从 `routers.grade_template` 导入旧名字的入口 | 文件开头的说明 |
| grade_review.py | 968 | 批阅后的复核、作废、入档与导出 | `batch_grade_save_manual_review_decision` |

### 模板这一组（grade_template_*.py）

一份模板依次经过：导入识别 → 分栏 → 改定位 / 增删题号 → 确认 → 入题库。这一组按同样的规则分层，`tests/test_grade_layers.py` 一并检查。

```mermaid
graph TD
    models[grade_template_models<br/>请求体、题目被拒的原因]
    runs[grade_template_layout_runs<br/>布局记录对每页说了什么]
    artifacts[grade_template_artifacts<br/>资产跟着题目走]
    tavail[grade_template_availability<br/>模板链路可用性]
    corrections[grade_template_corrections<br/>历史错题纠错]
    steps[grade_template_import_steps<br/>导入的节奏与失败]
    form[grade_template_import_form<br/>上传表单与导入任务]
    pages[grade_template_import_pages<br/>一页识别结果]
    imp[grade_template_import<br/>导入流]
    cut[grade_template_cut<br/>分栏候选]
    overrides[grade_template_layout_overrides<br/>保存定位]
    questions[grade_template_questions<br/>增删题号]
    confirm[grade_template_confirm<br/>确认模板]
    bank[grade_template_bank<br/>入私有题库]
    gt[grade_template.py<br/>只做路由组装]

    corrections --> models
    form --> steps
    pages --> steps & form & artifacts
    imp --> steps & form & pages & tavail
    cut --> runs & models
    overrides --> runs & artifacts & models
    questions --> runs & artifacts & models
    confirm --> runs & artifacts & models & corrections
    gt --> tavail & corrections & imp & cut & overrides & questions & confirm & bank
```

图中省略了它们对 grade_common、grade_uploads、grade_availability、grade_contract 的依赖，以及 grade_template.py 为兼容旧名字从各模块的导入。

| 文件 | 行数 | 负责什么 | 从哪里读起 |
|---|---:|---|---|
| grade_template_models.py | 188 | 模板接口从老师那里收什么：各接口的请求体；提交的题目不符合答案合同时，给老师看的原因 | `TemplateConfirmReq` |
| grade_template_layout_runs.py | 396 | 会话的布局记录（`layout_cut_runs`）对每一页说了什么：复核状态与待复核页、可带入新一轮的复核草稿、最新草稿、已冻结的分栏方案 | `_template_geometry_review_state` |
| grade_template_artifacts.py | 207 | 页面资产跟着本页题目走：按哪些题号建、改题号后重建、按已确认的框重新生成差分图、作图标签校验 | `_synchronized_page_artifact` |
| grade_template_availability.py | 150 | 模板导入现在能不能做：可用性接口；导入前的关口要求核心批改链路可用，远程版面服务只作参考 | `_require_template_recognition_chain` |
| grade_template_corrections.py | 293 | 这份模板可以纠正哪些历史错题：给老师看的候选，以及确认模板时在同一个事务里保存的授权 | `_save_template_correction_authorizations` |
| grade_template_import_steps.py | 233 | 导入的节奏：按页数计的总时限、一次只等一个慢步骤并持续发心跳、超时或识别失败时给老师看的错误 | `_TemplateImportSteps` |
| grade_template_import_form.py | 270 | 开始一个导入任务：先校验上传表单，再保存页面；保存失败就删掉整个模板目录 | `_parse_template_import_form` |
| grade_template_import_pages.py | 338 | 一页识别结果在会话里变成什么：整卷编号后的题目、导入布局记录里的一页、冻结的页面资产 | `_recognized_template_page` |
| grade_template_import.py | 737 | 导入流本身：后台任务把每一页做完矫正、版面、模型和冻结，SSE 只订阅有界的进度；状态接口读取已保存的结果 | 文件开头的说明，然后 `_template_import_events` |
| grade_template_cut.py | 492 | 分栏候选：读最近一次布局记录、给裁题核对的页面图；默认复用已冻结的分栏方案，老师强制时才请版面模型 | `batch_grade_template_cut_candidates` |
| grade_template_layout_overrides.py | 456 | 保存一页的定位：分栏成为布局草稿的新一版，题号框、作答区、作图几何一起换掉；改过的题目先通过答案合同校验才保存 | `batch_grade_template_layout_overrides` |
| grade_template_questions.py | 217 | 增删一个题号：增加时之后各题顺延（可跨页），删除时不重排；题号变了的页面重建资产 | `batch_grade_template_question_edit` |
| grade_template_confirm.py | 517 | 确认模板：提交的页面变成冻结的答案合同，资产和裁剪方案冻结到确认后的题目，纠错授权先于会话保存；在学生卷开始识别前可以反复确认 | 文件开头的说明，然后 `batch_grade_template_confirm` |
| grade_template_bank.py | 150 | 已确认的模板只入一次私有题库 | `_template_bank_import` |

## 读代码时的几个约定

- **状态存在哪里。**
  - 批次状态是 `services.quick_grader.QuickBatch`，存成 `uploads/quick_batch_{id}/meta.json`。
  - 模板状态是 `services.template_session.TemplateSession`，存成 `uploads/template_{id}/meta.json`。
  - 两者都是先写临时文件再原子替换（tmp + `os.replace`）。改动前要先拿 `batch_operation_lock`，拿到锁后重新读取。
- **`_require_*` 是关口。** 条件不满足就抛 HTTPException，宁可停下也不猜（fail closed）。读主流程时可以先跳过它们的函数体。
- **`*_events` 是 SSE 生成器。**
  - 每条事件都经过 `_sse` 生成，等待时按 `_GRADE_HEARTBEAT_SECONDS` 发心跳。
  - 客户端断开时，需要做完的收尾（落盘、关闭流水线）放在单独的任务里，由 `_settle_template_component` 等它结束，再次取消也不会打断。
- **日志名。** 从 grade.py 搬出来的代码仍用 `logging.getLogger("routers.grade")`，从 grade_template.py 搬出来的仍用 `logging.getLogger("routers.grade_template")`，所以日志来源名和拆分前一样。
- **旧名字。** `routers.grade.X` 和 `routers.grade_template.X` 仍然可以导入，但新代码请直接从定义它的文件导入。
- **子路由。** 有接口的模块各自建 `router`，由 grade.py 或 grade_template.py 按原来的路由顺序包含。`tests/test_route_table_identity.py` 拿路由表和 OpenAPI 文档与冻结的契约比对，接口的顺序、路径、方法、处理函数名变了都会失败。

## 测试里替换全局量

- 这一组文件各自绑定了 `UPLOAD_DIR` 等名字。如果只 patch `routers.grade` 上的那份，搬走的代码就看不到。
- `tests/patch_reach.py` 会让这种"打不到读取方"的 patch 直接失败，并点名漏掉的模块。
- 需要替换的名字有好几个模块在读时，用 `tests/grade_family.py` 的 `set_grade_global(monkeypatch, name, value)`，它会打到 grade 族里所有读这个名字的模块。
- 只有一个模块在读时，直接 patch 那个模块即可。
- 模板这一组也属于 grade 族：`set_grade_global` 同样会替换 grade_template_*.py 里读这个名字的模块，并且先替换子模块、再替换它们所属的模块（patch_reach 要求的顺序）。
- 包装一个已经被替换的函数时（先取原函数再包一层），要从实际读取它的模块去取，不要从旧路径 `routers.grade` 或 `routers.grade_template` 取：旧路径上的绑定不会被替换，取到的是真实实现。
- patch_reach 把测试代码里对 `模块.X` 的读取也算作“有人在读”。所以测试只要读一下旧路径上的名字，护栏就会放过一个其实打不到读取方的 patch；改指 patch 时，以实际读取这个名字的模块为准。
