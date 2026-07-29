import asyncio
from http.cookies import SimpleCookie
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import qrcode

from mcp_server_weibo.consts import DEFAULT_HEADERS, PROFILE_URL, FEEDS_URL, SEARCH_URL, COMMENTS_URL
from mcp_server_weibo.schemas import PagedFeeds, TrendingItem, FeedItem, UserProfile, CommentItem


WEB_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://weibo.com/",
}
PASSPORT_HEADERS = {
    **WEB_HEADERS,
    "Referer": "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https://weibo.com/",
    "X-Requested-With": "XMLHttpRequest",
}


class WeiboCrawler:
    """
    A crawler class for extracting data from Weibo (Chinese social media platform).
    Provides functionality to fetch user profiles, feeds, and search for users.

    Cookie can be provided via WEIBO_COOKIE or a persistent JSON file.
    """
    COOKIE_ENV = "WEIBO_COOKIE"
    COOKIE_FILE_ENV = "WEIBO_COOKIE_FILE"
    DEFAULT_COOKIE_FILE = Path.home() / ".config" / "mcp-server-weibo" / "cookies.json"

    def __init__(self, cookie_file: str | Path | None = None):
        self.logger = logging.getLogger(__name__)
        self.cookie_file = Path(cookie_file or os.environ.get(self.COOKIE_FILE_ENV, self.DEFAULT_COOKIE_FILE)).expanduser()
        self.cookies = self._load_cookies()

    @staticmethod
    def parse_cookie(cookie_header: str) -> dict[str, str]:
        parsed = SimpleCookie()
        parsed.load(cookie_header)
        return {name: morsel.value for name, morsel in parsed.items()}

    def _load_cookies(self) -> dict[str, str] | None:
        if cookie_header := os.environ.get(self.COOKIE_ENV):
            cookies = self.parse_cookie(cookie_header)
            if cookies:
                return cookies
        if not self.cookie_file.exists():
            return None
        try:
            data = json.loads(self.cookie_file.read_text())
            cookies = data.get("cookies", data)
            return cookies if isinstance(cookies, dict) and cookies else None
        except (OSError, json.JSONDecodeError):
            self.logger.warning("Unable to load Weibo cookies from %s", self.cookie_file)
            return None

    def save_cookies(self, cookies: dict[str, str]) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_file.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False))
        self.cookie_file.chmod(0o600)
        self.cookies = cookies

    async def _validate_cookies(self, cookies: dict) -> bool:
        try:
            async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, trust_env=False) as client:
                response = await client.get("https://weibo.com/ajax/config/get_config", headers=WEB_HEADERS)
                response.raise_for_status()
                data = response.json().get("data", {})
                return bool(data.get("login") and data.get("uid"))
        except (httpx.HTTPError, ValueError):
            return False

    async def _ensure_cookies(self, retry: bool = False) -> dict:
        if self.cookies:
            return self.cookies
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    "https://visitor.passport.weibo.cn/visitor/genvisitor2",
                    data={
                        "cb": "visitor_callback",
                        "from": "weibo",
                        "tid": "",
                        "return_url": "https://m.weibo.cn/",
                    },
                    headers={"User-Agent": DEFAULT_HEADERS.get("User-Agent", "")},
                )
                response.raise_for_status()
                match = re.search(r"visitor_callback\((.*)\)", response.text)
                if not match:
                    raise ValueError("Invalid visitor passport response")
                
                payload = json.loads(match.group(1))
                data = payload.get("data", {})
                sub = data.get("sub")
                subp = data.get("subp")
                if not sub or not subp:
                    raise ValueError("Missing SUB/SUBP in visitor passport response")

                self.cookies = {"SUB": sub, "SUBP": subp}
                return self.cookies
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            self.logger.error("Unable to initialize Weibo visitor cookies", exc_info=True)
            self.cookies = None
            if not retry:
                return await self._ensure_cookies(retry=True)
            raise

    async def get_session(self) -> dict:
        """Return the current real-user login state without exposing cookies."""
        if not self.cookies:
            return {"login": False, "uid": None}
        try:
            async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, trust_env=False) as client:
                response = await client.get("https://weibo.com/ajax/config/get_config", headers=WEB_HEADERS)
                response.raise_for_status()
                data = response.json().get("data", {})
                return {"login": bool(data.get("login")), "uid": data.get("uid")}
        except (httpx.HTTPError, ValueError):
            return {"login": False, "uid": None}

    async def _require_authenticated(self) -> None:
        if not (await self.get_session()).get("login"):
            raise RuntimeError("Weibo login is missing or expired; run `mcp-server-weibo login`")

    async def qr_login(self, timeout: int = 240) -> dict:
        """Log in with the Weibo app QR scanner and persist the resulting cookies."""
        params = {"entry": "miniblog", "source": "miniblog", "url": "https://weibo.com/"}
        async with httpx.AsyncClient(
            base_url="https://passport.weibo.com",
            headers=PASSPORT_HEADERS,
            follow_redirects=True,
            timeout=30,
            trust_env=False,
        ) as client:
            response = await client.get("/sso/signin", params=params)
            response.raise_for_status()
            csrf = self._cookie_value(client.cookies, "X-CSRF-TOKEN")
            if not csrf:
                raise RuntimeError("Weibo did not return a QR login CSRF token")
            client.headers["X-CSRF-TOKEN"] = csrf

            response = await client.get("/sso/v2/qrcode/image", params={"entry": "miniblog", "size": "180"})
            response.raise_for_status()
            payload = response.json()
            if payload.get("retcode") != 20000000:
                raise RuntimeError(payload.get("msg", "Unable to create Weibo login QR code"))
            qrid = payload["data"]["qrid"]
            image_url = payload["data"].get("image", "")
            scan_url = parse_qs(urlparse(image_url).query).get(
                "data", [f"https://passport.weibo.cn/signin/qrcode/scan?qr={qrid}"]
            )[0]

            print("\n请使用微博 App 扫描二维码，并在手机上确认登录：\n")
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
            qr.add_data(scan_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print("\n等待扫码（最多 4 分钟）…", flush=True)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            scanned = False
            while loop.time() < deadline:
                response = await client.get(
                    "/sso/v2/qrcode/check",
                    params={**params, "qrid": qrid, "rid": "", "ver": "20250520"},
                )
                response.raise_for_status()
                payload = response.json()
                retcode = payload.get("retcode")
                if retcode == 20000000:
                    data = payload.get("data", {})
                    if cross_url := data.get("url"):
                        await client.get(cross_url)
                    if alt := data.get("alt"):
                        await client.get(
                            "https://login.sina.com.cn/sso/login.php",
                            params={"entry": "miniblog", "alt": alt, "returntype": "TEXT"},
                        )
                    cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
                    if not await self._validate_cookies(cookies):
                        raise RuntimeError("QR scan completed, but Weibo did not create a valid session")
                    self.save_cookies(cookies)
                    return await self.get_session()
                if retcode == 50114002 and not scanned:
                    print("已扫码，请在手机上确认…", flush=True)
                    scanned = True
                elif retcode == 50114004:
                    raise RuntimeError("Weibo login QR code expired")
                elif retcode not in (50114001, 50114002):
                    raise RuntimeError(payload.get("msg", f"QR login failed ({retcode})"))
                await asyncio.sleep(2)
        raise RuntimeError("Weibo login QR code timed out")

    @staticmethod
    def _cookie_value(cookies: httpx.Cookies, name: str) -> str | None:
        return next((cookie.value for cookie in cookies.jar if cookie.name == name), None)

    async def get_home_timeline(self, limit: int = 20, max_id: str = "0") -> list[FeedItem]:
        """Get the logged-in user's following timeline."""
        await self._require_authenticated()
        async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, trust_env=False) as client:
            response = await client.get(
                "https://weibo.com/ajax/feed/friendstimeline",
                params={"count": min(max(limit, 1), 50), "max_id": max_id},
                headers=WEB_HEADERS,
            )
            response.raise_for_status()
            statuses = response.json().get("statuses", [])
            return [self._to_feed_item(status) for status in statuses[:limit]]

    async def follow_user(self, uid: int) -> dict:
        """Follow a user as the logged-in account."""
        return await self._friendship_action("create", uid, "friend_uid")

    async def unfollow_user(self, uid: int) -> dict:
        """Unfollow a user as the logged-in account."""
        # Weibo's real endpoint intentionally misspells "destroy" as "destory".
        return await self._friendship_action("destory", uid, "uid")

    async def _friendship_action(self, action: str, uid: int, uid_field: str) -> dict:
        await self._require_authenticated()
        async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, trust_env=False) as client:
            await client.get("https://weibo.com/", headers=WEB_HEADERS)
            xsrf = self._cookie_value(client.cookies, "XSRF-TOKEN")
            if not xsrf:
                raise RuntimeError("Weibo session is missing XSRF-TOKEN; log in again")
            response = await client.post(
                f"https://weibo.com/ajax/friendships/{action}",
                json={uid_field: str(uid), "lpage": "profile", "page": "profile"},
                headers={**WEB_HEADERS, "X-XSRF-TOKEN": xsrf},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("ok") != 1:
                raise RuntimeError(data.get("msg", f"Weibo friendship {action} failed"))
            return data


    async def get_profile(self, uid: int) -> UserProfile:
        """
        Extract user profile information from Weibo.

        Args:
            uid (int): The unique identifier of the Weibo user

        Returns:
            UserProfile: User profile information or empty dict if extraction fails
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                response=await client.get(PROFILE_URL.format(userId=uid), headers=DEFAULT_HEADERS)
                result=response.json()
                return self._to_user_profile(result["data"]["userInfo"])
            except httpx.HTTPError:
                self.logger.error(
                    f"Unable to extract profile for uid '{str(uid)}'", exc_info=True)
                return {}

    async def get_feeds(self, uid: int, limit: int=15) -> list[FeedItem]:
        """
        Extract user's Weibo feeds (posts) with pagination support.

        Args:
            uid (int): The unique identifier of the Weibo user
            limit (int): Maximum number of feeds to extract, defaults to 15

        Returns:
            list[FeedItem]: List of user's Weibo feeds
        """
        feeds=[]
        sinceId=''
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            containerId=await self._get_container_id(client, uid)

            while len(feeds) < limit:
                pagedFeeds=await self._extract_feeds(client, uid, containerId, sinceId)
                if not pagedFeeds.Feeds:
                    break

                feeds.extend(pagedFeeds.Feeds)
                sinceId=pagedFeeds.SinceId
                if not sinceId:
                    break

        return feeds

    async def get_hot_feeds(self, uid: int, limit: int=15) -> list[FeedItem]:
        """
        Extract hot feeds

        Args:
            uid (int): The unique identifier of the Weibo user
            limit (int): Maximum number of hot feeds to extract, defaults to 15

        Returns:
            list[FeedItem]: List of hot feeds from the user's profile
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                params={
                    'containerid': f'231002{str(uid)}_-_HOTMBLOG',
                    'type': 'uid',
                    'value': uid,
                }
                encoded_params=urlencode(params)

                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                result=response.json()
                cards=list(
                    filter(lambda x: x['card_type'] == 9, result["data"]["cards"]))
                feeds=[self._to_feed_item(item['mblog']) for item in cards]
                return feeds[:limit]
            except httpx.HTTPError:
                self.logger.error(
                    f"Unable to extract hot feeds for uid '{str(uid)}'", exc_info=True)
                return []

    async def search_users(self, keyword: str, limit: int=5, page: int=1) -> list[UserProfile]:
        """
        Search for Weibo users based on a keyword.

        Args:
            keyword (str): Search term to find users
            limit (int): Maximum number of users to return, defaults to 5

        Returns:
            list[UserProfile]: List of UserProfile objects containing user information
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                params={
                    'containerid': f'100103type=3&q={keyword}',
                    'page_type': 'searchall',
                    'page': page,
                }
                encoded_params=urlencode(params)

                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                result=response.json()
                cards=result["data"]["cards"]
                if len(cards) < 2:
                    return []
                else:
                    cardGroup=cards[1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError:
                self.logger.error(
                    f"Unable to search users for keyword '{keyword}'", exc_info=True)
                return []

    
    async def get_trendings(self, limit: int=15) -> list[TrendingItem]:
        """
        Get a list of hot search items from Weibo.

        Args:
            limit (int): Maximum number of hot search items to return, defaults to 15

        Returns:
            list[HotSearchItem]: List of HotSearchItem objects containing hot search information
        """
        try:
            params={
                'containerid': f'106003type=25&t=3&disable_hot=1&filter_type=realtimehot',
            }
            encoded_params=urlencode(params)

            await self._ensure_cookies()
            async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                data=response.json()
                cards=data.get('data', {}).get('cards', [])
                if not cards:
                    return []

                hot_search_card=next((card for card in cards if 'card_group' in card and isinstance(
                    card['card_group'], list)), None)
                if not hot_search_card or 'card_group' not in hot_search_card:
                    return []

                items=[item for item in hot_search_card['card_group']
                    if item.get('desc')]
                trending_items=list(map(lambda pair: self._to_trending_item(
                    {**pair[1], 'id': pair[0]}), enumerate(items[:limit])))
                return trending_items
        except httpx.HTTPError:
            self.logger.error(
                'Unable to fetch Weibo hot search list', exc_info=True)
            return []

    async def search_content(self, keyword: str, limit: int=15, page: int=1) -> list[FeedItem]:
        """
        Search Weibo content (posts) by keyword.

        Args:
            keyword (str): The search keyword
            limit (int): Maximum number of content results to return, defaults to 15
            page (int, optional): The starting page number, defaults to 1

        Returns:
            list[FeedItem]: List of FeedItem objects containing content search results
        """
        results=[]
        current_page=page
        try:
            await self._ensure_cookies()
            while len(results) < limit:
                params={
                    'containerid': f'100103type=1&q={keyword}',
                    'page_type': 'searchall',
                    'page': current_page,
                }
                encoded_params=urlencode(params)

                async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
                    response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                    data=response.json()

                cards=data.get('data', {}).get('cards', [])
                content_cards=[]
                for card in cards:
                    if card.get('card_type') == 9:
                        content_cards.append(card)
                    elif 'card_group' in card and isinstance(card['card_group'], list):
                        content_group=[
                            item for item in card['card_group'] if item.get('card_type') == 9]
                        content_cards.extend(content_group)

                if not content_cards:
                    break

                for card in content_cards:
                    if len(results) >= limit:
                        break

                    mblog=card.get('mblog')
                    if not mblog:
                        continue

                    content_result=self._to_feed_item(mblog)
                    results.append(content_result)

                current_page += 1
                cardlist_info=data.get('data', {}).get('cardlistInfo', {})
                if not cardlist_info.get('page') or str(cardlist_info.get('page')) == '1':
                    break
            return results[:limit]
        except httpx.HTTPError:
            self.logger.error(
                f"Unable to search Weibo content for keyword '{keyword}'", exc_info=True)
            return []

    async def search_topics(self, keyword: str, limit: int=15, page: int=1) -> list[dict]:
        """
        Search Weibo topics by keyword.

        Args:
            keyword (str): The search keyword
            limit (int): Maximum number of topic results to return, defaults to 15
            page (int, optional): The starting page number, defaults to 1

        Returns:
            list[dict: List of dict containing topic search results
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                params={
                    'containerid': f'100103type=38&q={keyword}',
                    'page_type': 'searchall',
                    'page': page,
                }
                encoded_params=urlencode(params)

                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                result=response.json()
                cards=result.get("data", {}).get("cards", [])
                for card in cards:
                    card_group=card.get('card_group')
                    if card_group:
                        return [self._to_topic_item(item) for item in card_group][:limit]
                return []
            except (httpx.HTTPError, httpx.ConnectError, KeyError):
                self.logger.error(
                    f"Unable to search topics for keyword '{keyword}'", exc_info=True)
                return []

    async def get_comments(self, feed_id: str, page: int=1) -> list[CommentItem]:
        """
        Get comments for a specific Weibo post.

        Args:
            feed_id (str): The ID of the Weibo post
            page (int): The page number for pagination, defaults to 1

        Returns:
            list[CommentItem]: List of comments for the specified Weibo post
        """
        try:
            await self._ensure_cookies()
            async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
                url=COMMENTS_URL.format(feed_id=feed_id, page=page)
                response=await client.get(url, headers=DEFAULT_HEADERS)
                data=response.json()
                comments=data.get('data', {}).get('data', [])
                return [self._to_comment_item(comment) for comment in comments]
        except httpx.HTTPError:
            self.logger.error(
                f"Unable to fetch comments for feed_id '{feed_id}'", exc_info=True)
            return []

    async def get_followers(self, uid: int, limit: int=15, page: int=1) -> list[UserProfile]:
        """
        Get followers of a specific Weibo user.

        Args:
            uid (int): The unique identifier of the Weibo user
            limit (int): Maximum number of followers to return, defaults to 15
            page (int): The page number for pagination, defaults to 1

        Returns:
            list[UserProfile]: List of UserProfile objects containing follower information
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                params={
                    'containerid': f'231051_-_followers_-_{str(uid)}',
                    'page': page,
                }
                encoded_params=urlencode(params)

                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                result=response.json()
                cards=result["data"]["cards"]
                if len(cards) < 1:
                    return []
                else:
                    cardGroup=cards[-1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError:
                self.logger.error(
                    f"Unable to get followers for uid '{str(uid)}'", exc_info=True)
                return []

    async def get_fans(self, uid: int, limit: int=15, page: int=1) -> list[UserProfile]:
        """
        Get fans of a specific Weibo user.

        Args:
            uid (int): The unique identifier of the Weibo user
            limit (int): Maximum number of fans to return, defaults to 15
            page (int): The page number for pagination, defaults to 1

        Returns:
            list[UserProfile]: List of UserProfile objects containing fan information
        """
        await self._ensure_cookies()
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False) as client:
            try:
                params={
                    'containerid': f'231051_-_fans_-_{str(uid)}',
                    'page': page,
                }
                encoded_params=urlencode(params)

                response=await client.get(f'{SEARCH_URL}?{encoded_params}', headers=DEFAULT_HEADERS)
                result=response.json()
                cards=result["data"]["cards"]
                if len(cards) < 1:
                    return []
                else:
                    cardGroup=cards[-1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError:
                self.logger.error(
                    f"Unable to get fans for uid '{str(uid)}'", exc_info=True)
                return []

    async def _get_container_id(self, client, uid: int):
        """
        Get the container ID for a user's Weibo feed.

        Args:
            client (httpx.AsyncClient): HTTP client instance
            uid (int): The unique identifier of the Weibo user

        Returns:
            str: Container ID for the user's feed or None if extraction fails
        """
        try:
            response=await client.get(PROFILE_URL.format(userId=str(uid)), headers=DEFAULT_HEADERS)
            data=response.json()
            tabs_info=data.get("data", {}).get(
                "tabsInfo", {}).get("tabs", [])
            for tab in tabs_info:
                if tab.get("tabKey") == "weibo":
                    return tab.get("containerid")
        except httpx.HTTPError:
            self.logger.error(
                f"Unable to extract containerId for uid '{str(uid)}'", exc_info=True)
            return None

    async def _extract_feeds(self, client, uid: int, container_id: str, since_id: str):
        """
        Extract a single page of Weibo feeds for a user.

        Args:
            client (httpx.AsyncClient): HTTP client instance
            uid (int): The unique identifier of the Weibo user
            container_id (str): Container ID for the user's feed
            since_id (str): ID of the last feed for pagination

        Returns:
            PagedFeeds: Object containing feeds and next page's since_id
        """
        try:
            url=FEEDS_URL.format(userId=str(
                uid), containerId=container_id, sinceId=since_id)
            response=await client.get(url, headers=DEFAULT_HEADERS)
            data=response.json()

            new_since_id=data.get("data", {}).get(
                "cardlistInfo", {}).get("since_id", "")
            cards=data.get("data", {}).get("cards", [])
            feeds=list(map(lambda x: self._to_feed_item(
                x.get('mblog', {})), cards))

            return PagedFeeds(SinceId=new_since_id, Feeds=feeds)
        except (httpx.HTTPError, httpx.ConnectError):
            self.logger.error(
                f"Unable to extract feeds for uid '{str(uid)}'", exc_info=True)
            return PagedFeeds(SinceId="", Feeds=[])

    def _to_trending_item(self, item: dict) -> TrendingItem:
        """
        Convert raw hot search item data to HotSearchItem object.

        Args:
            item (dict): Raw hot search item data from Weibo API

        Returns:
            HotSearchItem: Formatted hot search item information
        """
        extr_values=re.findall(r'\d+', str(item.get('desc_extr')))
        trending=int(extr_values[0]) if extr_values else 0
        return TrendingItem(
            id=item['id'],
            trending=trending,
            description=item['desc'],
            url=item.get('scheme', '')
        )

    def _to_feed_item(self, mblog: dict) -> FeedItem:
        """
        Convert a raw mblog data to FeedItem object.

        Args:
            mblog (dict): Raw mblog data from Weibo API

        Returns:
            FeedItem: Formatted feed item information
        """
        pics=[pic for pic in mblog.get(
            'pics', []) if 'url' in pic] if mblog.get('pics') else []
        pics=[{'thumbnail': pic['url'], 'large': pic['large']['url']}
            for pic in pics] if pics else []
        if not pics and mblog.get('pic_infos'):
            pics = [
                {
                    'thumbnail': info.get('thumbnail', {}).get('url', ''),
                    'large': info.get('largest', info.get('large', {})).get('url', ''),
                }
                for info in mblog['pic_infos'].values()
            ]

        videos={}
        page_info=mblog.get('page_info')
        if page_info and page_info.get('type') == 'video':
            if 'media_info' in page_info:
                videos['stream_url']=page_info['media_info'].get(
                    'stream_url', '')
                videos['stream_url_hd']=page_info['media_info'].get(
                    'stream_url_hd', '')
            elif 'urls' in page_info:
                videos['mp4_720p_mp4']=page_info['urls'].get(
                    'mp4_720p_mp4', '')
                videos['mp4_hd_mp4']=page_info['urls'].get('mp4_hd_mp4', '')
                videos['mp4_ld_mp4']=page_info['urls'].get('mp4_ld_mp4', '')

        user=self._to_user_profile(
            mblog.get('user', {})) if mblog.get('user') else {}
        return FeedItem(
            id=mblog.get('id'),
            text=mblog.get('text') or mblog.get('text_raw', ''),
            source=mblog.get('source', ''),
            created_at=mblog.get('created_at', ''),
            user=user,
            comments_count=mblog.get('comments_count', 0),
            attitudes_count=mblog.get('attitudes_count', 0),
            reposts_count=mblog.get('reposts_count', 0),
            raw_text=mblog.get('raw_text', ''),
            region_name=mblog.get('region_name', ''),
            pics=pics,
            videos=videos if videos else {}
        )

    def _to_user_profile(self, user: dict) -> UserProfile:
        """
        Convert raw user data to UserProfile object.

        Args:
            user (dict): Raw user data from Weibo API

        Returns:
            UserProfile: Formatted user profile information
        """
        return UserProfile(
            id=user['id'],
            screen_name=user.get('screen_name', ''),
            profile_image_url=user.get('profile_image_url', user.get('avatar_large', '')),
            profile_url=user.get('profile_url', f"/u/{user['id']}"),
            description=user.get('description', ''),
            follow_count=user.get('follow_count', 0),
            followers_count=user.get('followers_count', ''),
            avatar_hd=user.get('avatar_hd', ''),
            verified=user.get('verified', False),
            verified_reason=user.get('verified_reason', ''),
            gender=user.get('gender', '')
        )

    def _to_topic_item(self, item: dict) -> dict:
        """
        Convert raw topic data to a formatted dictionary.

        Args:
            item (dict): Raw topic data from Weibo API

        Returns:
            dict: Formatted topic information
        """
        return {
            'title': item['title_sub'],
            'desc1': item.get('desc1', ''),
            'desc2': item.get('desc2', ''),
            'url': item.get('scheme', '')
        }

    def _to_comment_item(self, item: dict) -> CommentItem:
        """
        Convert raw comment data to CommentItem object.

        Args:
            item (dict): Raw comment data from Weibo API

        Returns:
            CommentItem: Formatted comment information
        """
        return CommentItem(
            id=item.get('id'),
            text=item.get('text'),
            created_at=item.get('created_at'),
            user=self._to_user_profile(item.get('user', {})),
            source=item.get('source', ''),
            reply_id=item.get('reply_id', None),
            reply_text=item.get('reply_text', ''),
        )
