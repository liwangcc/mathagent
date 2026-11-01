# Template OCR Lab

这是学生答案识别的独立实验模块。它只做模板编译、整页配准、印刷层抑制、
学生笔迹证据提取、外部视觉回调编排和质量评估，不导入生产服务，也不内置
供应商、密钥或网络客户端。在识别正确率、安全性和人工复核率达到门槛前，
不应接回生产链路。

## 环境与输入

在仓库根目录使用项目已有 Python 环境运行：

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli --help
```

上线候选必须使用真实空白模板：电子版空白试卷图片、未填写实拍图均可；它必须
与学生卷版式一致且不含任何学生答案。多张学生卷重建共识模板只用于研究，无法
保证多人写在同一位置的相同答案不会被误吸收到印刷层。

`--rotate-clockwise` 的单位是顺时针四分之一圈：`0`、`1`、`2`、`3` 分别表示
0、90、180、270 度。该方向会写入模板清单，之后提取证据时自动复用。

模板编译命令还支持 `--preset balanced|fast|quality`，默认使用 `balanced`。
档位参数会完整写入 `manifest.json`，后续 `evidence` 和 `benchmark` 自动复用，
不能在同一工件的处理中途切换：

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli compile-blank `
  --image "D:\ocr-samples\blank.jpg" `
  --output ".\outputs\template-ocr-lab\artifact" `
  --preset balanced
```

此前在 10 张授权样本和研究用 `student_consensus` 工件上，将 OpenCV 限制为 2 个
CPU 线程后得到以下实验基线。这里没有使用 GPU；计时覆盖单张配准和证据提取，不含
一次性模板加载、OCR/API 和批阅，仅用于比较档位，不代表 2 核服务器的端到端耗时。
在加入模板绑定、歧义墨迹和配准硬门禁后，当前版本仍需用真实空白模板重新测量：

| 档位 | 图像/特征最长边 | 最大特征数 | 平均预处理耗时 | 配准通过 | 最低边缘召回 |
|---|---:|---:|---:|---:|---:|
| `quality` | 2200 / 1280 | 5000 | 4.744 秒/张 | 10/10 | 0.9011 |
| `balanced` | 1800 / 960 | 3500 | 2.322 秒/张 | 10/10 | 0.9320 |
| `fast` | 1600 / 800 | 3000 | 1.607 秒/张 | 10/10 | 0.8118 |

因此当前默认采用 `balanced`；`fast` 仅作实验档，不能仅凭这 10 张样本接入生产。
按单进程线性外推，300 张 `balanced` 约需 11.6 分钟纯预处理，尚未包含上传、OCR、
批阅、数据库和队列耗时；这只是容量估算，不是 300 张实测。模板特征会在同一批次
复用，避免每张试卷重复计算模板哈希和基准 SIFT 特征。缓存后曾观察到 `quality`
模板编译约从 27 秒降至 15.8 秒；该组数据与上表不是同一线程条件，不能直接横比。

## 1. 编译真实空白模板

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli compile-blank `
  --image "D:\ocr-samples\blank.jpg" `
  --output ".\outputs\template-ocr-lab\artifact" `
  --preset balanced `
  --rotate-clockwise 1
```

生成清单的 `source_mode` 为 `verified_blank`，`answer_preservation_safe` 为 `true`。
只有这种工件能通过答案保真来源门禁。无需扫描仪，但电子版必须先转为与学生照片
相同页型的图片；双页同拍时，空白模板也应保持相同的双页布局。

## 2. 编译研究用共识模板

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli compile `
  --images "D:\ocr-samples\*.jpg" `
  --output ".\outputs\template-ocr-lab\artifact" `
  --preset balanced `
  --rotate-clockwise 1
