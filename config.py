import math
import os

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_dotenv_disabled = os.environ.get("PYTHON_DOTENV_DISABLED", "").strip().lower()
if _dotenv_disabled not in {"1", "true", "yes", "on"}:
    load_dotenv(os.path.join(_BASE_DIR, ".env"))
    # 本机专用覆盖层：只放本地开发需要的密钥，绝不参与 Git 或服务器部署。
    # 生产环境未创建该文件时，行为与只加载 .env 完全一致。
    load_dotenv(os.path.join(_BASE_DIR, ".env.local"), override=True)

# Process-local resource profiles.  These values bound CPU/memory work inside
# one web worker; provider-wide API quotas remain configured independently
# below and are coordinated by Redis.  An explicit individual environment
# variable always wins over the selected profile.
# Historical Flash work window (80% of the measured >=320 clean point).
# Memory profiles size byte/CPU admission, not provider-I/O concurrency.
DASHSCOPE_FLASH_WORKING_CONCURRENCY = 256
_RESOURCE_PROFILES = {
    "low_memory": {
        "OCR_LOCAL_MAX_CONCURRENT": str(DASHSCOPE_FLASH_WORKING_CONCURRENCY),
        "OCR_BATCH_DISPATCH_CONCURRENT": str(DASHSCOPE_FLASH_WORKING_CONCURRENCY),
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES": str(256 * 1024 * 1024),
        "OCR_PREPROCESS_LOCAL_MAX_CONCURRENT": "1",
    },
    "standard": {
        # The byte budget, not a second network cap, protects image memory.
        "OCR_LOCAL_MAX_CONCURRENT": str(DASHSCOPE_FLASH_WORKING_CONCURRENCY),
        "OCR_BATCH_DISPATCH_CONCURRENT": str(DASHSCOPE_FLASH_WORKING_CONCURRENCY),
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES": str(512 * 1024 * 1024),
        "OCR_PREPROCESS_LOCAL_MAX_CONCURRENT": "2",
    },
}
_RESOURCE_PROFILE_ALIASES = {
    "low-memory": "low_memory",
    "lowmemory": "low_memory",
    "1.6gb": "low_memory",
    "1.6_gb": "low_memory",
    "4gb": "standard",
    "4_gb": "standard",
    "production_4gb": "standard",
    "production-4gb": "standard",
    "production4gb": "standard",
}
_requested_resource_profile = os.environ.get(
    "MATHAGENT_RESOURCE_PROFILE", "standard"
).strip().lower()
MATHAGENT_RESOURCE_PROFILE = _RESOURCE_PROFILE_ALIASES.get(
    _requested_resource_profile, _requested_resource_profile
)
if MATHAGENT_RESOURCE_PROFILE not in _RESOURCE_PROFILES:
    raise RuntimeError(
        "MATHAGENT_RESOURCE_PROFILE must be one of: "
        + ", ".join(sorted(_RESOURCE_PROFILES))
    )
for _resource_name, _resource_value in _RESOURCE_PROFILES[
    MATHAGENT_RESOURCE_PROFILE
].items():
    os.environ.setdefault(_resource_name, _resource_value)

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

# CORS 白名单（生产环境必须配置，逗号分隔）
# 示例: CORS_ORIGINS=https://math.example.com,https://www.example.com
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "")
# 开发模式允许所有来源（生产环境设为 False）
CORS_ALLOW_ALL = os.environ.get("CORS_ALLOW_ALL", "false").lower() == "true"

# ── 网络安全配置 ──
# 生产建议设置为业务域名，逗号分隔；留空则默认允许本机/代理常见 host，避免破坏本地开发。
# 示例: ALLOWED_HOSTS=math.example.com,www.math.example.com,127.0.0.1
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "")
# 仅在已全站 HTTPS 且反向代理正确设置协议头时开启。
FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "false").lower() == "true"
HSTS_MAX_AGE = int(os.environ.get("HSTS_MAX_AGE", "31536000"))
# 前端仍有本地验证码脚本/内联样式需求，CSP 默认走兼容策略；生产可用环境变量进一步收紧。
CONTENT_SECURITY_POLICY = os.environ.get(
    "CONTENT_SECURITY_POLICY",
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://*.alicaptcha.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://*.alicaptcha.com; "
    "frame-src https://*.alicaptcha.com; frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'",
)

# 教师聊天：Hermes 的回退线路与关闭 Hermes 时的旧意图模式（Hermes 另由 TEACHER_LLM_* 配置）。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_KEYS = os.environ.get("LLM_API_KEYS", "")  # 逗号分隔多Key，如 "key1,key2,key3"
LLM_KEY_CONCURRENCY = int(os.environ.get("LLM_KEY_CONCURRENCY", "8"))
LLM_API_KEY_WEIGHTS = os.environ.get("LLM_API_KEY_WEIGHTS", "")  # 权重，如 "2,2,3"
LLM_MODEL = os.environ.get("LLM_MODEL", "mimo-v2.6-pro")

def _migrate_template_output_tokens(value: str, *, default: str = "16000") -> int:
    """Move retired template output budgets to the GPT-6 probe budget."""
    raw = str(value or default).strip()
    if raw in {"4096", "9000"}:
        raw = default
    return int(raw)


# When teacher chat (Hermes) runs on the MiMo token plan it uses one per-worker
# lane (services/mimo_lane.py) sized to that plan's hard concurrency of 8.
MIMO_SUBSCRIPTION_GLOBAL_CONCURRENCY = 8

