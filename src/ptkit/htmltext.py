"""Plain-text rendering of Packet Tracer instruction pages (HTML).

Kept free of GUI imports so the CLI can use it too.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser


class _HtmlToText(HTMLParser):
    """Instruction pages are stored as HTML; render them as readable text."""

    BLOCK = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n  • ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)


def strip_html_comments(markup: str) -> str:
    """Remove comments the way browsers (and Packet Tracer) do: each runs
    from ``<!--`` to the first ``-->``; an unterminated one runs to the end.

    Done before HTMLParser sees the text because Python's parser also ends a
    comment at ``-- >`` (with whitespace). NetAcad banners contain
    "(<!-- and -- >)", which made the rest of the banner leak into the text.
    """
    return _HTML_COMMENT_RE.sub("", markup)


def html_to_text(markup: str) -> str:
    if "<" not in markup:
        return html.unescape(markup).strip()
    p = _HtmlToText()
    p.feed(strip_html_comments(markup))
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
