import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mathagent_monitor.py"
SPEC = importlib.util.spec_from_file_location("mathagent_monitor", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


@pytest.fixture(autouse=True)
def _reset_report_state():
    monitor.reset_report_state()


def test_health_probe_uses_rich_api_health_endpoint(monkeypatch):
    commands = []

    def fake_run(command, timeout=8):
        commands.append((command, timeout))
        if command[0] == "curl":
            return 0, '{"status":"ok","database":"ok","version":"0.1.0"}', ""
        return 0, "", ""

    monkeypatch.setattr(monitor, "run", fake_run)

    monitor.check_process_port_health()

    curl_command = next(command for command, _ in commands if command[0] == "curl")
    assert monitor.HEALTH_URL == "http://127.0.0.1:8080/api/health"
    assert monitor.HEALTH_URL in curl_command
    assert "教师 API 健康：数据库正常，版本 0.1.0" in monitor.OKS
    assert monitor.DOWNS == []


def test_account_and_processing_labels_match_current_query_semantics(monkeypatch):
    responses = iter((
        (0, '{"total":12,"active":10,"teachers":9,"admins":1}', ""),
        (0, '{"records":34,"images":51}', ""),
    ))
    monkeypatch.setattr(monitor, "run", lambda *_args, **_kwargs: next(responses))

    monitor.check_users()
    monitor.check_processing_totals()

    assert monitor.USER_MESSAGE == "注册 12，启用 10（教师 9，管理员 1）"
    assert monitor.PROCESSING_MESSAGE == "当前保留批改结果 34 条，对应处理图片 51 张"


def test_report_prioritizes_chinese_status_sections():
    monitor.USER_MESSAGE = "注册 12，启用 10（教师 9，管理员 1）"
    monitor.PROCESSING_MESSAGE = "当前保留批改结果 34 条，对应处理图片 51 张"
    monitor.add_ok("教师 Web 服务：运行中")
    monitor.add_warn("根分区使用率 86%")
    monitor.add_down("教师 API 健康异常：服务 degraded，数据库 error")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    report = monitor.build_report(now)

    assert report.startswith("🧭 MathAgent 每日巡查\n时间：2026-08-31 12:00 +0800")
    assert "状态：🔴 异常" in report
    assert "概览：✅ 正常 1 ｜ 🟡 提醒 1 ｜ 🔴 异常 1" in report
    assert report.index("异常项") < report.index("提醒项") < report.index("正常项")
    assert "OK:" not in report
    assert "WARN:" not in report
    assert "DOWN:" not in report


def test_all_clear_report_ends_with_no_action_conclusion():
    monitor.add_ok("教师 Web 服务：运行中")

    report = monitor.build_report(
        datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    )

    assert "状态：🟢 正常" in report
    assert report.endswith("结论：当前未发现需要处理的问题。")
