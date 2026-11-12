import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_shared_limiter_defaults_to_redis_when_no_environment_override(tmp_path):
    """A production process without an explicit override must not silently fail open."""
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("MODEL_LIMITER_BACKEND", None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [sys.executable, "-c", "from config import MODEL_LIMITER_BACKEND; print(MODEL_LIMITER_BACKEND)"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "redis"


def test_ocr_batch_dispatch_defaults_to_first_stage_teacher_window(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("OCR_BATCH_DISPATCH_CONCURRENT", None)
    env.pop("OCR_LOCAL_MAX_CONCURRENT", None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config import OCR_BATCH_DISPATCH_CONCURRENT; print(OCR_BATCH_DISPATCH_CONCURRENT)",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "256"


def test_resource_profile_defaults_to_balanced_4gb(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("MATHAGENT_RESOURCE_PROFILE", None)
    for name in (
        "OCR_LOCAL_MAX_CONCURRENT",
        "OCR_BATCH_DISPATCH_CONCURRENT",
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES",
    ):
        env.pop(name, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import MATHAGENT_RESOURCE_PROFILE, "
                "OCR_LOCAL_MAX_CONCURRENT, OCR_BATCH_DISPATCH_CONCURRENT, "
                "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES; "
                "print(MATHAGENT_RESOURCE_PROFILE, OCR_LOCAL_MAX_CONCURRENT, "
                "OCR_BATCH_DISPATCH_CONCURRENT, "
                "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "standard 256 256 536870912"


def test_low_memory_profile_can_be_selected_and_individual_settings_win(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in (
        "OCR_LOCAL_MAX_CONCURRENT",
        "OCR_BATCH_DISPATCH_CONCURRENT",
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES",
    ):
        env.pop(name, None)
    env["MATHAGENT_RESOURCE_PROFILE"] = "1.6gb"
    env["OCR_LOCAL_MAX_CONCURRENT"] = "3"
    env["OCR_BATCH_DISPATCH_CONCURRENT"] = "3"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import MATHAGENT_RESOURCE_PROFILE, "
                "OCR_LOCAL_MAX_CONCURRENT, OCR_BATCH_DISPATCH_CONCURRENT, "
                "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES; "
                "print(MATHAGENT_RESOURCE_PROFILE, OCR_LOCAL_MAX_CONCURRENT, "
                "OCR_BATCH_DISPATCH_CONCURRENT, "
                "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "low_memory 3 3 268435456"


def test_ocr_payload_budget_accounts_for_two_expanded_maximum_images(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "JWT_SECRET": "test-only-jwt-secret-with-at-least-32-characters",
        "PYTHONPATH": str(project_root),
        "OCR_MULTI_POOL_ENABLED": "false",
        "OCR_REQUIRE_GPT_POOLS": "false",
        "MAX_UPLOAD_FILE_SIZE": str(20 * 1024 * 1024),
        "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES": str(128 * 1024 * 1024),
    })

    completed = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "expanded peak of two maximum vision images" in completed.stderr


def test_resource_profile_rejects_unknown_value(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["MATHAGENT_RESOURCE_PROFILE"] = "unbounded"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "MATHAGENT_RESOURCE_PROFILE must be one of" in completed.stderr


def test_answer_transcription_defaults_to_higher_resolution(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("OCR_PREPROCESS_MAX_EDGE", None)
    env.pop("OCR_ANSWER_PREPROCESS_MAX_EDGE", None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import OCR_PREPROCESS_MAX_EDGE, "
                "OCR_ANSWER_PREPROCESS_MAX_EDGE; "
                "print(OCR_PREPROCESS_MAX_EDGE, OCR_ANSWER_PREPROCESS_MAX_EDGE)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "1920 3072"


def test_full_resolution_image_work_defaults_to_one_lane_per_worker(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("OCR_PREPROCESS_LOCAL_MAX_CONCURRENT", None)
    # A stale production drop-in may still export the retired dispatch knob.
    env["OCR_TEMPLATE_LOCAL_DISPATCH_CONCURRENT"] = "1"
    env["MATHAGENT_RESOURCE_PROFILE"] = "low_memory"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import config; "
                "print(config.OCR_PREPROCESS_LOCAL_MAX_CONCURRENT, "
                "hasattr(config, 'OCR_TEMPLATE_LOCAL_DISPATCH_CONCURRENT'))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    # The preprocess gate owns full-resolution work; no dispatch window may
    # stand in for it (the retired template knob serialized whole papers).
    assert completed.stdout.strip() == "1 False"


def test_current_ocr_and_template_defaults_are_independent_of_retired_pools(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in (
        "DASHSCOPE_MODEL",
        "OCR_MULTI_POOL_ENABLED",
        "OCR_TEMPLATE_GPT_MODEL",
        "OCR_TEMPLATE_GPT_REASONING_EFFORT",
        "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL",
        "OCR_TEMPLATE_GPT_FALLBACK_API_KEY",
        "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
    ):
        env.pop(name, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["OCR_REQUIRE_GPT_POOLS"] = "false"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import DASHSCOPE_MODEL, "
                "DASHSCOPE_MODEL_RATE_LIMITS, "
                "OCR_TEMPLATE_GPT_REASONING_EFFORT, "
                "OCR_TEMPLATE_GPT_FALLBACK_ENABLED; "
                "print(DASHSCOPE_MODEL, *DASHSCOPE_MODEL_RATE_LIMITS[DASHSCOPE_MODEL], "
                "OCR_TEMPLATE_GPT_REASONING_EFFORT, "
                "OCR_TEMPLATE_GPT_FALLBACK_ENABLED)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "qwen3.8-flash 30000 5000000 low False"


def test_retired_gpt_pool_requirement_no_longer_blocks_startup(tmp_path):
    """The GPT canonical pool is retired; a leftover switch is simply ignored."""
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_REQUIRE_GPT_POOLS"] = "true"
    for name in ("OCR_TEMPLATE_GPT_BASE_URL", "OCR_TEMPLATE_GPT_API_KEY"):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_retired_multi_pool_switch_does_not_require_legacy_pool_credentials(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_MULTI_POOL_ENABLED"] = "true"
    env["OCR_REQUIRE_GPT_POOLS"] = "false"
    for name in (
        "OCR_PRIMARY_BASE_URL",
        "OCR_PRIMARY_API_KEY",
        "OCR_ACCELERATOR_BASE_URL",
        "OCR_ACCELERATOR_API_KEY",
        "OCR_RETRY_BASE_URL",
        "OCR_RETRY_API_KEY",
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_retired_template_answer_and_deepseek_settings_do_not_block_startup(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "JWT_SECRET": "test-only-jwt-secret-with-at-least-32-characters",
        "PYTHONPATH": str(project_root),
        # A production .env may still carry these retired keys; they are ignored.
        "TEMPLATE_ANSWER_PROVIDER": "gpt-sol",
        "DEEPSEEK_REASONING_EFFORT": "invalid",
        "DEEPSEEK_GLOBAL_CONCURRENCY": "0",
        "MIMO_PAYG_REASONING_EFFORT": "invalid",
    })

    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_template_qwen_route_model_comes_from_the_environment(tmp_path):
    """Unset keeps qwen3.8-max, a set name is used as sent, empty turns the route off."""
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in (
        "OCR_MULTI_POOL_ENABLED",
        "OCR_REQUIRE_GPT_POOLS",
        "OCR_TEMPLATE_GPT_BASE_URL",
        "OCR_TEMPLATE_GPT_API_KEY",
        "OCR_TEMPLATE_GPT_FALLBACK_BASE_URL",
        "OCR_TEMPLATE_GPT_FALLBACK_API_KEY",
        "OCR_TEMPLATE_GPT_FALLBACK_MODEL",
        "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT",
    ):
        env.pop(name, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_REQUIRE_GPT_POOLS"] = "false"
    env["OCR_TEMPLATE_GPT_BASE_URL"] = "https://gpt.example.invalid/v1"
    env["OCR_TEMPLATE_GPT_API_KEY"] = "canonical-test-key"
    env["OCR_TEMPLATE_GPT_FALLBACK_BASE_URL"] = "https://qwen.example.invalid/v1"
    env["OCR_TEMPLATE_GPT_FALLBACK_API_KEY"] = "qwen-test-key"

    def route(model=None):
        run_env = dict(env)
        if model is not None:
            run_env["OCR_TEMPLATE_GPT_FALLBACK_MODEL"] = model
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                (
                    "from config import OCR_TEMPLATE_GPT_FALLBACK_ENABLED, "
                    "OCR_TEMPLATE_GPT_FALLBACK_MODEL, OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT; "
                    "print(repr((OCR_TEMPLATE_GPT_FALLBACK_ENABLED, "
                    "OCR_TEMPLATE_GPT_FALLBACK_MODEL, "
                    "OCR_TEMPLATE_GPT_FALLBACK_HARD_IN_FLIGHT)))"
                ),
            ],
            cwd=tmp_path,
            env=run_env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    for model, expected in (
        (None, "(True, 'qwen3.8-max', 4)"),
        ("qwen4-max-preview", "(True, 'qwen4-max-preview', 4)"),
        ("  qwen4-max-preview  ", "(True, 'qwen4-max-preview', 4)"),
        ("", "(False, '', 4)"),
    ):
        completed = route(model)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == expected, model

    # A leftover non-Qwen name (e.g. from the retired GPT route) stops startup
    # instead of being sent with parameters that model does not take.
    rejected = route("gpt-5.4")
    assert rejected.returncode != 0
    assert "OCR_TEMPLATE_GPT_FALLBACK_MODEL" in rejected.stderr


def test_legacy_wave_settings_remain_loadable_without_driving_flash_dispatch(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in (
        "OCR_TEACHER_WAVE_SIZE",
        "OCR_ACTIVE_TEACHER_LIMIT",
        "OCR_FIRST_WAVE_PRIORITY_GRACE_S",
    ):
        env.pop(name, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import OCR_TEACHER_WAVE_SIZE, "
                "OCR_ACTIVE_TEACHER_LIMIT, OCR_FIRST_WAVE_PRIORITY_GRACE_S; "
                "print(OCR_TEACHER_WAVE_SIZE, OCR_ACTIVE_TEACHER_LIMIT, "
                "OCR_FIRST_WAVE_PRIORITY_GRACE_S)"
            ),
        ],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=True,
    )
    assert completed.stdout.strip() == "256 4 15.0"


def test_student_ocr_per_teacher_limit_matches_first_stage_dispatch(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in ("OCR_PER_TEACHER_LIMIT", "OCR_BATCH_DISPATCH_CONCURRENT"):
        env.pop(name, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import OCR_PER_TEACHER_LIMIT, "
                "OCR_BATCH_DISPATCH_CONCURRENT; "
                "print(OCR_PER_TEACHER_LIMIT, OCR_BATCH_DISPATCH_CONCURRENT)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.strip() == "256 256"


def test_max_route_timeout_defaults_cover_high_reasoning(tmp_path):
    """The Max TTL must outlast observed stages (fullsolve max ~397 s, 09-10..10-03)."""
    project_root = Path(__file__).resolve().parents[1]
    env = _ocr_runtime_environment(project_root)
    for setting in (
        "OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S",
        "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S",
        "OCR_WRITE_TIMEOUT_S",
        "OCR_SSE_IDLE_TIMEOUT_S",
    ):
        env.pop(setting, None)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import (OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S, "
                "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S, "
                "OCR_WRITE_TIMEOUT_S, OCR_SSE_IDLE_TIMEOUT_S); "
                "print(OCR_TEMPLATE_GPT_FALLBACK_FIRST_EVENT_TIMEOUT_S, "
                "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S, "
                "OCR_WRITE_TIMEOUT_S, OCR_SSE_IDLE_TIMEOUT_S)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "60.0 480.0 90.0 180.0"


def _ocr_runtime_environment(project_root):
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "JWT_SECRET": "test-only-jwt-secret-with-at-least-32-characters",
            "PYTHONPATH": str(project_root),
            "OCR_MULTI_POOL_ENABLED": "false",
            "OCR_REQUIRE_GPT_POOLS": "false",
            "MAX_UPLOAD_FILES": "400",
            "OCR_ACTIVE_TEACHER_LIMIT": "3",
            "OCR_TEACHER_WAVE_SIZE": "50",
            "OCR_LOCAL_MAX_CONCURRENT": "60",
            "OCR_BATCH_DISPATCH_CONCURRENT": "40",
            "OCR_RETRY_RPM": "10",
            "OCR_RETRY_BURST": "2",
            "OCR_RETRY_ATTEMPT_TTL_S": "95",
            "OCR_MAX_AUTOMATIC_RETRIES_PER_BATCH": "10",
            "OCR_FINAL_GUARD_S": "10",
        }
    )
    return env


def test_max_upload_files_defaults_to_200(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "JWT_SECRET": "test-only-jwt-secret-with-at-least-32-characters",
        "PYTHONPATH": str(project_root),
        "OCR_MULTI_POOL_ENABLED": "false",
        "OCR_REQUIRE_GPT_POOLS": "false",
    })
    env.pop("MAX_UPLOAD_FILES", None)

    completed = subprocess.run(
        [sys.executable, "-c", "from config import MAX_UPLOAD_FILES; print(MAX_UPLOAD_FILES)"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "200"


@pytest.mark.parametrize(
    "setting",
    [
        "OCR_TEMPLATE_GPT_FALLBACK_ATTEMPT_TTL_S",
        "OCR_CONNECT_TIMEOUT_S",
        "OCR_WRITE_TIMEOUT_S",
        "OCR_SSE_IDLE_TIMEOUT_S",
        "OCR_POOL_CIRCUIT_COOLDOWN_S",
    ],
)
def test_ocr_float_settings_reject_nonfinite_values(tmp_path, setting):
    project_root = Path(__file__).resolve().parents[1]
    env = _ocr_runtime_environment(project_root)
    env[setting] = "nan"

    completed = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "OCR floating-point settings must be finite and positive" in completed.stderr


def test_zero_ocr_preprocess_concurrency_is_rejected_at_startup(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_PREPROCESS_LOCAL_MAX_CONCURRENT"] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "OCR_PREPROCESS_LOCAL_MAX_CONCURRENT must be positive" in completed.stderr


def test_template_page_limit_defaults_to_twenty(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_MULTI_POOL_ENABLED"] = "false"
    env["OCR_REQUIRE_GPT_POOLS"] = "false"
    env.pop("MAX_TEMPLATE_PAGES", None)

    completed = subprocess.run(
        [sys.executable, "-c", "from config import MAX_TEMPLATE_PAGES; print(MAX_TEMPLATE_PAGES)"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "20"


def test_nonpositive_template_page_limit_is_rejected(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    env["PYTHONPATH"] = str(project_root)
    env["OCR_MULTI_POOL_ENABLED"] = "false"
    env["OCR_REQUIRE_GPT_POOLS"] = "false"
    env["MAX_TEMPLATE_PAGES"] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "MAX_TEMPLATE_PAGES must be positive" in completed.stderr
