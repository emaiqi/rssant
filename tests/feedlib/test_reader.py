import asyncio
import os.path
import json
import traceback
import logging
from urllib.parse import urlparse, parse_qsl
from typing import Type
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.datastructures import Headers as HTTPHeaders
from werkzeug import Response as WerkzeugResponse, Request as WerkzeugRequest

from rssant_config import CONFIG
from rssant_feedlib.reader import FeedReader, FeedResponseStatus
from rssant_feedlib.async_reader import AsyncFeedReader


LOG = logging.getLogger(__name__)


class SyncAsyncFeedReader:
    def __init__(self, *args, **kwargs):
        self._loop = asyncio.get_event_loop()
        self._loop_run = self._loop.run_until_complete
        self._reader = AsyncFeedReader(*args, **kwargs)

    @property
    def has_rss_proxy(self):
        return self._reader.has_rss_proxy

    def read(self, *args, **kwargs):
        return self._loop_run(self._reader.read(*args, **kwargs))

    def __enter__(self):
        self._loop_run(self._reader.__aenter__())
        return self

    def __exit__(self, *args):
        return self._loop_run(self._reader.__aexit__(*args))

    def close(self):
        return self._loop_run(self._reader.close())


@pytest.mark.xfail(run=False, reason='depends on test network')
@pytest.mark.parametrize('url', [
    'https://www.reddit.com/r/Python.rss',
    'https://www.youtube.com/feeds/videos.xml?channel_id=UCBcRF18a7Qf58cCRy5xuWwQ',
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_by_proxy(reader_class: Type[FeedReader], url):
    with reader_class(
        rss_proxy_url=CONFIG.rss_proxy_url,
        rss_proxy_token=CONFIG.rss_proxy_token,
    ) as reader:
        response = reader.read(url, use_proxy=True)
    assert response.ok
    assert response.url == url


@pytest.mark.xfail(run=False, reason='depends on test network')
@pytest.mark.parametrize('url', [
    'https://www.ruanyifeng.com/blog/atom.xml',
    'https://blog.guyskk.com/feed.xml',
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_by_real(reader_class: Type[FeedReader], url):
    with reader_class() as reader:
        response = reader.read(url)
    assert response.ok
    assert response.url == url


@pytest.mark.parametrize('status', [
    200, 201, 301, 302, 400, 403, 404, 500, 502, 600,
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_status(reader_class: Type[FeedReader], httpserver: HTTPServer, status: int):
    options = dict(allow_non_webpage=True, allow_private_address=True)
    local_resp = WerkzeugResponse(str(status), status=status)
    httpserver.expect_request("/status").respond_with_response(local_resp)
    url = httpserver.url_for("/status")
    with reader_class(**options) as reader:
        response = reader.read(url)
        assert response.status == status
        assert response.content == str(status).encode()


@pytest.mark.parametrize('mime_type', [
    'image/png', 'text/csv',
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_non_webpage(reader_class: Type[FeedReader], httpserver: HTTPServer, mime_type: str):
    options = dict(allow_private_address=True)
    local_resp = WerkzeugResponse(b'xxxxxxxx', mimetype=mime_type)
    httpserver.expect_request("/non-webpage").respond_with_response(local_resp)
    url = httpserver.url_for("/non-webpage")
    with reader_class(**options) as reader:
        response = reader.read(url)
        assert response.status == FeedResponseStatus.CONTENT_TYPE_NOT_SUPPORT_ERROR
        assert not response.content


@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_private_addres(reader_class: Type[FeedReader], httpserver: HTTPServer):
    httpserver.expect_request("/private-address").respond_with_json(0)
    url = httpserver.url_for("/private-address")
    with reader_class() as reader:
        response = reader.read(url)
        assert response.status == FeedResponseStatus.PRIVATE_ADDRESS_ERROR
        assert not response.content


_data_dir = Path(__file__).parent / 'testdata'


def _collect_testdata_filepaths():
    cases = []
    for filepath in (_data_dir / 'encoding/chardet').glob("*"):
        cases.append(filepath.absolute())
    for filepath in (_data_dir / 'parser').glob("*/*"):
        cases.append(filepath.absolute())
    cases = [os.path.relpath(x, _data_dir) for x in cases]
    return cases


def _collect_header_cases():
    return [
        "application/json;charset=utf-8",
        "application/atom+xml; charset='us-ascii'",
        "application/atom+xml; charset='gb2312'",
        "application/atom+xml;CHARSET=GBK",
        None,
    ]


@pytest.mark.parametrize('filepath', _collect_testdata_filepaths())
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_testdata(reader_class: Type[FeedReader], httpserver: HTTPServer, filepath: str):
    filepath = _data_dir / filepath
    content = filepath.read_bytes()
    urls = []
    for i, x in enumerate(_collect_header_cases()):
        local_resp = WerkzeugResponse(content, content_type=x)
        httpserver.expect_request(f"/testdata/{i}").respond_with_response(local_resp)
        urls.append(httpserver.url_for(f"/testdata/{i}"))
    options = dict(allow_private_address=True)
    with reader_class(**options) as reader:
        for url in urls:
            response = reader.read(url)
            assert response.ok
            assert response.content == content
            assert response.encoding
            assert response.feed_type


_RSS_PROXY_TOKEN = 'TEST_RSS_PROXY_TOKEN'


def _parse_query(qs) -> dict:
    query = {}
    for k, v in parse_qsl(qs):
        query[k] = v
    return query


def rss_proxy_handler(request: WerkzeugRequest) -> WerkzeugResponse:
    try:
        data = json.loads(request.data.decode('utf-8'))
        assert data['token'] == _RSS_PROXY_TOKEN
        assert data.get('method') in (None, 'POST')
        url = urlparse(data['url'])
        query = _parse_query(url.query)
        assert url.path == '/not-proxy'
        assert HTTPHeaders(data['headers'])['user-agent']
    except Exception as ex:
        LOG.warning(ex, exc_info=ex)
        msg = traceback.format_exception_only(type(ex), ex)
        return WerkzeugResponse(msg, status=400)
    status = query.get('status')
    error = query.get('error')
    if error:
        if error == 'ERROR':
            headers = {'x-rss-proxy-status': 'ERROR'}
            return WerkzeugResponse(str(status), status=200, headers=headers)
        else:
            return WerkzeugResponse(str(status), status=int(error))
    else:
        status = int(status) if status else 200
        headers = {'x-rss-proxy-status': status}
        return WerkzeugResponse(str(status), status=200, headers=headers)


def _setup_rss_proxy(httpserver: HTTPServer):
    httpserver.expect_request("/rss-proxy", method='POST')\
        .respond_with_handler(rss_proxy_handler)
    httpserver.expect_request("/not-proxy").respond_with_data('ERROR', status=500)
    proxy_url = httpserver.url_for('/rss-proxy')
    url = httpserver.url_for('/not-proxy')
    options = dict(
        allow_private_address=True,
        rss_proxy_url=proxy_url,
        rss_proxy_token=_RSS_PROXY_TOKEN,
    )
    return options, url


@pytest.mark.parametrize('status', [
    200, 201, 301, 302, 400, 403, 404, 500, 502, 600,
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_rss_proxy(reader_class: Type[FeedReader], httpserver: HTTPServer, status: int):
    options, url = _setup_rss_proxy(httpserver)
    with reader_class(**options) as reader:
        response = reader.read(url + f'?status={status}', use_proxy=True)
        httpserver.check_assertions()
        assert response.status == status


@pytest.mark.parametrize('error', [
    301, 302, 400, 403, 404, 500, 502, 'ERROR',
])
@pytest.mark.parametrize('reader_class', [FeedReader, SyncAsyncFeedReader])
def test_read_rss_proxy_error(reader_class: Type[FeedReader], httpserver: HTTPServer, error):
    options, url = _setup_rss_proxy(httpserver)
    with reader_class(**options) as reader:
        response = reader.read(url + f'?error={error}', use_proxy=True)
        httpserver.check_assertions()
        assert response.status == FeedResponseStatus.RSS_PROXY_ERROR