# 独立答题卡批阅必须经过真实学生答题卡配准/裁剪验收后才可开放。
# 默认关闭，避免只有空白模板候选结果时被误认为生产可用。
ANSWER_SHEET_GRADING_ENABLED = os.environ.get(
    "ANSWER_SHEET_GRADING_ENABLED", "false"
).lower() == "true"
# 批改与 OCR 分离；文字和作图使用独立 Flash 请求。判分只走 qwen3.8-flash 按量：
# mimo / gpt-sol 已退役，代码分支已删除，不再可经环境变量选择。
GRADING_PROVIDER = os.environ.get("GRADING_PROVIDER", "qwen3.8-flash").strip().lower()
if GRADING_PROVIDER != "qwen3.8-flash":
    raise RuntimeError(f"GRADING_PROVIDER={GRADING_PROVIDER} 已退役；判分只支持 qwen3.8-flash")

# 全高栏学生卷固定先做视觉转录，再交给独立判分模型；旧的联合判分开关已移除。
# 已验证的模板题号定位参数。活动 GPT 路由的 reasoning effort 在下方按
# canonical 模板变量设置；学生卷转录仍单独使用 Qwen 合同。
# 所有思考强度设置与模型请求共用的合法档位（Qwen 合同里 minimal 发送时记作 low）。
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
OCR_TRANSCRIPTION_REASONING_EFFORT = os.environ.get(
    "OCR_TRANSCRIPTION_REASONING_EFFORT", "none"
).strip().lower()
if OCR_TRANSCRIPTION_REASONING_EFFORT not in REASONING_EFFORTS:
    raise RuntimeError(
        "OCR_TRANSCRIPTION_REASONING_EFFORT 必须为 none、minimal、low、medium、high、xhigh 或 max"
    )
# 学生 OCR 保留已验收的 9000-token 应用输出预算；不随模型迁移放大请求。
_configured_ocr_transcription_max_output_tokens = int(
    os.environ.get("OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS", "9000")
)
if _configured_ocr_transcription_max_output_tokens < 1:
    raise RuntimeError("OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS 必须为正数")
# Keep the validated application budget at both configuration and request
# boundaries. This is not the retired Plus model's provider ceiling.
OCR_TRANSCRIPTION_MAX_OUTPUT_TOKENS = min(
    _configured_ocr_transcription_max_output_tokens,
    9000,
)

# 学生试卷 OCR（Qwen 3.8 Flash，关闭思考）：一次请求接收全部冻结栏图。
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
# Comma-separated legacy list. Not a capacity lever: the provider rate-limits per egress IP,
# so same-vendor keys share one bucket; raise the provider quota instead of adding keys.
# Student OCR/grading use it only when no settings-group key and no DASHSCOPE_API_KEY exist.
# The template-cut route (TEMPLATE_CUT_API_KEYS defaults to this) merges it with the single
# key and rotates through them: failover, never extra throughput.
DASHSCOPE_API_KEYS = os.environ.get("DASHSCOPE_API_KEYS", "")
DASHSCOPE_API_KEY_WEIGHTS = os.environ.get("DASHSCOPE_API_KEY_WEIGHTS", "")
# 固定已验收的学生转录模型；部署中残留的 DASHSCOPE_MODEL 不再参与选路。
DASHSCOPE_MODEL = "qwen3.8-flash"
# DashScope 按量（cn-beijing）各模型独立计量，各用独立的滚动 RPM/TPM 窗口
# （services/dashscope_budget.py）。北京地域对 qwen3.8 系列按月消费档位动态软限流，
# 且可能按秒折算（RPM/60、TPM/60）执行；账号实际值以百炼控制台为准。下表为名义
# 上限，只用于平滑突发。学生 OCR、模板 Flash 与判分现为同模型，
# 必须共用一个 RPM/TPM 窗口和共享并发总额度，不再读取旧 Plus 的容量。
DASHSCOPE_MODEL_RATE_LIMITS = {
    "qwen3.8-flash": (30_000, 5_000_000),
}
# 速率预算等待：窗口 60 s，不超过每分钟上限的单次预留最迟一个窗口后可放行，
# 2.5 个窗口给排队请求留余量；到时才失败，调用方自身期限（Flash 300 s）仍生效。
MODEL_RATE_BUDGET_WAIT_SECONDS = float(
    os.environ.get("MODEL_RATE_BUDGET_WAIT_SECONDS", "150")
)
if not 60 <= MODEL_RATE_BUDGET_WAIT_SECONDS <= 600:
    raise RuntimeError("MODEL_RATE_BUDGET_WAIT_SECONDS 必须在 60 到 600 秒之间")
if DASHSCOPE_MODEL not in DASHSCOPE_MODEL_RATE_LIMITS:
    raise RuntimeError(f"DASHSCOPE_MODEL={DASHSCOPE_MODEL} 没有声明按量速率预算")
# Local admission must allow a sole teacher to fill the shared Flash window.
# Redis arbitrates competing teachers; explicit smaller local limits still apply.
OCR_PER_TEACHER_LIMIT = int(os.environ.get(
    "OCR_PER_TEACHER_LIMIT", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY),
))

