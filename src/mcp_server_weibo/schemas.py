from typing import Union
from pydantic import BaseModel, Field, model_serializer


class CompactModel(BaseModel):
    """Drop empty optional values so MCP/CLI payloads stay small."""

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        return {key: value for key, value in handler(self).items() if value not in (None, "", [], {})}


class UserRef(CompactModel):
    """Nested author on a feed or comment."""

    id: int = Field()
    screen_name: str = Field()
    verified: bool = Field(default=False)


class UserProfile(CompactModel):
    """User as the subject of profile, search, followers, or fans."""

    id: int = Field()
    screen_name: str = Field()
    description: str = Field(default="")
    followers_count: str = Field(default="")
    verified: bool = Field(default=False)


class FeedItem(CompactModel):
    """A single Weibo post."""

    id: int = Field()
    text: str = Field()
    created_at: str = Field(default="")
    user: Union[UserRef, None] = Field(default=None)
    pics: list[str] = Field(default_factory=list)
    video_url: str = Field(default="")


class PagedFeeds(BaseModel):
    SinceId: Union[int, str] = Field()
    Feeds: list[FeedItem] = Field()


class TrendingItem(CompactModel):
    id: int = Field()
    trending: int = Field()
    description: str = Field()


class CommentItem(CompactModel):
    id: int = Field()
    text: str = Field()
    created_at: str = Field(default="")
    source: str = Field(default="")
    user: Union[UserRef, None] = Field(default=None)
    reply_id: Union[int, None] = Field(default=None)
    reply_text: str = Field(default="")