```

该命令至少需要 3 张同一页型、方向一致的试卷照片。生成清单的 `source_mode` 为
`student_consensus`，`answer_preservation_safe` 固定为 `false`，不能作为上线工件。
证据提取默认拒绝这种工件；仅做研究时可显式传
`--allow-unsafe-consensus`，其结果仍会全题进入复核且无法通过上线门禁。
为避免 4 GB 机器因多组整页数组耗尽内存，共识编译最多接受 24 张研究图片；正式
批量不是把 300 张学生卷一起编译，而是编译 1 张真实空白模板后逐张流式提取证据。

输出目录包含：

- `manifest.json`：模板版本、参数、输入方向、文件哈希和两轮配准统计；
- `template.png`：由多份同页型试卷重建的共识印刷层；
- `print_mask.png`：印刷内容掩码；
- `registration.png`：仅供本地特征配准使用的基准图，不会发送给 OCR。

清单记录三个图像工件的 SHA-256。后续文件被替换或损坏时，证据提取会拒绝使用。

## 3. 提取单张试卷证据

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli evidence `
  --manifest ".\outputs\template-ocr-lab\artifact\manifest.json" `
  --image "D:\ocr-samples\paper-01.jpg" `
  --output ".\outputs\template-ocr-lab\paper-01" `
  --sample-id S01
```

输出包括对齐图、二值笔迹掩码、只保留笔迹的证据图和诊断 JSON。`sample-id` 应使用
匿名编号，不要把学生姓名写入实验产物文件名。

如已有题目归属区域，可同时生成分组证据图：

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli evidence `
  --manifest ".\outputs\template-ocr-lab\artifact\manifest.json" `
  --image "D:\ocr-samples\paper-01.jpg" `
  --output ".\outputs\template-ocr-lab\paper-01" `
  --sample-id S01 `
  --regions ".\experiments\template_ocr_lab\regions.example.json" `
  --sheets-output ".\outputs\template-ocr-lab\paper-01\sheets"
```

`--regions` 与 `--sheets-output` 必须同时提供。区域坐标使用归一化的 `0..1000`
坐标系，优先读取 `ownership_bbox`，也兼容 `answer_bbox` 或 `bbox`：

```json
{
  "artifact_registration_sha256": "从该工件 manifest.json 复制 registration_sha256",
  "questions": [
    {
      "question_number": "1",
      "question_type": "choice",
      "ownership_bbox": [40, 120, 480, 190]
    },
    {
      "question_number": "2",
      "question_type": "fill",
      "ownership_bbox": [520, 120, 960, 190]
    }
  ]
}
```

`question_type` 只能是 `choice`、`fill` 或 `solve`，题号不可重复。分组图每行左侧只画
固定 `Q` 题号，右侧只画对应所有权区域中的笔迹；区域默认不扩张，避免相邻题重复。
所有权区域不得有面积重叠，共享边界会映射到同一个像素坐标。区域文件必须绑定当前
工件的 `registration_sha256`；误用其他页型或重新编译前的旧区域文件会在 OCR 前
直接失败。仓库中的 `regions.example.json` 使用全零占位哈希，只展示格式，实际运行
前必须替换。CLI 同时返回每题 `blank|uncertain|visible`、边界触碰比例等本地诊断。

## 4. OCR 合同

`recognition.py` 不绑定供应商、不读取密钥，只接受调用方注入的异步视觉回调。回调
签名为 `(image_path, prompt, policy)`；不可变 `policy` 固定要求
`enable_thinking=false`、`temperature=0` 和严格 JSON，供应商适配器必须把这些要求
落实到实际请求，不能自行打开思考模式。提示词不给题干和标准答案，只说明“老师要
亲自批阅原始答案”，并固定当前分组的题号顺序。

OCR 响应必须是纯 JSON，题号数量、值和顺序必须完全一致。缺题、重复、乱序、数字型
题号、Markdown 包裹或额外字段都会整组失败，程序不会排序、补空或沿用上次结果。
真实空白工件中，本地墨迹状态与 OCR 空/非空冲突、不可读、选择题非单一 A/B/C/D、
笔迹触碰所有权边界时，才定向进入复核。`answer_preservation_safe` 不为 `true` 的
共识工件会把每一道题标记为 `unsafe_artifact_source` 并全部送入复核，不能以此
测量可接受的人工复核率。

调用方应使用 `recognize_evidence()` 主入口，而不是自行拼接分组、题号和安全标志。
该入口从证据中读取工件哈希与 `answer_preservation_safe`，校验区域归属，按同一顺序
生成证据表、调用 OCR 并复核结果。底层解析函数只用于单元测试和供应商适配器开发。

## 5. 批量评估配准

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli benchmark `
  --manifest ".\outputs\template-ocr-lab\artifact\manifest.json" `
  --images "D:\ocr-samples\*.jpg" `
  --output ".\outputs\template-ocr-lab\benchmark.json"