# ── 教师模板分栏（qwen3.8-flash 按量）──
# 2026-10-03 起单栏/双栏/三栏分栏由 qwen3.8-flash 承担，与批改 Flash 同一
# DashScope 按量账号同模型（qwen3.8flash 一律走按量），可共享同一份隐式缓存。
# 思考参数固定为 Qwen 官方关闭组合：enable_thinking=false +
# reasoning_effort="none" 两键同发；分栏不是推理任务，实测关闭思考档即通过
# 双栏/三栏分栏验收（见 Desktop/yangben 测试真值对照 qwen-column-split-20261003）。
# 分栏路线固定为 qwen3.8-flash；旧 Plus/其它模型环境值不参与选路。
TEMPLATE_CUT_MODEL = "qwen3.8-flash"
TEMPLATE_CUT_BASE_URL = os.environ.get("TEMPLATE_CUT_BASE_URL", DASHSCOPE_BASE_URL).strip()
TEMPLATE_CUT_API_KEY = os.environ.get("TEMPLATE_CUT_API_KEY", DASHSCOPE_API_KEY).strip()
TEMPLATE_CUT_API_KEYS = os.environ.get("TEMPLATE_CUT_API_KEYS", DASHSCOPE_API_KEYS).strip()
TEMPLATE_CUT_API_KEY_WEIGHTS = os.environ.get(
    "TEMPLATE_CUT_API_KEY_WEIGHTS", DASHSCOPE_API_KEY_WEIGHTS
).strip()
# 分栏的单凭证本地窗口跟随共享 Flash 工作窗；Redis 才是跨 worker/跨路由总闸。
# 运营方可显式调小，但默认不能把同模型 OCR/判分压在 8 路。
TEMPLATE_CUT_KEY_CONCURRENCY = int(
    os.environ.get("TEMPLATE_CUT_KEY_CONCURRENCY", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY))
)
TEMPLATE_CUT_TIMEOUT_S = float(os.environ.get("TEMPLATE_CUT_TIMEOUT_S", "150"))
TEMPLATE_CUT_MAX_TOKENS = int(os.environ.get("TEMPLATE_CUT_MAX_TOKENS", "8192"))
TEMPLATE_CUT_TEMPERATURE = float(os.environ.get("TEMPLATE_CUT_TEMPERATURE", "0"))
# 官方关闭组合是应用合同，不开放配置：两键缺一或档位非 none 都会让
# qwen3.8-flash 走进思考模式，既烧 reasoning token 又诱发补全。
TEMPLATE_CUT_ENABLE_THINKING = False
TEMPLATE_CUT_REASONING_EFFORT = "none"
if not TEMPLATE_CUT_MODEL:
    raise RuntimeError("TEMPLATE_CUT_MODEL 不能为空")
if TEMPLATE_CUT_MODEL not in DASHSCOPE_MODEL_RATE_LIMITS:
    raise RuntimeError(f"TEMPLATE_CUT_MODEL={TEMPLATE_CUT_MODEL} 没有声明按量速率预算")
if not 10 <= TEMPLATE_CUT_TIMEOUT_S <= 300:
    raise RuntimeError("TEMPLATE_CUT_TIMEOUT_S 必须在 10 到 300 秒之间")
if TEMPLATE_CUT_MAX_TOKENS < 1:
    raise RuntimeError("TEMPLATE_CUT_MAX_TOKENS 必须为正数")
if TEMPLATE_CUT_KEY_CONCURRENCY < 1:
    raise RuntimeError("TEMPLATE_CUT_KEY_CONCURRENCY 必须为正数")
if not math.isfinite(TEMPLATE_CUT_TEMPERATURE):
    raise RuntimeError("TEMPLATE_CUT_TEMPERATURE 参数无效")


# ── 模板路线 ──
# GPT canonical / YS / 豁免 / retry 号池均已退役：调度器只构建下方 qwen3.8-max
# Token Plan 路线，旧变量即使仍留在部署 .env 中也不再读取。以下两项只是
# call_gpt_vision 的默认参数（模板 Max 调用显式覆盖），名称沿用历史前缀。
OCR_TEMPLATE_GPT_REASONING_EFFORT = (
    os.environ.get("OCR_TEMPLATE_GPT_REASONING_EFFORT", "low").strip().lower()
    or "low"
)
OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS = _migrate_template_output_tokens(
    os.environ.get("OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS", "16000")
)

OCR_TEMPLATE_GPT_FALLBACK_NAME = "template-gpt-fallback"
OCR_TEMPLATE_GPT_FALLBACK_BASE_URL = os.environ.get(
    "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL", ""
).strip()
OCR_TEMPLATE_GPT_FALLBACK_API_KEY = os.environ.get(
    "OCR_TEMPLATE_GPT_FALLBACK_API_KEY", ""
).strip()
# 模板 Max 路线（定位与解题）发给服务商的模型请求名，由部署决定，例如 qwen4-max-preview；
# 不设时为 qwen3.8-max，设为空值则路线关闭。请求按 Qwen 的 Chat Completions 合同发
# enable_thinking / reasoning_effort / response_format，所以只接受 qwen 开头的模型名。
OCR_TEMPLATE_GPT_FALLBACK_MODEL = os.environ.get(
    "OCR_TEMPLATE_GPT_FALLBACK_MODEL", "qwen3.8-max"
).strip()
OCR_TEMPLATE_GPT_FALLBACK_ENABLED = all((
    OCR_TEMPLATE_GPT_FALLBACK_BASE_URL,
    OCR_TEMPLATE_GPT_FALLBACK_API_KEY,
    OCR_TEMPLATE_GPT_FALLBACK_MODEL,
))
# 备注（模板 Max 路线）：qwen3.8-max 走阿里云百炼 Token Plan 订阅（Standard 档），
# 按 Credits 计费，没有 RPM/TPM 限额；唯一限制是档位并发（Lite 1–2、Essential 2–3、
# Standard 3–4、Pro 6–8），且平台按整体负载动态下调，超出时返回
# "concurrency allocated quota exceeded"，稍后重试即可。因此本路线只设在途上限，
# 不设 RPM/TPM 门（0 = 不限速）。一页模板同时发定位与解题 2 个请求，4 路约两份
# 模板并行。变量名沿用历史 GPT_FALLBACK 前缀，以兼容现有部署 .env。
OCR_TEMPLATE_QWEN_FALLBACK_MAX_RPM = 0.0
OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT = int(
    os.environ.get("OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT", "4")
)
# 模板 Max 整页解题按题组拆成几路并行（1 = 不拆，与拆分前相同）。每路带同一组图片和
# 同一段目录提示（前缀可命中缓存），只输出自己负责的题；实际路数不超过题组数，
# 也不超过上面的在途上限减去定位占用的路数（整页定位 1 路，分块定位每块 1 路），最少 1 路。
OCR_TEMPLATE_SOLVER_SHARDS = int(os.environ.get("OCR_TEMPLATE_SOLVER_SHARDS", "2"))
# 模板 Max 定位按题分块：每块最多几道题（0 = 不分块，整页一次请求，与分块前相同）。
# 每块是一张带独立 0–1000 网格的裁图，模型报本图坐标，后端换算回整页后按题号拼接。
# 各块并行占用上面的 Max 在途名额，块数多于名额时排队。
OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS = int(os.environ.get("OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS", "0"))
# 分块开启时，每块所含题目的粗定位框在整页 0–1000 高度上最多跨多少；0 表示不限高度，只按题数切。
# 整体等待时间由最慢的一块决定，所以切块同时受题数和高度约束，并在块数最少的切法里取最均衡的一种。
OCR_TEMPLATE_LOCATOR_TILE_SPAN = int(os.environ.get("OCR_TEMPLATE_LOCATOR_TILE_SPAN", "350"))
# 分块开启时，一页最多切几块。各块并行、各占 1 路 Max 在途名额，剩下的名额留给解题；按上面两个限制算出来
# 需要更多块时，在这个块数内取最均衡的切法，单块会超出题数或高度限制。每栏至少一块，栏数多于此值时以栏数为准。
OCR_TEMPLATE_LOCATOR_TILES = int(os.environ.get("OCR_TEMPLATE_LOCATOR_TILES", "4"))
OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S = float(
    os.environ.get("OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S", "60")
)
OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S = float(
    os.environ.get("OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S", "480")
)

