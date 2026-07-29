import json
import stat

import httpx
import pytest

from mcp_server_weibo import weibo
from mcp_server_weibo.weibo import WeiboCrawler


@pytest.mark.asyncio
async def test_cookie_precedence_persistence_and_no_visitor_overwrite(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": {"SUB": "from-file"}}))
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=from-env; XSRF-TOKEN=csrf")

    crawler = WeiboCrawler(cookie_file)
    monkeypatch.setattr(weibo.httpx, "AsyncClient", lambda **kwargs: pytest.fail("network was used"))
    assert await crawler._ensure_cookies() == {"SUB": "from-env", "XSRF-TOKEN": "csrf"}

    crawler.save_cookies(crawler.cookies)
    assert json.loads(cookie_file.read_text())["cookies"]["SUB"] == "from-env"
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_home_timeline_and_friendship_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=user; XSRF-TOKEN=csrf-token")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ajax/config/get_config":
            return httpx.Response(200, json={"data": {"login": True, "uid": "123"}})
        if request.url.path == "/ajax/feed/friendstimeline":
            return httpx.Response(200, json={"statuses": [{
                "id": 1,
                "text_raw": "首页内容",
                "user": {"id": 2, "screen_name": "用户"},
            }]})
        if request.url.path == "/":
            return httpx.Response(200)
        if request.url.path in ("/ajax/friendships/create", "/ajax/friendships/destory"):
            return httpx.Response(200, json={"ok": 1})
        raise AssertionError(f"unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        weibo.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    crawler = WeiboCrawler(tmp_path / "cookies.json")
    timeline = await crawler.get_home_timeline(limit=1)
    assert timeline[0].text == "首页内容"
    await crawler.follow_user(456)
    await crawler.unfollow_user(456)

    writes = [request for request in requests if request.method == "POST"]
    assert [request.url.path for request in writes] == [
        "/ajax/friendships/create",
        "/ajax/friendships/destory",
    ]
    assert all(request.headers["x-xsrf-token"] == "csrf-token" for request in writes)
    assert json.loads(writes[0].content)["friend_uid"] == "456"
    assert json.loads(writes[1].content)["uid"] == "456"
