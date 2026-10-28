"""QR login through MCP, including the image/structured-content boundary."""
import base64
import json

import httpx
import pytest
from fastmcp import Client

from mcp_server_weibo import server, weibo
from mcp_server_weibo.weibo import WeiboCrawler


@pytest.fixture
def qr_flow(tmp_path, monkeypatch):
    monkeypatch.delenv("WEIBO_COOKIE", raising=False)
    state = {"retcode": 50114001, "valid": True, "qrid": 0, "checks": 0}

    def handler(request):
        if request.url.path == "/sso/signin":
            return httpx.Response(200, headers={"set-cookie": "X-CSRF-TOKEN=csrf; Path=/"})
        if request.url.path == "/sso/v2/qrcode/image":
            state["qrid"] += 1
            return httpx.Response(200, json={"retcode": 20000000, "data": {
                "qrid": str(state["qrid"]),
                "image": "https://example/qrcode?data=https%3A%2F%2Fscan",
            }})
        if request.url.path == "/sso/v2/qrcode/check":
            state["checks"] += 1
            assert request.headers["x-csrf-token"] == "csrf"
            assert "X-CSRF-TOKEN=csrf" in request.headers["cookie"]
            if state["checks"] > 1:
                assert "check-session=retained" in request.headers["cookie"]
            return httpx.Response(200, json={
                "retcode": state["retcode"], "data": {"alt": "ticket"},
            }, headers={"set-cookie": "check-session=retained; Path=/"})
        if request.url.path == "/sso/v2/login":
            assert request.url.params["alt"] == "ticket"
            return httpx.Response(200, headers={
                "set-cookie": "SUB=private-session; Domain=.weibo.com; Path=/",
            })
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"data": {
                "login": state["valid"], "uid": "123", "st": "private-csrf",
            }})
        raise AssertionError(f"Unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(weibo.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    crawler = WeiboCrawler(tmp_path / "cookies.json")
    monkeypatch.setattr(server, "_crawler", crawler)
    return crawler, state


@pytest.mark.asyncio
async def test_mcp_qr_image_and_login_lifecycle(qr_flow, capsys):
    crawler, state = qr_flow
    async with Client(server.mcp) as client:
        result = await client.call_tool("start_qr_login", {})
        metadata = result.structured_content
        assert metadata["status"] == "waiting"
        assert metadata["expires_in"] == 240
        assert len(result.content) == 1
        image = result.content[0]
        assert image.type == "image"
        assert image.mimeType == "image/png"
        assert base64.b64decode(image.data).startswith(b"\x89PNG\r\n\x1a\n")
        # The generated image encodes the same scan URL used by terminal login.
        assert crawler._qr_login.scan_url == "https://scan"
        assert not crawler.cookie_file.exists()
        for code, status in [(50114001, "waiting"), (50114002, "scanned"),
                             (20000000, "authenticated")]:
            state["retcode"] = code
            checked = await client.call_tool("check_qr_login", {
                "login_id": metadata["login_id"],
            })
            assert checked.structured_content["status"] == status
            assert checked.content == []
            assert "private-" not in str(checked.structured_content)
        assert checked.structured_content == {
            "status": "authenticated", "login": True, "uid": "123",
        }
        assert (await client.call_tool("get_session", {})).structured_content == {
            "login": True, "uid": "123",
        }
    assert crawler._qr_login is None
    assert json.loads(crawler.cookie_file.read_text())["cookies"]["SUB"] == "private-session"
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_replaced_unknown_and_locally_expired_attempts(qr_flow):
    crawler, state = qr_flow
    first, _ = await crawler.start_qr_login()
    second, _ = await crawler.start_qr_login()
    assert first["login_id"] != second["login_id"]
    for login_id in (first["login_id"], "unknown", "无效登录编号"):
        with pytest.raises(RuntimeError, match="Unknown login_id"):
            await crawler.check_qr_login(login_id)
    crawler._qr_login.expires_at = 0
    assert await crawler.check_qr_login(second["login_id"]) == {"status": "expired"}
    assert state["checks"] == 0
    assert crawler._qr_login is None
    assert not crawler.cookie_file.exists()


@pytest.mark.asyncio
async def test_remote_expiry(qr_flow):
    crawler, state = qr_flow
    metadata, _ = await crawler.start_qr_login()
    state["retcode"] = 50114004
    assert await crawler.check_qr_login(metadata["login_id"]) == {"status": "expired"}
    assert crawler._qr_login is None
    assert not crawler.cookie_file.exists()


@pytest.mark.asyncio
async def test_failed_validation_preserves_existing_session(qr_flow):
    crawler, state = qr_flow
    crawler.save_cookies({"SUB": "existing"})
    metadata, _ = await crawler.start_qr_login()
    state.update(retcode=20000000, valid=False)
    async with Client(server.mcp) as client:
        result = await client.call_tool("check_qr_login", {
            "login_id": metadata["login_id"],
        }, raise_on_error=False)
    assert result.is_error
    assert "valid session" in result.content[0].text
    assert crawler.cookies == {"SUB": "existing"}
    assert json.loads(crawler.cookie_file.read_text())["cookies"] == {"SUB": "existing"}


@pytest.mark.asyncio
async def test_env_cookie_login_fails_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIBO_COOKIE", "SUB=env-session")
    crawler = WeiboCrawler(tmp_path / "cookies.json")
    monkeypatch.setattr(weibo.httpx, "AsyncClient", lambda **kw: pytest.fail("network used"))
    with pytest.raises(RuntimeError, match="Remove WEIBO_COOKIE"):
        await crawler.start_qr_login()
    assert crawler.cookies == {"SUB": "env-session"}