# OCR_MULTI_POOL_COORDINATOR selects the Max route's coordinator (redis across
# workers, or local). The enabled flag only remains for legacy direct callers;
# student Flash dispatch no longer uses whole-wave admission.
OCR_MULTI_POOL_ENABLED = os.environ.get("OCR_MULTI_POOL_ENABLED", "false").lower() == "true"
OCR_MULTI_POOL_COORDINATOR = os.environ.get(
    "OCR_MULTI_POOL_COORDINATOR", "redis"
).lower()
# Legacy callers may still select this compatibility protocol for hand-built
# PoolSpec instances. Active template routes do not use it: GPT/YS are bound
# to Responses and the qwen3.8max replacement is bound to Chat Completions
# below, so one legacy switch cannot mix the active provider contracts.
OCR_API_PROTOCOL = os.environ.get(
    "OCR_API_PROTOCOL", "chat_completions"
).strip().lower()
if OCR_API_PROTOCOL not in {"chat_completions", "responses"}:
    raise RuntimeError("OCR_API_PROTOCOL 只能为 chat_completions 或 responses")
# The qwen3.8-max route is pinned to Chat Completions; OCR_API_PROTOCOL only
# applies to isolated hand-built PoolSpec callers.
OCR_TEMPLATE_QWEN_FALLBACK_API_PROTOCOL = "chat_completions"
# Model credentials remain on the server. Desktop/platform nodes only receive
# CPU/GPU image-processing jobs through the accelerator task protocol.
_configured_ocr_api_execution_target = os.environ.get(
    "OCR_API_EXECUTION_TARGET", "server"
).strip().lower()
if _configured_ocr_api_execution_target not in {"", "server"}:
    raise RuntimeError(
        "OCR_API_EXECUTION_TARGET 已固定为 server；外部节点仅处理图片"
    )
OCR_API_EXECUTION_TARGET = "server"

# 知识点分类复用 Qwen Flash 文本调用与 DashScope 现有限额，不另建路由或号池。
# 分类独立指定思考档位，不改变 Flash 判分/模板调用的共享默认值。
# 三学段最小对照选择 low：关闭思考的高中批次曾漏题，严格接收器拒收。
KNOWLEDGE_CLASSIFIER_REASONING_EFFORT = "low"
KNOWLEDGE_CLASSIFIER_MAX_TOKENS = int(
    os.environ.get("KNOWLEDGE_CLASSIFIER_MAX_TOKENS", "2048")
)
KNOWLEDGE_CLASSIFIER_BATCH_SIZE = int(
    os.environ.get("KNOWLEDGE_CLASSIFIER_BATCH_SIZE", "8")
)
KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S = float(
    os.environ.get("KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S", "10")
)
KNOWLEDGE_CLASSIFIER_LEASE_S = float(
    os.environ.get("KNOWLEDGE_CLASSIFIER_LEASE_S", "300")
)
KNOWLEDGE_CLASSIFIER_WORKER_ENABLED = (
    os.environ.get("KNOWLEDGE_CLASSIFIER_WORKER_ENABLED", "true")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)

OCR_TEACHER_WAVE_SIZE = int(
    os.environ.get("OCR_TEACHER_WAVE_SIZE", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY))
)
OCR_ACTIVE_TEACHER_LIMIT = int(os.environ.get("OCR_ACTIVE_TEACHER_LIMIT", "4"))
_ocr_batch_dispatch_concurrent = int(
    os.environ.get("OCR_BATCH_DISPATCH_CONCURRENT", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY))
)
OCR_FIRST_WAVE_PRIORITY_GRACE_S = float(
    os.environ.get("OCR_FIRST_WAVE_PRIORITY_GRACE_S", "15")
)
OCR_FINAL_GUARD_S = float(os.environ.get("OCR_FINAL_GUARD_S", "10"))
OCR_CONNECT_TIMEOUT_S = float(os.environ.get("OCR_CONNECT_TIMEOUT_S", "10"))
OCR_WRITE_TIMEOUT_S = float(os.environ.get("OCR_WRITE_TIMEOUT_S", "90"))
OCR_SSE_IDLE_TIMEOUT_S = float(os.environ.get("OCR_SSE_IDLE_TIMEOUT_S", "180"))
OCR_MAX_CROSS_POOL_RETRIES_PER_PAPER = int(
    os.environ.get("OCR_MAX_CROSS_POOL_RETRIES_PER_PAPER", "1")
)
OCR_MAX_AUTOMATIC_RETRIES_PER_BATCH = int(
    os.environ.get("OCR_MAX_AUTOMATIC_RETRIES_PER_BATCH", "10")
)
OCR_POOL_CIRCUIT_FAILURE_THRESHOLD = int(
    os.environ.get("OCR_POOL_CIRCUIT_FAILURE_THRESHOLD", "3")
)
OCR_POOL_CIRCUIT_COOLDOWN_S = float(
    os.environ.get("OCR_POOL_CIRCUIT_COOLDOWN_S", "60")
)

