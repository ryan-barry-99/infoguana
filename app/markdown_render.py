"""Server-side markdown rendering for note content.

Uses markdown-it-py in CommonMark mode with GFM-ish extras (tables,
strikethrough, linkify). `html: False` makes the parser escape any raw HTML in
the source, so the output contains only tags we generated — no sanitizer pass
needed.
"""
from markdown_it import MarkdownIt
from markupsafe import Markup

_md = (
    MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": True})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    return Markup(_md.render(text))
