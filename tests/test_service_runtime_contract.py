from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_web_service_survives_redis_outage_and_limits_restart_pressure():
    unit = (ROOT / "deploy" / "mathagent.service").read_text(encoding="utf-8")

    assert "Requires=redis-server.service" not in unit
    assert "Wants=network-online.target redis-server.service" in unit
    assert "After=network-online.target redis-server.service" in unit
    assert "Restart=on-failure" in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitBurst=3" in unit
    assert "OOMPolicy=stop" in unit


def test_web_service_has_a_bounded_memory_envelope():
    unit = (ROOT / "deploy" / "mathagent.service").read_text(encoding="utf-8")

    assert "MemoryHigh=2500M" in unit
    assert "MemoryMax=3200M" in unit
    assert "MemorySwapMax=512M" in unit


def test_web_service_leaves_the_config_profile_overridable():
    unit = (ROOT / "deploy" / "mathagent.service").read_text(encoding="utf-8")

    assert 'Environment="GUNICORN_WORKERS=1"' in unit
    assert 'Environment="MATHAGENT_RESOURCE_PROFILE=' not in unit
    assert 'Environment="OCR_LOCAL_MAX_CONCURRENT=' not in unit
    assert 'Environment="MIMO_GRADING_BATCH_LOCAL_CONCURRENT=' not in unit


def test_setup_precreates_all_runtime_writable_directories():
    setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")

    for path in (
        "/opt/mathagent/uploads",
        "/opt/mathagent/output",
        "/opt/mathagent/data",
        "/opt/mathagent/logs",
        "/opt/mathagent/backups",
        "/opt/mathagent/.cache/matplotlib",
        "/opt/mathagent/学生档案",
        "/opt/mathagent/批阅失败",
    ):
        assert path in setup
    assert 'install -d -o www-data -g www-data -m 0775 "${runtime_dirs[@]}"' in setup