if OCR_MULTI_POOL_COORDINATOR not in {"redis", "local"}:
    raise RuntimeError("OCR_MULTI_POOL_COORDINATOR 只能为 redis 或 local")
_ocr_positive_float_settings = {
    "OCR_CONNECT_TIMEOUT_S": OCR_CONNECT_TIMEOUT_S,
    "OCR_WRITE_TIMEOUT_S": OCR_WRITE_TIMEOUT_S,
    "OCR_SSE_IDLE_TIMEOUT_S": OCR_SSE_IDLE_TIMEOUT_S,
    "OCR_POOL_CIRCUIT_COOLDOWN_S": OCR_POOL_CIRCUIT_COOLDOWN_S,
    "OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S": OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S,
    "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S": OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S,
}
_invalid_ocr_float_settings = [
    name
    for name, value in _ocr_positive_float_settings.items()
    if not math.isfinite(value) or value <= 0
]
if _invalid_ocr_float_settings:
    raise RuntimeError(
        "OCR floating-point settings must be finite and positive: "
        + ", ".join(_invalid_ocr_float_settings)
    )
if OCR_TEMPLATE_GPT_REASONING_EFFORT not in REASONING_EFFORTS:
    raise RuntimeError(
        "OCR_TEMPLATE_GPT_REASONING_EFFORT 必须为 none、minimal、low、medium、high、xhigh 或 max"
    )
if OCR_TEMPLATE_GPT_FALLBACK_MODEL and not OCR_TEMPLATE_GPT_FALLBACK_MODEL.lower().startswith("qwen"):
    raise RuntimeError("OCR_TEMPLATE_GPT_FALLBACK_MODEL 必须是 qwen 开头的模型名（该路线按 Qwen 合同发送思考参数）")
if not 1 <= OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT <= 8:
    raise RuntimeError("OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT 必须在 1 到 8 之间（Token Plan 最高档 Pro 为 6–8）")
if not 1 <= OCR_TEMPLATE_SOLVER_SHARDS <= 4:
    raise RuntimeError("OCR_TEMPLATE_SOLVER_SHARDS 必须在 1 到 4 之间")
if OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS != 0 and not 2 <= OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS <= 20:
    raise RuntimeError("OCR_TEMPLATE_LOCATOR_TILE_QUESTIONS 必须为 0（不分块）或 2 到 20")
if OCR_TEMPLATE_LOCATOR_TILE_SPAN != 0 and not 150 <= OCR_TEMPLATE_LOCATOR_TILE_SPAN <= 1000:
    raise RuntimeError("OCR_TEMPLATE_LOCATOR_TILE_SPAN 必须为 0（不限高度）或 150 到 1000")
if not 2 <= OCR_TEMPLATE_LOCATOR_TILES <= 8:
    raise RuntimeError("OCR_TEMPLATE_LOCATOR_TILES 必须在 2 到 8 之间")
if OCR_TEACHER_WAVE_SIZE < 1 or OCR_ACTIVE_TEACHER_LIMIT < 1:
    raise RuntimeError("OCR 教师波次与活跃教师上限必须为正数")
if OCR_PER_TEACHER_LIMIT < 1:
    raise RuntimeError("OCR_PER_TEACHER_LIMIT must be positive")
if _ocr_batch_dispatch_concurrent < 1:
    raise RuntimeError("OCR_BATCH_DISPATCH_CONCURRENT must be positive")
if OCR_MAX_CROSS_POOL_RETRIES_PER_PAPER != 1:
    raise RuntimeError("OCR 每卷跨池重试必须严格为 1 次")
if OCR_MAX_AUTOMATIC_RETRIES_PER_BATCH < 1:
    raise RuntimeError("OCR 每批自动重试上限必须为正数")
if (
    not math.isfinite(OCR_FIRST_WAVE_PRIORITY_GRACE_S)
    or OCR_FIRST_WAVE_PRIORITY_GRACE_S < 0
):
    raise RuntimeError("OCR_FIRST_WAVE_PRIORITY_GRACE_S must be finite and nonnegative")
if not math.isfinite(OCR_FINAL_GUARD_S) or OCR_FINAL_GUARD_S < 0:
    raise RuntimeError("OCR_FINAL_GUARD_S must be finite and nonnegative")

if KNOWLEDGE_CLASSIFIER_REASONING_EFFORT not in {
    "none", "low", "medium", "high", "xhigh"
}:
    raise RuntimeError(
        "KNOWLEDGE_CLASSIFIER_REASONING_EFFORT 参数无效"
    )
if KNOWLEDGE_CLASSIFIER_MAX_TOKENS < 1:
    raise RuntimeError("KNOWLEDGE_CLASSIFIER_MAX_TOKENS 必须为正数")
