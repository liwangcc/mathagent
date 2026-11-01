from pathlib import Path


def test_deploy_reasserts_protected_env_ownership_before_restart():
    script = Path("deploy.sh").read_text(encoding="utf-8")

    chown = 'chown root:www-data "$REMOTE_DIR/.env"'
    chmod = 'chmod 0640 "$REMOTE_DIR/.env"'
    readable = 'sudo -u www-data test -r "$REMOTE_DIR/.env"'
    restart = "systemctl restart mathagent"

    assert chown in script
    assert chmod in script
    assert readable in script
    assert script.index(chown) < script.index(restart)
    assert script.index(chmod) < script.index(restart)
    assert script.index(readable) < script.index(restart)