"""Rate-limit storage must follow the configured data root, including clean clones."""
from pathlib import Path
import runpy
import sys
from types import ModuleType


def test_rate_limit_uses_configured_directory_and_creates_missing_parent(tmp_path, monkeypatch):
    configured_root = tmp_path / "isolated" / "rate-limits"
    config_stub = ModuleType("config")
    config_stub.DATA_DIR = str(configured_root)
    monkeypatch.setitem(sys.modules, "config", config_stub)
    source = Path(__file__).resolve().parents[1] / "services" / "ip_rate_limit.py"
    module = runpy.run_path(str(source))

    assert Path(module["DB_PATH"]) == configured_root / "ip_failures.db"
    assert Path(module["DB_PATH"]).is_file()
    assert module["get_ip_lock"]("testclient") == 0
    for _ in range(module["IP_LOCK_THRESHOLD"]):
        module["record_ip_failure"]("testclient")
    assert module["get_ip_lock"]("testclient") > 0
    module["clear_ip_failures"]("testclient")
    assert module["get_ip_lock"]("testclient") == 0