for _classifier_name, _classifier_value in (
    ("KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S", KNOWLEDGE_CLASSIFIER_POLL_INTERVAL_S),
    ("KNOWLEDGE_CLASSIFIER_LEASE_S", KNOWLEDGE_CLASSIFIER_LEASE_S),
):
    if not math.isfinite(_classifier_value) or _classifier_value <= 0:
        raise RuntimeError(f"{_classifier_name} 必须为有限正数")
if not 1 <= KNOWLEDGE_CLASSIFIER_BATCH_SIZE <= 50:
    raise RuntimeError("KNOWLEDGE_CLASSIFIER_BATCH_SIZE 必须在 1 到 50 之间")


# MAX_UPLOAD_FILES also bounds one logical OCR batch. The retired multi-pool
# batch-budget derivation that used to live here was a pure function of the
# deleted OCR_PRIMARY/OCR_ACCELERATOR TTL and RPM scalars; answer sheets carry
# their own deadline constant instead.
_ocr_max_batch_papers = int(os.environ.get("MAX_UPLOAD_FILES", "200"))
if _ocr_max_batch_papers < 1:
    raise RuntimeError("MAX_UPLOAD_FILES must be positive")

# ── 跨 gunicorn worker 的模型并发调度 ──
# Redis 负责全站 Key 容量与教师公平令牌；图片预处理仍需由每 worker
# 的小信号量限制，不能把 API 的 500 并发等价为 500 张图片同时 Base64。
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
# Production defaults fail closed into Redis coordination. Windows local
# development must explicitly opt into `local` when no native Redis exists.
MODEL_LIMITER_BACKEND = os.environ.get("MODEL_LIMITER_BACKEND", "redis").lower()
if MODEL_LIMITER_BACKEND not in {"redis", "local"}:
    raise RuntimeError("MODEL_LIMITER_BACKEND 只能为 redis 或 local")

# A lease is deliberately bounded: a stuck request cannot keep renewing a
# shared permit forever. The finite 300s hard deadline covers one complete
# model call (Flash allows 300 s) without extending a stale permit through the
# wider HTTP request window.
MODEL_LIMITER_LEASE_SECONDS = int(os.environ.get("MODEL_LIMITER_LEASE_SECONDS", "180"))
MODEL_LIMITER_MAX_HOLD_SECONDS = int(
    os.environ.get("MODEL_LIMITER_MAX_HOLD_SECONDS", "300")
)
MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS = int(
    os.environ.get("MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS", "30")
)
MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS = float(
    os.environ.get("MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS", "5")
)
if not 30 <= MODEL_LIMITER_LEASE_SECONDS <= 300:
    raise RuntimeError("MODEL_LIMITER_LEASE_SECONDS 必须在 30 到 300 秒之间")
if not MODEL_LIMITER_LEASE_SECONDS <= MODEL_LIMITER_MAX_HOLD_SECONDS <= 900:
    raise RuntimeError("MODEL_LIMITER_MAX_HOLD_SECONDS 必须不小于 lease 且不大于 900 秒")
if not 1 <= MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS <= 120:
    raise RuntimeError("MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS 必须在 1 到 120 秒之间")
if not 0.1 <= MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS <= 30:
    raise RuntimeError("MODEL_LIMITER_REDIS_OPERATION_TIMEOUT_SECONDS 必须在 0.1 到 30 秒之间")

# 用户触发的聊天/出题/办公助手同样必须跨 worker 共用限流窗口。默认跟随
# MODEL_LIMITER_BACKEND，避免生产 Redis 已启用而用户额度仍被 worker 数放大。
LLM_RATE_LIMITER_BACKEND = os.environ.get(
    "LLM_RATE_LIMITER_BACKEND", MODEL_LIMITER_BACKEND
).lower()
if LLM_RATE_LIMITER_BACKEND not in {"redis", "local"}:
    raise RuntimeError("LLM_RATE_LIMITER_BACKEND 只能为 redis 或 local")
LLM_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("LLM_RATE_LIMIT_WINDOW_SECONDS", "60"))
LLM_RATE_LIMIT_MAX_CALLS = int(os.environ.get("LLM_RATE_LIMIT_MAX_CALLS", "20"))
if LLM_RATE_LIMIT_WINDOW_SECONDS < 1 or LLM_RATE_LIMIT_MAX_CALLS < 1:
    raise RuntimeError("LLM 用户限流窗口和次数必须为正数")

MODEL_TEACHER_BASE_CONCURRENCY = int(os.environ.get("MODEL_TEACHER_BASE_CONCURRENCY", "30"))
MODEL_TEACHER_BURST_CONCURRENCY = int(
    os.environ.get("MODEL_TEACHER_BURST_CONCURRENCY", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY))
)
MODEL_GLOBAL_RESERVE_CONCURRENCY = int(
    os.environ.get("MODEL_GLOBAL_RESERVE_CONCURRENCY", "0")
)
# Flash 历史真实载荷测到 >=320 并发（2026-09-11）；256 是其 80% 工作窗，
# 不是供应商硬上限，也不是旧 Plus 的 96 窗口。迁移只共用此窗，不追加容量。
# 残留的通用 500 配置只能调小，不能绕过此工作上限。
DASHSCOPE_GLOBAL_CONCURRENCY = min(
    int(os.environ.get("DASHSCOPE_GLOBAL_CONCURRENCY", str(DASHSCOPE_FLASH_WORKING_CONCURRENCY))),
    DASHSCOPE_FLASH_WORKING_CONCURRENCY,
)
if DASHSCOPE_GLOBAL_CONCURRENCY < 1:
    raise RuntimeError("DASHSCOPE_GLOBAL_CONCURRENCY must be positive")
