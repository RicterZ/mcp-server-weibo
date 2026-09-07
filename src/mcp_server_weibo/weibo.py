import asyncio
from http.cookies import SimpleCookie
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import qrcode

from mcp_server_weibo.consts import DEFAULT_HEADERS, PROFILE_URL, FEEDS_URL, SEARCH_URL, COMMENTS_URL
from mcp_server_weibo.html_text import html_to_text
from mcp_server_weibo.schemas import PagedFeeds, TrendingItem, FeedItem, UserProfile, UserRef, CommentItem


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


class WeiboError(RuntimeError):
    """A user-facing error returned by the Weibo API integration."""


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
        self._uses_cookie_env = False
        self._cookie_file_signature: tuple[int, int, int, int] | None = None
        self.cookies = self._load_cookies()

    @staticmethod
    def _response_json(response: httpx.Response) -> dict:
        """Parse API JSON and turn login redirects into a clear error."""
        location = response.headers.get("location", "")
        if response.is_redirect or response.status_code in (401, 403) or "passport.weibo.com" in location:
            raise WeiboError(
                "微博 Cookie 已失效或未登录，请重新登录：`mcp-server-weibo login`"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WeiboError(
                f"微博接口返回了非 JSON 响应（HTTP {response.status_code}），"
                "可能是 Cookie 失效或触发风控，请重新登录后重试"
            ) from exc

    @staticmethod
    def parse_cookie(cookie_header: str) -> dict[str, str]:
        parsed = SimpleCookie()
        parsed.load(cookie_header)
        return {name: morsel.value for name, morsel in parsed.items()}

    def _load_cookies(self) -> dict[str, str] | None:
        if cookie_header := os.environ.get(self.COOKIE_ENV):
            cookies = self.parse_cookie(cookie_header)
            if cookies:
                self._uses_cookie_env = True
                return cookies
        signature = self._get_cookie_file_signature()
        if signature is None:
            return None
        try:
            data = json.loads(self.cookie_file.read_text())
            cookies = data.get("cookies", data)
            if not isinstance(cookies, dict):
                raise ValueError("Cookie file must contain a JSON object")
            if self._get_cookie_file_signature() == signature:
                self._cookie_file_signature = signature
            return cookies or None
        except (OSError, json.JSONDecodeError, ValueError):
            self.logger.warning("Unable to load Weibo cookies from %s", self.cookie_file)
            return None

    def _get_cookie_file_signature(self) -> tuple[int, int, int, int] | None:
        try:
            stat = self.cookie_file.stat()
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            return None
        except OSError:
            self.logger.warning("Unable to stat Weibo cookies at %s", self.cookie_file)
            return None

    def _reload_cookies_if_changed(self) -> None:
        """Reload file-backed cookies after another process updates them."""
        if self._uses_cookie_env:
            return

        signature = self._get_cookie_file_signature()
        if signature == self._cookie_file_signature:
            return
        if signature is None:
            if self._cookie_file_signature is not None:
                self.cookies = None
                self._cookie_file_signature = None
            return

        try:
            data = json.loads(self.cookie_file.read_text())
            cookies = data.get("cookies", data)
            if not isinstance(cookies, dict):
                raise ValueError("Cookie file must contain a JSON object")
        except (OSError, json.JSONDecodeError, ValueError):
            self.logger.warning(
                "Unable to reload Weibo cookies from %s; keeping the current session",
                self.cookie_file,
            )
            return

        # If the file changed while it was being read, retry on the next request.
        if self._get_cookie_file_signature() != signature:
            return
        self.cookies = cookies or None
        self._cookie_file_signature = signature

    def save_cookies(self, cookies: dict[str, str]) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"cookies": cookies}, ensure_ascii=False)
        fd, temporary_path = tempfile.mkstemp(
            dir=self.cookie_file.parent,
            prefix=f".{self.cookie_file.name}.",
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as file:
                fd = -1
                file.write(content)
            os.replace(temporary_path, self.cookie_file)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        self.cookies = cookies
        self._cookie_file_signature = self._get_cookie_file_signature()

    async def _validate_cookies(self, cookies: dict) -> bool:
        try:
            async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, trust_env=False) as client:
                response = await client.get("https://m.weibo.cn/api/config", headers=DEFAULT_HEADERS)
                response.raise_for_status()
                data = response.json().get("data", {})
                if data.get("st"):
                    cookies["XSRF-TOKEN"] = data["st"]
                return bool(data.get("login") and data.get("uid"))
        except (httpx.HTTPError, ValueError):
            return False

    async def _ensure_cookies(self, retry: bool = False) -> dict:
        self._reload_cookies_if_changed()
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
        self._reload_cookies_if_changed()
        if not self.cookies:
            return {"login": False, "uid": None}
        try:
            async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, trust_env=False) as client:
                response = await client.get("https://m.weibo.cn/api/config", headers=DEFAULT_HEADERS)
                response.raise_for_status()
                data = response.json().get("data", {})
                if data.get("st"):
                    self.cookies["XSRF-TOKEN"] = data["st"]
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
                payload = self._response_json(response)
                retcode = payload.get("retcode")
                if retcode == 20000000:
                    data = payload.get("data", {})
                    if alt := data.get("alt"):
                        response = await client.get(
                            "/sso/v2/login",
                            params={"entry": "miniblog", "alt": alt, "returntype": "META"},
                        )
                        response.raise_for_status()
                    elif cross_url := data.get("url"):
                        await client.get(cross_url)
                    cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
                    if not await self._validate_cookies(cookies):
                        names = ", ".join(sorted(cookies)) or "none"
                        raise RuntimeError(
                            f"QR scan completed, but Weibo did not create a valid session (cookie names: {names})"
                        )
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

    async def get_home_timeline(self, limit: int = 20, page: int = 1) -> list[FeedItem]:
        """Get the logged-in user's following timeline."""
        await self._require_authenticated()
        async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, trust_env=False) as client:
            max_id = "0"
            for current_page in range(1, max(page, 1) + 1):
                params = {
                    "list_id": 0,
                    "refresh": 4,
                    "since_id": 0,
                    "count": min(max(limit, 1), 50),
                }
                if max_id != "0":
                    params["max_id"] = max_id
                response = await client.get(
                    "https://weibo.com/ajax/feed/friendstimeline",
                    params=params,
                    headers={**WEB_HEADERS, "X-Requested-With": "XMLHttpRequest"},
                )
                response.raise_for_status()
                payload = self._response_json(response)
                if payload.get("ok") != 1:
                    raise RuntimeError(payload.get("message") or payload.get("msg") or "Unable to fetch Weibo home timeline")
                statuses = payload.get("statuses") or []
                if current_page == max(page, 1):
                    return await self._to_feed_items(client, [
                        status for status in statuses[:limit]
                        if status.get("id") or status.get("idstr")
                    ])
                max_id = str(payload.get("max_id_str") or payload.get("max_id") or "0")
                if not statuses or max_id == "0":
                    return []
        return []

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
            data = self._response_json(response)
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
                result=self._response_json(response)
                return self._to_user_profile(result["data"]["userInfo"])
            except httpx.HTTPError as exc:
                self.logger.error(
                    f"Unable to extract profile for uid '{str(uid)}'", exc_info=True)
                raise WeiboError(f"获取微博用户资料失败（uid={uid}）：{exc}") from exc

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
                result=self._response_json(response)
                cards=list(
                    filter(lambda x: x['card_type'] == 9, result["data"]["cards"]))
                return await self._to_feed_items(
                    client, [item['mblog'] for item in cards[:limit]]
                )
            except httpx.HTTPError as exc:
                self.logger.error(
                    f"Unable to extract hot feeds for uid '{str(uid)}'", exc_info=True)
                raise WeiboError(f"获取微博热门微博失败（uid={uid}）：{exc}") from exc

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
                result=self._response_json(response)
                cards=result["data"]["cards"]
                if len(cards) < 2:
                    return []
                else:
                    cardGroup=cards[1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError as exc:
                self.logger.error(
                    f"Unable to search users for keyword '{keyword}'", exc_info=True)
                raise WeiboError(f"搜索微博用户失败（关键词={keyword}）：{exc}") from exc

    
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
                data=self._response_json(response)
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
        except httpx.HTTPError as exc:
            self.logger.error(
                'Unable to fetch Weibo hot search list', exc_info=True)
            raise WeiboError(f"获取微博热搜失败：{exc}") from exc

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
                    data=self._response_json(response)

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

                    mblogs = [card['mblog'] for card in content_cards if card.get('mblog')]
                    results.extend(await self._to_feed_items(client, mblogs[:limit - len(results)]))

                current_page += 1
                cardlist_info=data.get('data', {}).get('cardlistInfo', {})
                if not cardlist_info.get('page') or str(cardlist_info.get('page')) == '1':
                    break
            return results[:limit]
        except httpx.HTTPError as exc:
            self.logger.error(
                f"Unable to search Weibo content for keyword '{keyword}'", exc_info=True)
            raise WeiboError(f"搜索微博内容失败（关键词={keyword}）：{exc}") from exc

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
                result=self._response_json(response)
                cards=result.get("data", {}).get("cards", [])
                for card in cards:
                    card_group=card.get('card_group')
                    if card_group:
                        return [self._to_topic_item(item) for item in card_group][:limit]
                return []
            except (httpx.HTTPError, httpx.ConnectError, KeyError) as exc:
                self.logger.error(
                    f"Unable to search topics for keyword '{keyword}'", exc_info=True)
                raise WeiboError(f"搜索微博话题失败（关键词={keyword}）：{exc}") from exc

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
                data=self._response_json(response)
                comments=data.get('data', {}).get('data', [])
                return [self._to_comment_item(comment) for comment in comments]
        except httpx.HTTPError as exc:
            self.logger.error(
                f"Unable to fetch comments for feed_id '{feed_id}'", exc_info=True)
            raise WeiboError(f"获取微博评论失败（feed_id={feed_id}）：{exc}") from exc

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
                result=self._response_json(response)
                cards=result["data"]["cards"]
                if len(cards) < 1:
                    return []
                else:
                    cardGroup=cards[-1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError as exc:
                self.logger.error(
                    f"Unable to get followers for uid '{str(uid)}'", exc_info=True)
                raise WeiboError(f"获取微博粉丝失败（uid={uid}）：{exc}") from exc

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
                result=self._response_json(response)
                cards=result["data"]["cards"]
                if len(cards) < 1:
                    return []
                else:
                    cardGroup=cards[-1]['card_group']
                    return [self._to_user_profile(item['user']) for item in cardGroup][:limit]
            except httpx.HTTPError as exc:
                self.logger.error(
                    f"Unable to get fans for uid '{str(uid)}'", exc_info=True)
                raise WeiboError(f"获取微博关注失败（uid={uid}）：{exc}") from exc

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
            data=self._response_json(response)
            tabs_info=data.get("data", {}).get(
                "tabsInfo", {}).get("tabs", [])
            for tab in tabs_info:
                if tab.get("tabKey") == "weibo":
                    return tab.get("containerid")
        except httpx.HTTPError as exc:
            self.logger.error(
                f"Unable to extract containerId for uid '{str(uid)}'", exc_info=True)
            raise WeiboError(f"获取微博用户动态容器失败（uid={uid}）：{exc}") from exc

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
            data=self._response_json(response)

            new_since_id=data.get("data", {}).get(
                "cardlistInfo", {}).get("since_id", "")
            cards=data.get("data", {}).get("cards", [])
            mblogs = [card.get('mblog') for card in cards]
            feeds = await self._to_feed_items(client, [mblog for mblog in mblogs
                if mblog and (mblog.get('id') or mblog.get('idstr'))])

            return PagedFeeds(SinceId=new_since_id, Feeds=feeds)
        except (httpx.HTTPError, httpx.ConnectError) as exc:
            self.logger.error(
                f"Unable to extract feeds for uid '{str(uid)}'", exc_info=True)
            raise WeiboError(f"获取微博动态失败（uid={uid}）：{exc}") from exc

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
        )

    async def _to_feed_items(self, client: httpx.AsyncClient, mblogs: list[dict]) -> list[FeedItem]:
        """Expand long posts with bounded concurrency, preserving result order."""
        semaphore = asyncio.Semaphore(4)

        async def convert(mblog: dict) -> FeedItem:
            async with semaphore:
                return await self._expand_feed(client, mblog)

        return list(await asyncio.gather(*(convert(mblog) for mblog in mblogs)))

    async def _expand_feed(self, client: httpx.AsyncClient, mblog: dict) -> FeedItem:
        feed = self._to_feed_item(mblog)
        embedded = mblog.get('longText') or {}
        full_text = embedded.get('longTextContent') if isinstance(embedded, dict) else None
        if isinstance(full_text, str) and full_text.strip():
            feed.text = html_to_text(full_text)
            return feed

        text = mblog.get('text') or ''
        if not (mblog.get('isLongText') or re.search(r'>\s*(?:全文|展开)\s*</(?:a|span)>', text)):
            return feed

        feed.text_truncated = True
        # Mobile HTML preserves the same topic/link labels as search results.
        # The desktop endpoint is a fallback when mobile expansion is unavailable.
        for url, headers in (
            ('https://m.weibo.cn/statuses/extend', DEFAULT_HEADERS),
            ('https://weibo.com/ajax/statuses/longtext', WEB_HEADERS),
        ):
            try:
                response = await client.get(url, params={'id': str(feed.id)}, headers=headers, timeout=10)
                response.raise_for_status()
                payload = self._response_json(response)
                if not isinstance(payload, dict) or payload.get('ok') != 1:
                    continue
                data = payload.get('data')
                if not isinstance(data, dict) or data.get('ok', 1) != 1:
                    continue
                raw_text = data.get('longTextContent_raw')
                full_text = data.get('longTextContent')
                if isinstance(raw_text, str) and raw_text.strip():
                    feed.text = raw_text
                elif isinstance(full_text, str) and full_text.strip():
                    feed.text = html_to_text(full_text)
                else:
                    continue
                feed.text_truncated = None
                return feed
            except (httpx.HTTPError, WeiboError):
                continue

        self.logger.warning('Unable to expand Weibo post %s; returning marked excerpt', feed.id)
        return feed

    def _to_feed_item(self, mblog: dict) -> FeedItem:
        """
        Convert a raw mblog data to FeedItem object.

        Args:
            mblog (dict): Raw mblog data from Weibo API

        Returns:
            FeedItem: Formatted feed item information
        """
        pics=self._to_pic_urls(mblog)
        page_info=mblog.get('page_info') or {}
        video_url=''
        if page_info.get('type') == 'video':
            video_url=page_info.get('page_url') or page_info.get('url_ori') or ''

        user=self._to_user_ref(mblog['user']) if mblog.get('user') else None
        return FeedItem(
            id=mblog.get('id') or mblog.get('idstr'),
            text=html_to_text(mblog.get('text') or mblog.get('text_raw', '')),
            created_at=mblog.get('created_at', ''),
            user=user,
            pics=pics,
            video_url=video_url,
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
            description=user.get('description', ''),
            followers_count=str(user.get('followers_count') or ''),
            verified=bool(user.get('verified', False)),
        )

    def _to_user_ref(self, user: dict) -> UserRef:
        return UserRef(
            id=user['id'],
            screen_name=user.get('screen_name', ''),
            verified=bool(user.get('verified', False)),
        )

    @staticmethod
    def _to_pic_urls(mblog: dict) -> list[str]:
        urls=[]
        for pic in mblog.get('pics') or []:
            if isinstance(pic.get('large'), dict) and pic['large'].get('url'):
                urls.append(pic['large']['url'])
            elif pic.get('url'):
                urls.append(pic['url'])
        if urls:
            return urls
        for info in (mblog.get('pic_infos') or {}).values():
            url=(info.get('largest') or info.get('large') or {}).get('url')
            if url:
                urls.append(url)
        return urls

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
        user=item.get('user')
        return CommentItem(
            id=item.get('id'),
            text=html_to_text(item.get('text')),
            created_at=item.get('created_at') or '',
            user=self._to_user_ref(user) if user else None,
            source=item.get('source', ''),
            reply_id=item.get('reply_id', None),
            reply_text=html_to_text(item.get('reply_text', '')),
        )
