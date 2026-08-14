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
async def test_file_cookies_reload_after_external_login(tmp_path, monkeypatch):
    monkeypatch.delenv("WEIBO_COOKIE", raising=False)
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": {"SUB": "old-session"}}))
    crawler = WeiboCrawler(cookie_file)

    # Simulate weibo-cli login writing to the volume from another process.
    WeiboCrawler(cookie_file).save_cookies(
        {"SUB": "new-session", "XSRF-TOKEN": "fresh-csrf"}
    )

    monkeypatch.setattr(weibo.httpx, "AsyncClient", lambda **kwargs: pytest.fail("network was used"))
    assert await crawler._ensure_cookies() == {
        "SUB": "new-session",
        "XSRF-TOKEN": "fresh-csrf",
    }


@pytest.mark.asyncio
async def test_session_check_uses_reloaded_file_cookies(tmp_path, monkeypatch):
    monkeypatch.delenv("WEIBO_COOKIE", raising=False)
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": {"SUB": "old-session"}}))
    crawler = WeiboCrawler(cookie_file)
    WeiboCrawler(cookie_file).save_cookies({"SUB": "new-session"})
    cookie_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie_headers.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"data": {"login": True, "uid": "123"}})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        weibo.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    assert await crawler.get_session() == {"login": True, "uid": "123"}
    assert cookie_headers == ["SUB=new-session"]


@pytest.mark.asyncio
async def test_home_timeline_and_friendship_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=user; XSRF-TOKEN=csrf-token")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"data": {"login": True, "uid": "123", "st": "fresh-csrf"}})
        if request.url.path == "/ajax/feed/friendstimeline":
            timeline_call = sum(r.url.path == request.url.path for r in requests)
            text = "第一页" if timeline_call == 1 else "首页内容"
            return httpx.Response(200, json={
                "ok": 1,
                "max_id_str": "next-page",
                "statuses": [{"id": timeline_call, "text_raw": text,
                              "user": {"id": 2, "screen_name": "用户"}}],
            })
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
    timeline = await crawler.get_home_timeline(limit=1, page=2)
    assert timeline[0].text == "首页内容"
    timeline_requests = [request for request in requests if request.url.path == "/ajax/feed/friendstimeline"]
    assert len(timeline_requests) == 2
    assert timeline_requests[0].url.params["list_id"] == "0"
    assert timeline_requests[0].url.params["since_id"] == "0"
    assert timeline_requests[1].url.params["max_id"] == "next-page"
    await crawler.follow_user(456)
    await crawler.unfollow_user(456)

    writes = [request for request in requests if request.method == "POST"]
    assert [request.url.path for request in writes] == [
        "/ajax/friendships/create",
        "/ajax/friendships/destory",
    ]
    assert all(request.headers["x-xsrf-token"] == "fresh-csrf" for request in writes)
    assert json.loads(writes[0].content)["friend_uid"] == "456"
    assert json.loads(writes[1].content)["uid"] == "456"


@pytest.mark.asyncio
async def test_qr_login_exchanges_alt_and_saves_valid_session(tmp_path, monkeypatch):
    monkeypatch.delenv("WEIBO_COOKIE", raising=False)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/sso/signin":
            return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=csrf; Path=/"})
        if request.url.path == "/sso/v2/qrcode/image":
            return httpx.Response(200, json={
                "retcode": 20000000,
                "data": {"qrid": "qr", "image": "https://example/qrcode?data=https%3A%2F%2Fscan"},
            })
        if request.url.path == "/sso/v2/qrcode/check":
            return httpx.Response(200, json={"retcode": 20000000, "data": {"alt": "ticket"}})
        if request.url.path == "/sso/v2/login":
            return httpx.Response(200, headers={"set-cookie": "SUB=user-session; Domain=.weibo.com; Path=/"})
        if request.url.path == "/api/config":
            return httpx.Response(200, json={
                "ok": 1,
                "data": {"login": True, "uid": "123", "st": "fresh-csrf"},
            })
        raise AssertionError(f"unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        weibo.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(weibo.qrcode.QRCode, "print_ascii", lambda self, **kwargs: None)

    cookie_file = tmp_path / "cookies.json"
    session = await WeiboCrawler(cookie_file).qr_login(timeout=1)

    assert session == {"login": True, "uid": "123"}
    assert any(request.url.path == "/sso/v2/login" for request in requests)
    saved = json.loads(cookie_file.read_text())["cookies"]
    assert saved["SUB"] == "user-session"
    assert saved["XSRF-TOKEN"] == "fresh-csrf"


@pytest.mark.asyncio
async def test_feed_cards_without_mblog_are_ignored(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {
            "cardlistInfo": {"since_id": "next"},
            "cards": [
                {"card_type": 11},
                {"card_type": 9, "mblog": {
                    "idstr": "42",
                    "text": "有效微博",
                    "user": {"id": 2, "screen_name": "用户"},
                }},
            ],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await WeiboCrawler()._extract_feeds(client, 2, "container", "")

    assert page.SinceId == "next"
    assert [feed.id for feed in page.Feeds] == [42]


@pytest.mark.asyncio
async def test_expired_cookie_returns_clear_error(monkeypatch):
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=expired")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://passport.weibo.com/sso/signin"},
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        weibo.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(RuntimeError, match="Cookie 已失效"):
        await WeiboCrawler().get_trendings()