# Existing deployments can retain the former teacher burst, but the active
# model window is always capped here rather than failing startup.
MODEL_TEACHER_BURST_CONCURRENCY = min(
    MODEL_TEACHER_BURST_CONCURRENCY, DASHSCOPE_GLOBAL_CONCURRENCY,
)
if not 1 <= MODEL_TEACHER_BASE_CONCURRENCY <= MODEL_TEACHER_BURST_CONCURRENCY:
    raise RuntimeError(
        "MODEL_TEACHER_BASE_CONCURRENCY must be between 1 and "
        "MODEL_TEACHER_BURST_CONCURRENCY"
    )
if MODEL_TEACHER_BURST_CONCURRENCY > DASHSCOPE_GLOBAL_CONCURRENCY:
    raise RuntimeError("MODEL_TEACHER_BURST_CONCURRENCY exceeds provider capacity")
if not 0 <= MODEL_GLOBAL_RESERVE_CONCURRENCY < DASHSCOPE_GLOBAL_CONCURRENCY:
    raise RuntimeError("MODEL_GLOBAL_RESERVE_CONCURRENCY is outside provider capacity")
# Local request admission can fill the shared window; image byte/CPU budgets
# remain independent. Explicit lower values are operator resource constraints.
OCR_LOCAL_MAX_CONCURRENT = min(
    int(os.environ.get("OCR_LOCAL_MAX_CONCURRENT", str(DASHSCOPE_GLOBAL_CONCURRENCY))),
    DASHSCOPE_GLOBAL_CONCURRENCY,
)
OCR_PER_TEACHER_LIMIT = min(OCR_PER_TEACHER_LIMIT, DASHSCOPE_GLOBAL_CONCURRENCY)
if min(OCR_LOCAL_MAX_CONCURRENT, OCR_PER_TEACHER_LIMIT) < 1:
    raise RuntimeError("OCR request and per-teacher windows must be positive")
# Base64 strings remain resident while httpx also materializes the JSON request
# body. Limit their combined per-worker footprint independently from request
# count so a batch of high-entropy scans cannot exhaust process memory.
OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES = int(
    os.environ.get(
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES",
        str(128 * 1024 * 1024),
    )
)
if not 32 * 1024 * 1024 <= OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES <= 2 * 1024 * 1024 * 1024:
    raise RuntimeError(
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES must be between 32 MiB and 2 GiB"
    )
# Provider-wide Flash quota is the actual cap; the local process gate may
# lower it only when an operator explicitly sets that resource value.
OCR_BATCH_DISPATCH_CONCURRENT = min(
    _ocr_batch_dispatch_concurrent, DASHSCOPE_GLOBAL_CONCURRENCY,
)
if not 1 <= OCR_BATCH_DISPATCH_CONCURRENT <= OCR_LOCAL_MAX_CONCURRENT:
    raise RuntimeError(
        "OCR_BATCH_DISPATCH_CONCURRENT 必须在 1 到 OCR_LOCAL_MAX_CONCURRENT 之间"
    )
# Full-resolution native image work, including the server CPU template matcher,
# is gated by OCR_PREPROCESS_LOCAL_MAX_CONCURRENT below rather than by a dispatch
# window: a dispatched paper mostly waits on the accelerator node or the provider.
# CPU 密集的旋转/透视/增强比外部 HTTP 更窄，避免 2 核机器同时预处理 30 张图。
OCR_PREPROCESS_LOCAL_MAX_CONCURRENT = int(
    os.environ.get("OCR_PREPROCESS_LOCAL_MAX_CONCURRENT", "1")
)
if OCR_PREPROCESS_LOCAL_MAX_CONCURRENT <= 0:
    raise RuntimeError("OCR_PREPROCESS_LOCAL_MAX_CONCURRENT must be positive")
OCR_PREPROCESS_MODE = os.environ.get("OCR_PREPROCESS_MODE", "fast").strip().lower()
if OCR_PREPROCESS_MODE not in {"off", "fast", "enhanced"}:
    raise RuntimeError("OCR_PREPROCESS_MODE must be off, fast, or enhanced")
OCR_PREPROCESS_MAX_EDGE = int(os.environ.get("OCR_PREPROCESS_MAX_EDGE", "1920"))
if not 1024 <= OCR_PREPROCESS_MAX_EDGE <= 4096:
    raise RuntimeError("OCR_PREPROCESS_MAX_EDGE must be between 1024 and 4096")
OCR_ANSWER_PREPROCESS_MAX_EDGE = int(
    os.environ.get(
        "OCR_ANSWER_PREPROCESS_MAX_EDGE",
        str(max(3072, OCR_PREPROCESS_MAX_EDGE)),
    )
)
if not OCR_PREPROCESS_MAX_EDGE <= OCR_ANSWER_PREPROCESS_MAX_EDGE <= 4096:
    raise RuntimeError(
        "OCR_ANSWER_PREPROCESS_MAX_EDGE must be between "
        "OCR_PREPROCESS_MAX_EDGE and 4096"
    )
# PaddleOCR fallback
OCR_LANG = os.environ.get("OCR_LANG", "ch")
OCR_USE_GPU = os.environ.get("OCR_USE_GPU", "True").lower() == "true"

_DEFAULT_DB_PATH = os.path.join(_BASE_DIR, "mathagent.db").replace("\\", "/")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")

UPLOAD_DIR = os.path.join(_BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(_BASE_DIR, "output")
DATA_DIR = os.path.join(_BASE_DIR, "data")

# ── 上传硬限制（P1-8）──
# 单文件最大字节数：与 security.validate_upload_file 的 20MB 上限一致。
MAX_UPLOAD_FILE_SIZE = int(os.environ.get("MAX_UPLOAD_FILE_SIZE", str(20 * 1024 * 1024)))
_OCR_MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_UPLOAD_FILE_SIZE + 2) // 3)
_OCR_TWO_IMAGE_EXPANDED_BYTES = 2 * (
    MAX_UPLOAD_FILE_SIZE + 3 * _OCR_MAX_ENCODED_IMAGE_BYTES
)
if OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES < _OCR_TWO_IMAGE_EXPANDED_BYTES:
    raise RuntimeError(
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES must hold the expanded peak of "
        "two maximum vision images"
    )
