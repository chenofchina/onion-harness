"""Text normalisation for full-text search.

FTS5's built-in tokenizers treat a run of CJK characters as a single token, so
searching for 偏好 inside 用户偏好使用 fails. Since the application has no way to
add a Chinese tokenizer to the SQLite build, memory text is indexed twice: once
verbatim, and once through this module, which emits CJK bigrams plus latin words.

The bigram form is what search actually matches against. It is crude but it makes
substring queries work without an external tokenizer extension.
"""

from __future__ import annotations

import re
from typing import List

# Latin words, numbers, and identifier-ish tokens (foo_bar, foo.bar, run_python).
_LATIN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
# Runs of CJK characters (Chinese/Japanese/Korean unified + kana + hangul).
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+"
)


def tokenize(text: str) -> List[str]:
    """Split text into searchable tokens: latin words + CJK bigrams.

    Singletons are kept as-is so a one-character query still matches, and bigrams
    mean a two-character query finds its substring anywhere in a longer run.
    """
    if not text:
        return []

    tokens: List[str] = []
    for match in _LATIN_RE.finditer(text):
        tokens.append(match.group(0).lower())

    for match in _CJK_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))

    return tokens


def index_text(text: str) -> str:
    """The string stored in the FTS column: tokens separated by spaces."""
    return " ".join(tokenize(text))


def query_terms(query: str) -> List[str]:
    """Tokens to search for, longest first so specific terms rank higher."""
    tokens = tokenize(query)
    # A latin word is more specific than a bigram; keep both, order stable.
    return sorted(set(tokens), key=lambda token: (-len(token), token))


def build_match_expression(query: str) -> str:
    """An FTS5 MATCH expression: every term OR'd, quoted to avoid operators.

    OR rather than AND because recall matters more than precision here: a memory
    that shares one distinctive term is worth returning, and the ranker sorts it.
    """
    terms = query_terms(query)
    if not terms:
        return ""
    escaped = [f'"{term}"' for term in terms if '"' not in term]
    return " OR ".join(escaped)


__all__ = ["tokenize", "index_text", "query_terms", "build_match_expression"]