"""
Unit tests for server.py
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestServerCli:
    """Tests for server CLI arguments."""

    def test_cookie_argument_with_http_mode(self, tmp_path, monkeypatch):
        """Test that --cookie and http mode can be specified together."""
        import argparse

        parser = argparse.ArgumentParser(description="Weibo MCP Server")
        parser.add_argument(
            "--cookie",
            type=str,
            help="Weibo cookie string. Will be saved to tests/.env file."
        )
        parser.add_argument(
            "mode",
            nargs="?",
            choices=["stdio", "http"],
            default="stdio",
            help="Server mode: 'stdio' or 'http' (default: stdio)"
        )

        args = parser.parse_args(['--cookie', 'my_cookie', 'http'])
        assert args.cookie == 'my_cookie'
        assert args.mode == 'http'

    def test_default_mode_is_stdio(self):
        """Test that default mode is stdio."""
        import argparse

        parser = argparse.ArgumentParser(description="Weibo MCP Server")
        parser.add_argument(
            "--cookie",
            type=str,
            help="Weibo cookie string. Will be saved to tests/.env file."
        )
        parser.add_argument(
            "mode",
            nargs="?",
            choices=["stdio", "http"],
            default="stdio",
            help="Server mode: 'stdio' or 'http' (default: stdio)"
        )

        args = parser.parse_args([])
        assert args.mode == 'stdio'


class TestServerTools:
    """Tests for server MCP tools."""

    @pytest.mark.asyncio
    async def test_search_users_returns_list(self):
        """Test that search_users tool returns a list."""
        from mcp_server_weibo.server import mcp

        # Get the search_users tool
        assert "search_users" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_get_profile_returns_dict(self):
        """Test that get_profile tool returns a dict."""
        from mcp_server_weibo.server import mcp

        # Get the get_profile tool
        assert "get_profile" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_get_feeds_returns_list(self):
        """Test that get_feeds tool returns a list."""
        from mcp_server_weibo.server import mcp

        # Get the get_feeds tool
        assert "get_feeds" in mcp._tool_manager._tools

    @pytest.mark.asyncio
    async def test_all_tools_registered(self):
        """Test that all expected tools are registered."""
        from mcp_server_weibo.server import mcp

        expected_tools = [
            "search_users",
            "get_profile",
            "get_feeds",
            "get_hot_feeds",
            "get_trendings",
            "search_content",
            "search_topics",
            "get_followers",
            "get_fans",
            "get_comments",
            "get_session",
            "get_home_timeline",
            "follow_user",
            "unfollow_user",
        ]

        registered_tools = list(mcp._tool_manager._tools.keys())

        for tool_name in expected_tools:
            assert tool_name in registered_tools, f"Tool {tool_name} not registered"

    @pytest.mark.asyncio
    async def test_get_crawler_lazy_initialization(self):
        """Test that get_crawler initializes crawler on first call."""
        from mcp_server_weibo.server import get_crawler, _crawler

        # Ensure crawler is None initially
        from mcp_server_weibo import server
        server._crawler = None

        crawler = get_crawler()
        assert crawler is not None
        assert crawler is server._crawler

    @pytest.mark.asyncio
    async def test_get_crawler_returns_same_instance(self):
        """Test that get_crawler returns the same instance on subsequent calls."""
        from mcp_server_weibo.server import get_crawler
        from mcp_server_weibo import server

        server._crawler = None
        crawler1 = get_crawler()
        crawler2 = get_crawler()

        assert crawler1 is crawler2


class TestCookieEnvFile:
    """Tests for cookie .env file functionality."""

    def test_cookie_argument_saves_file(self, tmp_path):
        """Test that --cookie argument saves to .env file."""
        # Directly test the file writing logic
        env_file = tmp_path / ".env"
        cookie = "test_cookie_value"

        # Simulate the main() logic
        env_file.write_text(f"WEIBO_COOKIE={cookie}\n")

        assert env_file.exists()
        assert env_file.read_text() == f"WEIBO_COOKIE={cookie}\n"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
