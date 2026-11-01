# 04 · 一次批阅用掉多少 token

## 回答什么问题

模板处理和学生批阅，各自用掉多少输入、多少输出 token？

本目录**只给 token 计数，不给钱**。价格是另一个变量（随供应商、部署区域、缓存命中率变），由维护者补充；
补上 `prices.json` 后 `compute.py` 能把 token 换算成金额。

用阿里云 Token Plan 订阅的，直接看 [`token-plan.md`](token-plan.md)：那里把实测用量换算成了 Credits，
并给出一次批阅在各档位下占多少额度（`compute.py --credits` 输出同口径的数字）。

## 数据来源（都是供应商回报的值，没有估算）

| 阶段 | 模型 | 条数 | 来源 |
|---|---|---|---|
| `template_hierarchy` 层级识别 | qwen3.8-flash | 1 | 生产回执的 SSE `usage` 字段 |
| `template_coarse_locator` 粗定位 | qwen3.8-flash | 1 | 同上 |
| `template_transcribe` 模板转录 | qwen3.8-flash | 1 | 同上 |
| `template_max_solve` 模板解题 | qwen3.8-max | 1 | 同上（走 Token Plan，`usage` 同样回报） |
| `student_ocr` 学生卷转录 | qwen3.8-flash | 2 | 2026-11-01 按生产形态现场调用，取 `request_receipt["usage"]` |
| `grading` 整卷判分 | qwen3.8-flash | 24 | 生产回执的 SSE `usage` 字段 |

`usage.json` 里每条只保留 `prompt_tokens` / `completion_tokens` / `total_tokens`（`student_ocr` 另有 `image_tokens`），
不含任何正文、答案、题号或身份信息；`request` 字段是投递目录名里的内容哈希，不是学生标识。

## 一次正常批阅的调用形态

```
模板处理（每页）：层级识别 1 次 + 粗定位 1 次 + 模板转录 1 次 + 解题 1 次（Max）
学生卷（每份）：  差分转录 每栏 1 次 + 整卷判分 1 次
```

也就是说，一个两页模板、一份两栏学生卷的批阅 = 模板 8 次调用 + 学生 3 次调用。

**为什么模板只算一次**：同一份模板被整个班的学生复用，摊到每份卷上约等于 0；本目录按「一次批阅」单算，
所以模板占比看起来很高。真实成本要按「模板一次 + 学生 N 份」摊。

## 复算

```bash
# 1) 放一份 prices.json（元 / 百万 token）：
#    {"qwen3.8-flash": {"input": 0, "output": 0, "cache_hit": 0},
#     "qwen3.8-max":   {"input": 0, "output": 0, "cache_hit": 0}}
#    cache_hit 是缓存命中价（阿里云百炼为输入价的 10%），不用缓存就填成和 input 一样。
python evidence/04_usage-cost/compute.py prices.json --pages 2 --columns 2 --students 100
# 要 Credits 而不是金额：加 --credits（1 Credit = ¥0.01 按量表价）
python evidence/04_usage-cost/compute.py prices.json --pages 2 --columns 2 --students 50 --credits
```

脚本会打印各阶段的平均单次用量、以及给定规模下的总账；每一步都按「usage × 单价」展开，不做四舍五入的中间值。

`--columns` 决定每份学生卷转录几次：一份卷只有一张图时用 `--columns 1`，两栏差分就是 2。
所以「每份学生多少钱」会随这个参数变，报数时必须带上它——否则同一份 `usage.json` 能算出两个差 30% 的结果。

在维护者补价格之前，`compute.py` 不会输出任何金额：`prices.json` 里缺哪个模型的 `input`/`output` 就直接报错退出，
不填默认值、不猜。

## 边界

- 判分那 24 条来自同一批作业（同一张模板、同一套题），题型单一，不代表所有卷面的判分用量；
- 模板 4 条各只有 1 次，只能当量级参考；
- 本目录不含价格，也不含任何供应商账单截图。要核对金额，拿 `usage × 官方价目表` 自己算。