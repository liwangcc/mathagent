import json

import httpx

from services import hermes_tools


class _SearchResponse:
    status_code = 200
    text = """
    <html><body><ol>
      <li class="b_algo">
        <h2><a href="https://www.moe.gov.cn/example">普通高中数学课程标准</a></h2>
        <div class="b_caption"><p>教育部公开信息摘要。</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="javascript:alert(1)">无效链接</a></h2>
      </li>
    </ol></body></html>
    """

    def raise_for_status(self):
        return None


def test_public_search_uses_fixed_endpoint_and_returns_metadata(monkeypatch):
    observed = {}

    def fake_get(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return _SearchResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    result = json.loads(hermes_tools._public_web_search({
        "query": "  教育部   高中数学课程标准  ",
        "limit": 9,
    }))

    assert result["success"] is True
    assert result["data"]["web"] == [{
        "title": "普通高中数学课程标准",
        "url": "https://www.moe.gov.cn/example",
        "description": "教育部公开信息摘要。",
        "position": 1,
    }]
    assert observed["url"] == "https://cn.bing.com/search"
    assert observed["params"] == {"q": "教育部 高中数学课程标准"}
    assert observed["follow_redirects"] is True
    assert observed["timeout"] == hermes_tools._PUBLIC_SEARCH_TIMEOUT_SECONDS


def test_public_search_blocks_private_or_internal_queries_before_network(monkeypatch):
    def forbidden_get(*_args, **_kwargs):
        raise AssertionError("private search text must not leave the server")

    monkeypatch.setattr(httpx, "get", forbidden_get)

    for query in (
        "查询学生姓名张三的学号 123456",
        "查一下手机号 13800138000",
        "访问 203.0.113.10 的服务器路径",
        "读取 C:\\project\\.env",
    ):
        result = json.loads(hermes_tools._public_web_search({"query": query}))
        assert result["success"] is False
        assert "已阻止发送到公网" in result["error"]
