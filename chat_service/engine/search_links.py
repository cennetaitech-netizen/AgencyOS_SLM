"""Web search links for questions the assistant doesn't answer.

When a question is outside media campaigns and AgencyOS (e.g. "Who is the Prime Minister of ...?"), the model
refuses and doesn't answer it. The engine then adds these links so the user can look it up themselves. They're
built here, not by the model, so they're always well-formed and never point anywhere unexpected.
"""

from __future__ import annotations

from urllib.parse import quote_plus

_ENGINES = (
    ("Google", "https://www.google.com/search?q="),
    ("Bing", "https://www.bing.com/search?q="),
    ("DuckDuckGo", "https://duckduckgo.com/?q="),
    ("Wikipedia", "https://en.wikipedia.org/w/index.php?search="),
)


def search_links(question: str, max_chars: int = 300) -> str:
    """Markdown list of search links for ``question``, starting with a blank line (appended to the answer)."""
    q = quote_plus(" ".join(question.split())[:max_chars])
    if not q:
        return ""
    items = "\n".join(f"- [Search on {name}]({base}{q})" for name, base in _ENGINES)
    return f"\n\nYou can look this up yourself:\n{items}"
