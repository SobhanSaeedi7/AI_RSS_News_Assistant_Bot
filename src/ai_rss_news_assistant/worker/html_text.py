"""HTML-to-text extraction for article content.

Uses only the standard library (html.parser) to convert HTML article bodies
into plain text suitable for summarization and Q&A ground truth.

Design notes:
- Strips script/style/noscript/iframe/svg/canvas/template blocks entirely.
- Inserts newlines for block-level elements (p, div, headings, list items,
  tables, pre, blockquote, etc.) to preserve rough document structure.
- Strips leading/trailing whitespace from each text chunk and collapses
  blank lines, preserving paragraph separation.
- Does not attempt to resolve relative URLs or extract images/links.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _HTMLToText(HTMLParser):
    """Convert HTML to plain text using only the standard library."""

    # Block-level elements that should introduce paragraph breaks.
    _BLOCK_LEVEL = frozenset({
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "td", "th", "pre", "blockquote",
        "section", "article", "main", "footer", "header",
        "nav", "aside", "figure", "figurecaption", "table",
        "ul", "ol", "dl", "dt", "dd",
    })

    # Elements whose contents should be skipped entirely.
    _SKIP_TAGS = frozenset({
        "script", "style", "noscript", "iframe", "svg", "canvas",
        "template",
    })

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" or tag in self._BLOCK_LEVEL:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        """Return the extracted text with collapsed whitespace."""
        text = " ".join(self._parts)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)


def html_to_text(html: str) -> str:
    """Convert HTML to plain text.

    Strips script/style/iframe/svg/canvas/template blocks and extracts
    visible text with paragraph separation for block-level elements.

    Returns an empty string if the input is empty or contains no text.
    """
    if not html or not html.strip():
        return ""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()