# 单次批量上传的最大文件数。
MAX_UPLOAD_FILES = _ocr_max_batch_papers
MAX_TEMPLATE_PAGES = int(os.environ.get("MAX_TEMPLATE_PAGES", "20"))
if MAX_TEMPLATE_PAGES < 1:
    raise RuntimeError("MAX_TEMPLATE_PAGES must be positive")
# 单次批量上传的总字节数上限（默认 2GB）。
MAX_UPLOAD_TOTAL_SIZE = int(os.environ.get("MAX_UPLOAD_TOTAL_SIZE", str(2 * 1024 * 1024 * 1024)))
# 数据导入文件（Excel/JSON 名单、成绩、答案）单文件上限（默认 10MB）。
# 这类文件是结构化表格，正常远小于图片；给个独立较小上限防内存耗尽。
MAX_IMPORT_FILE_SIZE = int(os.environ.get("MAX_IMPORT_FILE_SIZE", str(10 * 1024 * 1024)))
# 识图 and roster photos (ocr_service.read_image) share the OCR payload budget.
_READ_IMAGE_MAX_RAW_BYTES = max(
    MAX_UPLOAD_FILE_SIZE,
    MAX_IMPORT_FILE_SIZE,
    20 * 1024 * 1024,  # vision-office's upload cap
)
_READ_IMAGE_MAX_ENCODED_BYTES = 4 * ((_READ_IMAGE_MAX_RAW_BYTES + 2) // 3)
if OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES < (
    _READ_IMAGE_MAX_RAW_BYTES + 3 * _READ_IMAGE_MAX_ENCODED_BYTES
):
    raise RuntimeError(
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES must hold one maximum read_image upload"
    )
# 流式落盘的分块大小（1MB）。
UPLOAD_CHUNK_SIZE = 1024 * 1024

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "学生档案")
FAILED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "批阅失败")

for d in [ARCHIVE_DIR, FAILED_DIR]:
    os.makedirs(d, exist_ok=True)

# 阿里云短信配置
SMS_ACCESS_KEY_ID = os.environ.get("SMS_ACCESS_KEY_ID", "")
SMS_ACCESS_KEY_SECRET = os.environ.get("SMS_ACCESS_KEY_SECRET", "")
SMS_SIGN_NAME = os.environ.get("SMS_SIGN_NAME", "MathAgent")
SMS_TEMPLATE_CODE = os.environ.get("SMS_TEMPLATE_CODE", "")

# 微信开放平台配置
WECHAT_APP_ID = os.environ.get("WECHAT_APP_ID", "")
WECHAT_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")
WECHAT_REDIRECT_URI = os.environ.get("WECHAT_REDIRECT_URI", "")

# 图形验证码配置
# CAPTCHA_PROVIDER 决定短信发送前的验证方式：
#   builtin（默认）服务端出算术题，签名 + 一次性 + 绑 IP，不需要任何外部账号；
#   aliyun 走阿里云验证码（需要下面三项已实名配置）；
#   off     关闭图形验证（仅建议在内网/本地开发使用）。
CAPTCHA_PROVIDER = os.environ.get("CAPTCHA_PROVIDER", "builtin").strip().lower()
if CAPTCHA_PROVIDER not in {"builtin", "aliyun", "off"}:
    raise RuntimeError("CAPTCHA_PROVIDER 只能为 builtin、aliyun 或 off")
CAPTCHA_APP_ID = os.environ.get("CAPTCHA_APP_ID", "")
CAPTCHA_APP_KEY = os.environ.get("CAPTCHA_APP_KEY", "")
CAPTCHA_SCENE_CODE = os.environ.get("CAPTCHA_SCENE_CODE", "")

# 短信登录总开关。没有阿里云短信账号的私有部署设为 0：
# /auth/sms/send 与 /auth/sms/login 直接拒绝，前端隐藏短信入口，只保留密码登录。
SMS_ENABLED = os.environ.get("SMS_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# JWT 配置 — 未设置或仍是示例占位值时启动即报错
JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET 未设置！请在 .env 中设置 JWT_SECRET（至少32字符随机字符串）。\n"
        "生成命令: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
# 占位值检测：示例 .env 里的文案一旦被原样复制到生产，等于签名密钥公开，
# 任何人都能伪造管理员 token。这里宁可直接拒绝启动。
_JWT_PLACEHOLDER_HINTS = (
    "replace-with",
    "your-secret",
    "your_secret",
    "changeme",
    "change-me",
    "please-change",
    "placeholder",
    "example",
    "test-secret",
    "secret-key-here",
    "1234567890",
)
if len(JWT_SECRET) < 32:
    raise RuntimeError(
        f"JWT_SECRET 长度不足 32 字符（当前 {len(JWT_SECRET)}）。\n"
        "生成命令: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
_lowered_secret = JWT_SECRET.lower()
if any(hint in _lowered_secret for hint in _JWT_PLACEHOLDER_HINTS):
    raise RuntimeError(
        "JWT_SECRET 仍是示例/占位值，拒绝启动：这类值在公开仓库里人人可见，"
        "等于任何人都能伪造登录 token。\n"
        "生成命令: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
# token 默认有效期 48 小时（H1：原 720h/30天过长，被盗 token 暴露窗口太大）。
# 生产可按需在 .env 调整；如需更短配合前端续期，再引入 refresh token。
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "48"))

# 学生信息识别置信度阈值（低于此值标记需要老师确认）
STUDENT_INFO_CONFIDENCE_THRESHOLD = float(os.environ.get("STUDENT_INFO_CONFIDENCE_THRESHOLD", "0.7"))
