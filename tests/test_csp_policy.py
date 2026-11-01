"""CSP 只允许阿里验证码域，不再放行任何人可托管内容的通配域。

第 4 轮复审 finding 6：`https://*.aliyun.com` 没有任何页面在用，
`*.aliyuncs.com`（OSS 用户桶）早已删除；两个都写死在这里防回退。
"""
from pathlib import Path

from config import CONTENT_SECURITY_POLICY

ROOT = Path(__file__).resolve().parent.parent


def test_policy_has_no_user_hostable_wildcards():
    for forbidden in ("aliyuncs.com", "*.aliyun.com"):
        assert forbidden not in CONTENT_SECURITY_POLICY, forbidden


def test_policy_still_allows_the_captcha_vendor():
    assert "https://*.alicaptcha.com" in CONTENT_SECURITY_POLICY


def test_nginx_policy_matches_backend():
    nginx = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    assert "aliyuncs.com" not in nginx
    assert "*.aliyun.com" not in nginx
    assert "https://*.alicaptcha.com" in nginx