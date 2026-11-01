import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_rejects_remote_dir_shell_injection(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the deployment shell script")
    marker = tmp_path / "must-not-exist"
    env = {
        **os.environ,
        "MATHAGENT_REMOTE_DIR": f"/opt/mathagent' ; touch {marker.as_posix()}; #'",
    }
    result = subprocess.run(
        [bash, "deploy.sh", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "simple absolute path" in result.stderr
    assert not marker.exists()


def test_setup_requires_valid_domain_and_renders_template_before_install():
    setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")

    assert "__DOMAIN__" in template
    assert "MATHAGENT_DOMAIN" in setup
    assert 'sed "s/__DOMAIN__/$DOMAIN/g"' in setup
    assert "grep -q '__DOMAIN__'" in setup
    assert "sudo cp deploy/nginx.conf" not in setup


def test_nginx_disables_buffering_for_streamed_grading_requests():
    template = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_buffering off;" in template
    assert "proxy_cache off;" in template
    assert "proxy_read_timeout 720s;" in template
    assert "proxy_send_timeout 720s;" in template
    assert "proxy_request_buffering off;" in template
    assert "client_max_body_size 2100M;" in template
    assert "client_body_timeout 600s;" in template


def test_nginx_capture_route_keeps_its_own_camera_policy():
    template = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    capture = template.split("location ^~ /capture/ {", 1)[1].split("\n    }", 1)[0]

    # A URI as the last try_files argument is an internal redirect: /index.html would be
    # re-matched by `location /` and served with camera=(), which blocks getUserMedia.
    assert "try_files $uri /index.html =404;" in capture
    assert 'add_header Permissions-Policy "camera=(self), microphone=(), geolocation=()" always;' in capture


def test_single_worker_does_not_recycle_during_long_grading_streams(monkeypatch):
    monkeypatch.setenv("GUNICORN_WORKERS", "1")
    config = runpy.run_path(str(ROOT / "deploy" / "gunicorn.conf.py"))

    assert config["workers"] == 1
    assert config["max_requests"] == 0
    assert config["max_requests_jitter"] == 0


def test_gunicorn_worker_watchdog_remains_bounded(monkeypatch):
    monkeypatch.setenv("GUNICORN_WORKERS", "1")
    config = runpy.run_path(str(ROOT / "deploy" / "gunicorn.conf.py"))

    assert config["timeout"] == 480


def test_baseline_deploy_keeps_native_image_preprocessing_single_file():
    template = (ROOT / "deploy" / "redis-shared-limits.env.example").read_text(
        encoding="utf-8"
    )

    assert "OCR_PREPROCESS_LOCAL_MAX_CONCURRENT=1" in template
    assert "OCR_WRITE_TIMEOUT_S=90" in template
    assert "OCR_BATCH_DEADLINE_S=" not in template


def test_release_gate_includes_answer_sheet_suite():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "-m pytest -q" in script
    assert "--ignore=tests/test_answer_sheet_session.py" not in script


def test_release_archive_requires_all_new_runtime_modules():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    for path in (
        "services/accelerator_business.py",
        "services/system_readiness.py",
        "services/retry_backoff.py",
        "services/template_canonicalization_contract.py",
        "services/answer_sheet_acceleration.py",
    ):
        assert path in script
    assert "runtime_release_files=(" in script


def test_remote_release_preflights_candidate_before_stopping_service():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    stop = script.index("systemctl stop mathagent", script.index("SERVICE_WAS_ACTIVE=1"))
    assert script.index("check_redis_ping \"$STAGE_DIR\"") < stop
    assert script.index("check_application_candidate \"$STAGE_DIR\"") < stop
    assert script.index("systemd-analyze verify \"$STAGE_DIR/deploy/mathagent.service\"") < stop
    assert "requirements.txt changed; prepare a reviewed production venv" in script
    assert "pip install" not in script
    assert "billable model HTTP request attempted during deployment readiness" in script
    assert "billable_http_attempted" in script
    assert 'for filename in .env .env.local' in script
    assert 'mount --bind "$source" "$target"' in script
    assert 'umount "$mount_path"' in script
    assert 'ln -s "$REMOTE_DIR/.env"' not in script


def test_remote_candidate_tree_is_traversable_by_service_user():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    recursive_ownership = 'chown -R root:www-data "$STAGE_DIR"'
    assert recursive_ownership in script
    assert script.index(recursive_ownership) < script.index(
        'check_application_candidate "$STAGE_DIR"'
    )


def test_remote_release_installs_and_backs_up_resource_profile_dropin():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    dropin = "deploy/mathagent.service.d/20-resource-profile.conf"
    assert dropin in script
    assert '"$BACKUP_DIR/20-resource-profile.conf.before"' in script
    install = (
        'install -D -m 0644 "$REMOTE_DIR/deploy/mathagent.service.d/'
        '20-resource-profile.conf"'
    )
    assert install in script
    assert script.index(install) < script.rindex("systemctl daemon-reload")
    assert "20-resource-profile.conf.missing" in script


def test_remote_release_has_single_exit_rollback_for_health_failures():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert "trap on_exit EXIT" in script
    assert "rollback \"$status\"" in script
    assert "source-before.tar.gz" in script
    assert "database-before.json" in script
    assert "database-before.db" in script
    assert "database rollback metadata is missing" in script.lower()
    assert "mathagent.service.before" in script
    assert "gunicorn.conf.py.before" in script
    assert "Loopback database health did not become ready" in script
    assert "trap - EXIT HUP INT TERM" not in script
    assert "SOURCE_BACKUP_TMP=\"$BACKUP_DIR/source-before.tar.gz.incomplete\"" in script
    assert "mv \"$SOURCE_BACKUP_TMP\" \"$BACKUP_DIR/source-before.tar.gz\"" in script


def test_remote_release_checks_runtime_directories_and_coordination_health():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    for directory in (
        "uploads",
        "output",
        "data",
        "logs",
        "backups",
        "学生档案",
        "批阅失败",
    ):
        assert f'"$root/{directory}"' in script
    assert "systemctl is-active --quiet mathagent" in script
    assert "check_redis_ping \"$REMOTE_DIR\"" in script
    assert "get_grading_infrastructure_readiness" in script
    assert "--check-config" in script
