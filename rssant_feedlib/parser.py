import logging
from typing import List

from validr import Invalid, T, mark_index

from rssant_common.validator import compiler

from .raw_parser import RawFeedResult, FeedParserError
from .feed_checksum import FeedChecksum
from rssant_api.helper import shorten
from .processor import (
    story_html_to_text, story_html_clean,
    story_has_mathjax, process_story_links, normlize_url, validate_url,
)


LOG = logging.getLogger(__name__)


FeedSchema = T.dict(
    version=T.str.maxlen(200),
    title=T.str.maxlen(200),
    url=T.url,
    home_url=T.url.invalid_to_default.optional,
    icon_url=T.url.invalid_to_default.optional,
    description=T.str.maxlen(300).optional,
    dt_updated=T.datetime.object.optional,
    author_name=T.str.maxlen(100).optional,
    author_url=T.url.invalid_to_default.optional,
    author_avatar_url=T.url.invalid_to_default.optional,
)


StorySchema = T.dict(
    ident=T.str.maxlen(200),
    title=T.str.maxlen(200),
    url=T.url.optional,
    content=T.str.optional,
    summary=T.str.maxlen(300).optional,
    has_mathjax=T.bool.optional,
    image_url=T.url.invalid_to_default.optional,
    dt_published=T.datetime.object.optional,
    dt_updated=T.datetime.object.optional,
    author_name=T.str.maxlen(100).optional,
    author_url=T.url.invalid_to_default.optional,
    author_avatar_url=T.url.invalid_to_default.optional,
)


validate_feed = compiler.compile(FeedSchema)
validate_story = compiler.compile(StorySchema)


class FeedResult:

    __slots__ = ('_feed', '_storys', '_checksum')

    def __init__(self, feed, storys, checksum):
        self._feed = feed
        self._storys = storys
        self._checksum = checksum

    def __repr__(self):
        return '<{} url={!r} version={!r} title={!r} has {} storys>'.format(
            type(self).__name__,
            self.feed['url'],
            self.feed['version'],
            self.feed['title'],
            len(self.storys),
        )

    @property
    def feed(self) -> FeedSchema:
        return self._feed

    @property
    def storys(self) -> List[StorySchema]:
        return self._storys

    @property
    def checksum(self) -> FeedChecksum:
        return self._checksum


class FeedParser:
    def __init__(self, checksum: FeedChecksum = None, validate: bool = True):
        if checksum is None:
            checksum = FeedChecksum()
        else:
            checksum = checksum.copy()
        self._checksum = checksum
        self._validate = validate

    def _parse_feed(self, feed: dict):
        url = feed['url']
        title = story_html_to_text(feed['title'])[:200]
        home_url = normlize_url(feed['home_url'], base_url=url)
        icon_url = normlize_url(feed['icon_url'], base_url=url)
        description = story_html_to_text(feed['description'])[:300]
        author_name = story_html_to_text(feed['author_name'])[:100]
        author_url = normlize_url(feed['author_url'], base_url=url)
        author_avatar_url = normlize_url(feed['author_avatar_url'], base_url=url)
        return dict(
            version=feed['version'],
            title=title,
            url=url,
            home_url=home_url,
            icon_url=icon_url,
            description=description,
            dt_updated=feed['dt_updated'],
            author_name=author_name,
            author_url=author_url,
            author_avatar_url=author_avatar_url,
        )

    def _process_content(self, content, link):
        content = story_html_clean(content)
        if len(content) >= 1024 * 1024:
            msg = 'too large story link=%r content length=%s, will only save plain text!'
            LOG.warning(msg, link, len(content))
            content = story_html_to_text(content)
        content = process_story_links(content, link)
        return content

    def _parse_story(self, story: dict, feed_url: str):
        ident = story['ident'][:200]
        title = story_html_to_text(story['title'])[:200]
        url = normlize_url(story['url'] or story['ident'], base_url=feed_url)
        try:
            valid_url = validate_url(url)
        except Invalid:
            valid_url = None
        base_url = valid_url or feed_url
        image_url = normlize_url(story['image_url'], base_url=base_url)
        author_name = story_html_to_text(story['author_name'])[:100]
        author_url = normlize_url(story['author_url'], base_url=base_url)
        author_avatar_url = normlize_url(story['author_avatar_url'], base_url=base_url)
        content = self._process_content(story['content'], link=base_url)
        summary = story_html_clean(story['summary'])
        summary = shorten(story_html_to_text(summary), width=300)
        has_mathjax = story_has_mathjax(content)
        return dict(
            ident=ident,
            title=title,
            url=valid_url,
            content=content,
            summary=summary,
            has_mathjax=has_mathjax,
            image_url=image_url,
            dt_published=story['dt_published'],
            dt_updated=story['dt_updated'],
            author_name=author_name,
            author_url=author_url,
            author_avatar_url=author_avatar_url,
        )

    def _validate_result(self, result: FeedResult) -> FeedResult:
        storys = []
        try:
            feed = validate_feed(result.feed)
            for i, s in enumerate(result.storys):
                with mark_index(i):
                    s = validate_story(s)
                    storys.append(s)
        except Invalid as ex:
            raise FeedParserError(str(ex)) from ex
        return FeedResult(feed, storys, checksum=result.checksum)

    def _check_update_storys(self, storys: list):
        update_storys = []
        for story in storys:
            ident = story['ident']
            content = story['content'] or ''
            if self._checksum.update(ident, content):
                update_storys.append(story)
        return update_storys

    def parse(self, raw: RawFeedResult) -> FeedResult:
        update_storys = self._check_update_storys(raw.storys)
        feed = self._parse_feed(raw.feed)
        feed_url = feed['url']
        storys = []
        for story in update_storys:
            story = self._parse_story(story, feed_url=feed_url)
            storys.append(story)
        result = FeedResult(feed, storys, checksum=self._checksum)
        if self._validate:
            result = self._validate_result(result)
        return result