```

报告始终打印到标准输出；指定 `--output` 时也会写入 JSON。当前代码中的配准门槛为：

- 配准通过率至少 95%；
- 所有通过样本的最小模板边缘召回率至少 0.72。

单张证据提取的模板边缘召回低于 0.72 时也会立即失败，不会继续生成可接受的 OCR
结果。新增的配置字段属于实验工件合同；旧实验工件字段不完整时应重新编译，不能
沿用当前默认值补齐。

`alignment_gate_pass` 只代表图像配准门槛，不能代表答案识别已经达标。接回生产前
还需要在人工标注集上验证：题号错位为 0、静默抬分为 0、人工复核率不高于 10%、
逐题原样转录正确率至少 98%。先用 10 份试卷淘汰明显失败方案，再用 100 份以上、
覆盖不同拍摄角度和书写情况的试卷复验。

现有共识工件的 250 个题区中，只有 1 个触碰所有权边界，说明固定题号所有权区域
可以显著降低答案错位；但印刷残边令其中 249 个被本地程序判为有墨迹。因此这组
数据只能评估配准和区域边界，不能用于评价空白判断、复核率或答案保真。

## 6. 上线质量门禁

质量结果文件只使用匿名 `sample_id`，每个样本需提供真实分数、识别分数、逐题人工
转录核对数、复核数、题号错位数、静默抬分数和 OCR 合同失败数。运行：

```powershell
.\venv_local\Scripts\python.exe -m experiments.template_ocr_lab.cli quality-gate `
  --results ".\outputs\template-ocr-lab\quality-results.json" `
  --manifest ".\outputs\template-ocr-lab\artifact\manifest.json" `
  --regions ".\outputs\template-ocr-lab\regions.json" `
  --minimum-samples 10 `
  --output ".\outputs\template-ocr-lab\quality-report.json"
```

门禁读取并校验真实工件与区域文件，不相信结果 JSON 自报的来源。alignment 和每个
样本必须携带同一 `artifact_registration_sha256`；每个样本还必须携带同一
`region_contract_sha256`、唯一匿名 `sample_id` 和唯一 `run_id`，混用旧模板、
旧区域或交换学生结果会直接失败。

门禁同时要求：真实空白模板、样本数达标、配准通过率至少 95%、最低边缘召回至少
0.72、OCR 合同失败为 0、题号错位为 0、静默抬分为 0、最高分数抬升为 0、单张
最大降分不超过 4 分、逐题原样转录正确率至少 98%、人工复核率不高于 10%。可用
`--maximum-score-downward` 调整降分阈值。10 张只用于淘汰方案；接回生产时应将
`--minimum-samples` 提高到至少 100。

## 当前状态

目前已有配准、证据提取和安全合同的测试，以及三档预处理性能数据；尚未取得
`verified_blank` 工件下真实 Qwen 的逐题转录正确率、分差和复核率，不能声称识别
已经达标。下一步必须先提供与学生卷同页型的真实空白模板，用它重跑 10 张匿名样本，
再通过外部视觉回调生成质量结果；方案通过淘汰测试后，最终仍需至少 100 张人工逐题
金标样本通过上线门禁。

## 边界

- 本模块不读取 `.env`、数据库、上传目录或生产档案；
- 本模块不识别姓名、学号，不做学生归属；
- 本模块不解题；评分门禁只比较外部提供的匿名真实分数和识别分数；
- 本模块本身不向任何模型发送图片，OCR 网络调用必须由外部回调显式注入；
- 共识模板必须按页型分别编译，不能把不同版式混在同一个模板中；
- `student_consensus` 工件永远不能通过上线来源门禁；
- 三档性能均来自不安全共识工件，只能用于方案和性能比较，不能证明答案保真；
- 配准或证据图不可靠时应保留为实验失败，不能静默回退到生产批阅。
