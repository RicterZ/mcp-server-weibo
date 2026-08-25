"""
Unit tests for WeiboCrawler.
Requires WEIBO_COOKIE environment variable for real API requests.
"""
import pytest
import asyncio
import os

from pathlib import Path
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from mcp_server_weibo.weibo import WeiboCrawler


COOKIE_ENV = "WEIBO_COOKIE"


def has_cookie():
    return bool(os.environ.get(COOKIE_ENV, ""))


@pytest.fixture
def crawler():
    if not has_cookie():
        pytest.skip(f"Set {COOKIE_ENV} to run live Weibo API tests")
    return WeiboCrawler()


class TestWeiboCrawler:
    """Real API tests - require WEIBO_COOKIE environment variable."""

    @pytest.mark.asyncio
    async def test_get_profile(self, crawler):
        """Test fetching user profile with real API."""
        result = await crawler.get_profile(uid=1749127163)

        assert result is not None, "Profile result should not be None"
        assert hasattr(result, 'id'), "Profile should have 'id' attribute"
        assert hasattr(result, 'screen_name'), "Profile should have 'screen_name' attribute"
        assert hasattr(result, 'description'), "Profile should have 'description' attribute"
        assert hasattr(result, 'followers_count'), "Profile should have 'followers_count' attribute"
        assert result.id == 1749127163, f"Profile ID should be 1749127163, got {result.id}"
        assert isinstance(result.screen_name, str), "screen_name should be a string"
        assert len(result.screen_name) > 0, "screen_name should not be empty"

    @pytest.mark.asyncio
    async def test_get_feeds(self, crawler):
        """Test fetching user feeds with real API."""
        result = await crawler.get_feeds(uid=1749127163, limit=5)

        assert isinstance(result, list), "Feeds should be a list"
        assert len(result) > 0, "Should return at least one feed"

        feed = result[0]
        assert hasattr(feed, 'id'), "Feed should have 'id' attribute"
        assert hasattr(feed, 'text'), "Feed should have 'text' attribute"
        assert hasattr(feed, 'created_at'), "Feed should have 'created_at' attribute"
        assert hasattr(feed, 'user'), "Feed should have 'user' attribute"
        assert isinstance(feed.id, int), "Feed ID should be an integer"
        assert isinstance(feed.text, str), "Feed text should be a string"

    @pytest.mark.asyncio
    async def test_get_hot_feeds(self, crawler):
        """Test fetching hot feeds with real API."""
        result = await crawler.get_hot_feeds(uid=1749127163, limit=5)

        assert isinstance(result, list), "Hot feeds should be a list"
        assert len(result) <= 5, f"Should return at most 5 hot feeds, got {len(result)}"

        if len(result) > 0:
            feed = result[0]
            assert hasattr(feed, 'id'), "Hot feed should have 'id' attribute"
            assert hasattr(feed, 'text'), "Hot feed should have 'text' attribute"

    @pytest.mark.asyncio
    async def test_search_users(self, crawler):
        """Test searching users with real API."""
        result = await crawler.search_users(keyword="程序员", limit=5)

        assert isinstance(result, list), "Search results should be a list"
        assert len(result) <= 5, f"Should return at most 5 users, got {len(result)}"

        if len(result) > 0:
            user = result[0]
            assert hasattr(user, 'id'), "User should have 'id' attribute"
            assert hasattr(user, 'screen_name'), "User should have 'screen_name' attribute"
            assert isinstance(user.id, int), "User ID should be an integer"
            assert isinstance(user.screen_name, str), "User screen_name should be a string"

    @pytest.mark.asyncio
    async def test_get_trendings(self, crawler):
        """Test fetching trending topics with real API."""
        result = await crawler.get_trendings(limit=10)

        assert isinstance(result, list), "Trending results should be a list"
        assert len(result) <= 10, f"Should return at most 10 trending items, got {len(result)}"

        if len(result) > 0:
            item = result[0]
            assert hasattr(item, 'id'), "Trending item should have 'id' attribute"
            assert hasattr(item, 'description'), "Trending item should have 'description' attribute"
            assert hasattr(item, 'trending'), "Trending item should have 'trending' attribute"
            assert isinstance(item.trending, int), "Trending value should be an integer"

    @pytest.mark.asyncio
    async def test_search_content(self, crawler):
        """Test searching content with real API."""
        result = await crawler.search_content(keyword="python", limit=5)

        assert isinstance(result, list), "Search results should be a list"
        assert len(result) <= 5, f"Should return at most 5 content results, got {len(result)}"

        if len(result) > 0:
            feed = result[0]
            assert hasattr(feed, 'id'), "Content should have 'id' attribute"
            assert hasattr(feed, 'text'), "Content should have 'text' attribute"
            assert isinstance(feed.id, int), "Content ID should be an integer"

    @pytest.mark.asyncio
    async def test_get_comments(self, crawler):
        """Test fetching comments with real API."""
        result = await crawler.get_comments(feed_id="4939868213786618", page=1)

        assert isinstance(result, list), "Comments should be a list"

        if len(result) > 0:
            comment = result[0]
            assert hasattr(comment, 'id'), "Comment should have 'id' attribute"
            assert hasattr(comment, 'text'), "Comment should have 'text' attribute"
            assert hasattr(comment, 'user'), "Comment should have 'user' attribute"
            assert isinstance(comment.id, int), "Comment ID should be an integer"

    @pytest.mark.asyncio
    async def test_get_followers(self, crawler):
        """Test fetching followers with real API."""
        result = await crawler.get_followers(uid=1749127163, limit=5)

        assert isinstance(result, list), "Followers should be a list"
        assert len(result) <= 5, f"Should return at most 5 followers, got {len(result)}"

        if len(result) > 0:
            user = result[0]
            assert hasattr(user, 'id'), "Follower should have 'id' attribute"
            assert hasattr(user, 'screen_name'), "Follower should have 'screen_name' attribute"
            assert isinstance(user.id, int), "Follower ID should be an integer"

    @pytest.mark.asyncio
    async def test_get_fans(self, crawler):
        """Test fetching fans with real API."""
        result = await crawler.get_fans(uid=1749127163, limit=5)

        assert isinstance(result, list), "Fans should be a list"
        assert len(result) <= 5, f"Should return at most 5 fans, got {len(result)}"

        if len(result) > 0:
            user = result[0]
            assert hasattr(user, 'id'), "Fan should have 'id' attribute"
            assert hasattr(user, 'screen_name'), "Fan should have 'screen_name' attribute"
            assert isinstance(user.id, int), "Fan ID should be an integer"

    @pytest.mark.asyncio
    async def test_search_topics(self, crawler):
        """Test searching topics with real API."""
        result = await crawler.search_topics(keyword="编程", limit=5)

        assert isinstance(result, list), "Search results should be a list"
        assert len(result) <= 5, f"Should return at most 5 topics, got {len(result)}"

        if len(result) > 0:
            topic = result[0]
            assert isinstance(topic, dict), "Topic should be a dictionary"
            assert 'title' in topic, "Topic should have 'title' key"
            assert 'url' in topic, "Topic should have 'url' key"
            assert isinstance(topic['title'], str), "Topic title should be a string"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
