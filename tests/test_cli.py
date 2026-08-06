from click.testing import CliRunner

from mcp_server_weibo import weibo_cli


def test_login_command_runs_qr_login(monkeypatch):
    calls = []

    class FakeCrawler:
        async def qr_login(self, timeout):
            calls.append(timeout)
            return {"uid": "123"}

    monkeypatch.setattr(weibo_cli, "WeiboCrawler", FakeCrawler)
    result = CliRunner().invoke(weibo_cli.cli, ["login", "--timeout", "7"])

    assert result.exit_code == 0
    assert calls == [7]
    assert "登录成功，UID: 123" in result.output


def test_login_command_reports_failure(monkeypatch):
    class FakeCrawler:
        async def qr_login(self, timeout):
            raise RuntimeError("微博 Cookie 已失效")

    monkeypatch.setattr(weibo_cli, "WeiboCrawler", FakeCrawler)
    result = CliRunner().invoke(weibo_cli.cli, ["login"])

    assert result.exit_code != 0
    assert "微博 Cookie 已失效" in result.output
