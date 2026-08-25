"""Convert Weibo HTML fragments into readable plain text."""
from html.parser import HTMLParser
import re

_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_EXPAND_LINK = re.compile(r"/status/\d+")
_CHROME_TEXT = frozenset({"全文", "展开"})


class _WeiboHTMLToText(HTMLParser):
    """Keep visible text, emoji alt labels, and line breaks; drop chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_tags: list[str] = []

    @property
    def _skipping(self) -> bool:
        return bool(self._skip_tags)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {key.lower(): (value or "") for key, value in attrs}
        classes = attrs_d.get("class", "")
        if tag == "br":
            self.parts.append("\n")
            return
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")
        if tag == "img":
            if alt := attrs_d.get("alt", "").strip():
                self.parts.append(alt)
            return
        if tag == "a" and _EXPAND_LINK.search(attrs_d.get("href", "")):
            self._skip_tags.append(tag)
            return
        if tag == "span" and "expand" in classes.split():
            self._skip_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tags and self._skip_tags[-1] == tag:
            self._skip_tags.pop()
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipping or data.strip() in _CHROME_TEXT:
            return
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._skipping:
            super().handle_entityref(name)

    def handle_charref(self, name: str) -> None:
        if not self._skipping:
            super().handle_charref(name)


def html_to_text(value: str | None) -> str:
    """Extract plain text from a Weibo HTML snippet.

    Already-plain strings are returned unchanged. ``<br>`` becomes a newline,
    emoji ``<img alt>`` keeps the alt label, topic/mention/link inner text is
    preserved, and ``全文`` expand links are dropped.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if not value or not _HTML_TAG.search(value):
        return value

    parser = _WeiboHTMLToText()
    parser.feed(value)
    parser.close()
    return _normalize_text("".join(parser.parts))


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            blank_run = 0
            cleaned.append(line)
            continue
        blank_run += 1
        if blank_run <= 1:
            cleaned.append("")
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)
