import httpx
import pytest

from mcp_server_weibo import weibo
from mcp_server_weibo.weibo import WeiboCrawler


POST = {
    'id': '5340512228738781',
    'text': '虽然旁边写着“儿童用”，但在凉爽 ...<a href="/status/5340512228738781">全文</a>',
    'isLongText': True,
    'user': {'id': 6011443154, 'screen_name': '蔚蓝档案'},
    'pics': [{'url': 'https://example/picture.gif'}],
}
FULL_TEXT = '虽然旁边写着“儿童用”，但在凉爽面前，这些都无关紧要。<br />莲华（泳装）&amp; 紫（泳装）'


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', [
    ('search_content', {'keyword': '家具互动', 'limit': 1}),
    ('get_feeds', {'uid': 6011443154, 'limit': 1}),
    ('get_hot_feeds', {'uid': 6011443154, 'limit': 1}),
    ('get_home_timeline', {'limit': 1}),
])
async def test_feed_entrypoints_expand_long_text(monkeypatch, method, args):
    monkeypatch.setenv('WEIBO_COOKIE', 'SUB=test')
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == '/api/config':
            return httpx.Response(200, json={'data': {'login': True, 'uid': '1'}})
        if request.url.path == '/statuses/extend':
            assert request.url.params['id'] == POST['id']
            return httpx.Response(200, json={'ok': 1, 'data': {'longTextContent': FULL_TEXT}})
        if request.url.path == '/ajax/feed/friendstimeline':
            return httpx.Response(200, json={'ok': 1, 'statuses': [POST]})
        if request.url.path == '/api/container/getIndex':
            return httpx.Response(200, json={'data': {
                'tabsInfo': {'tabs': [{'tab_type': 'weibo', 'containerid': 'container'}]},
                'cardlistInfo': {},
                'cards': [{'card_type': 9, 'mblog': POST}],
            }})
        pytest.fail(f'Unexpected URL: {request.url}')

    real_client = httpx.AsyncClient
    monkeypatch.setattr(weibo.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    result = await getattr(WeiboCrawler(), method)(**args)
    assert len(result) == 1
    feed = result[0].model_dump()
    assert feed['text'] == '虽然旁边写着“儿童用”，但在凉爽面前，这些都无关紧要。\n莲华（泳装）& 紫（泳装）'
    assert feed['user']['id'] == 6011443154
    assert feed['pics'] == ['https://example/picture.gif']
    assert 'text_truncated' not in feed
    assert sum(r.url.path == '/statuses/extend' for r in requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'http', 'login', 'api', 'empty', 'malformed'])
async def test_failed_expansion_preserves_and_marks_excerpt(failure):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        if failure == 'http':
            return httpx.Response(503)
        if failure == 'login':
            return httpx.Response(200, text='<html>Login required</html>')
        payload = {
            'api': {'ok': 0, 'msg': 'unavailable'},
            'empty': {'ok': 1, 'data': {'longTextContent': ''}},
            'malformed': {'ok': 1, 'data': []},
        }[failure]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await WeiboCrawler()._expand_feed(client, POST)
    assert feed.text.endswith('但在凉爽 ...')
    assert feed.model_dump()['text_truncated'] is True
    assert feed.id == int(POST['id'])


@pytest.mark.asyncio
async def test_desktop_fallback_preserves_raw_text():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == '/statuses/extend':
            return httpx.Response(200, json={'ok': 0})
        return httpx.Response(200, json={'ok': 1, 'data': {
            'longTextContent_raw': '完整正文 & <literal>\n第二行',
            'longTextContent': '不应选用这个字段',
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await WeiboCrawler()._expand_feed(client, POST)
    assert feed.text == '完整正文 & <literal>\n第二行'
    assert 'text_truncated' not in feed.model_dump()
    assert requests == ['/statuses/extend', '/ajax/statuses/longtext']


@pytest.mark.asyncio
async def test_short_and_embedded_full_text_do_not_fetch():
    def handler(request):
        pytest.fail(f'Unnecessary expansion request: {request.url}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feeds = await WeiboCrawler()._to_feed_items(client, [
            {'id': 1, 'text': '原文自带省略号 ...'},
            {**POST, 'longText': {'longTextContent': FULL_TEXT}},
        ])
    assert [feed.id for feed in feeds] == [1, int(POST['id'])]
    assert feeds[0].text == '原文自带省略号 ...'
    assert '莲华（泳装）& 紫（泳装）' in feeds[1].text
    assert all('text_truncated' not in feed.model_dump() for feed in feeds)


@pytest.mark.asyncio
async def test_expand_link_without_long_text_flag():
    def handler(request):
        return httpx.Response(200, json={'ok': 1, 'data': {'longTextContent': FULL_TEXT}})

    post = {key: value for key, value in POST.items() if key != 'isLongText'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        feed = await WeiboCrawler()._expand_feed(client, post)
    assert '莲华（泳装）& 紫（泳装）' in feed.text
