from mcp_server_weibo.weibo import WeiboCrawler


def test_feed_dump_drops_heavy_fields():
    feed = WeiboCrawler()._to_feed_item({
        "id": 1,
        "text": "正文",
        "source": "iPhone",
        "created_at": "刚刚",
        "comments_count": 1,
        "attitudes_count": 2,
        "reposts_count": 3,
        "raw_text": "should not appear",
        "region_name": "",
        "user": {
            "id": 2,
            "screen_name": "用户",
            "profile_image_url": "https://example/avatar.jpg",
            "avatar_hd": "https://example/hd.jpg",
            "profile_url": "https://m.weibo.cn/u/2",
            "description": "简介",
            "follow_count": 21,
            "followers_count": "1.8万",
            "verified": True,
            "verified_reason": "博主",
            "gender": "m",
        },
        "pics": [{"url": "https://example/thumb.jpg", "large": {"url": "https://example/large.jpg"}}],
        "page_info": {
            "type": "video",
            "page_url": "https://video.weibo.com/show?fid=1",
            "media_info": {
                "stream_url": "https://cdn.example/ld.mp4?sig=expired",
                "stream_url_hd": "https://cdn.example/hd.mp4?sig=expired",
            },
        },
    })

    dumped = feed.model_dump()
    assert dumped["user"] == {"id": 2, "screen_name": "用户", "verified": True}
    assert dumped["pics"] == ["https://example/large.jpg"]
    assert dumped["video_url"] == "https://video.weibo.com/show?fid=1"
    assert "raw_text" not in dumped
    assert "videos" not in dumped
    assert "region_name" not in dumped
    assert "profile_image_url" not in dumped["user"]
    assert "followers_count" not in dumped["user"]


def test_profile_keeps_subject_fields_only():
    profile = WeiboCrawler()._to_user_profile({
        "id": 2,
        "screen_name": "用户",
        "description": "简介",
        "followers_count": "1.8万",
        "verified": True,
        "profile_image_url": "https://example/avatar.jpg",
        "avatar_hd": "https://example/hd.jpg",
        "profile_url": "https://m.weibo.cn/u/2",
        "follow_count": 21,
        "verified_reason": "博主",
        "gender": "m",
    })
    dumped = profile.model_dump()
    assert dumped == {
        "id": 2,
        "screen_name": "用户",
        "description": "简介",
        "followers_count": "1.8万",
        "verified": True,
    }


def test_trending_omits_search_url():
    item = WeiboCrawler()._to_trending_item({
        "id": 1,
        "desc": "热搜",
        "desc_extr": "123456",
        "scheme": "https://m.weibo.cn/search?containerid=" + "x" * 400,
    })
    dumped = item.model_dump()
    assert dumped == {"id": 1, "trending": 123456, "description": "热搜"}
    assert "url" not in dumped
