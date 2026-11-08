"""Hybrid output: Markdown interleaved with declarative ``<ui>`` blocks.

The model can emit ordinary Markdown and, where a plain sentence is not enough,
a ``<ui>`` block containing limited HTML with a single interaction primitive:

    <ui>
      <p>Flash the device?</p>
      <button onclick="sendAction('approve_flash')">确认刷机</button>
    </ui>

Three constraints shape this module, and they matter more than the parsing.

**The HTML is never executed.** It is *parsed* here, validated against a small
allowlist, and handed to the GUI as data. The GUI renders the allowlisted tags
itself. Nothing that arrives as a string ever reaches ``innerHTML`` or a browser
engine, so "the model wrote a script tag" is not a vulnerability - it is a
validation failure.

**Only one interaction form is understood.** ``onclick="sendAction('id')"`` is not
arbitrary JavaScript; it is a declarative instruction to send an action id back to
the agent. Any other handler is dropped. That is the difference between "the model
can build UI" and "the model can run code in the user's app".

**A malformed block must not lose the message.** If the model writes broken HTML,
the surrounding text still renders and the broken part degrades to a visible note.
Dropping the whole reply because of one bad tag would be a worse failure than the
tag.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

# -- limits ------------------------------------------------------------------
MAX_UI_BLOCKS = 12
MAX_HTML_CHARS = 20_000
MAX_ACTION_ID = 120

# Tags the renderer understands. Anything else is dropped (not escaped - dropped,
# so a rejected tag cannot smuggle attributes through a text node).
ALLOWED_TAGS = {
    "div", "span", "p", "br", "hr",
    "b", "strong", "i", "em", "u", "s", "small",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td",
    "code", "pre",
    "button", "label", "input",
    "details", "summary",
    "section", "header", "footer",
}

VOID_TAGS = {"br", "hr", "input"}

# Attributes whose presence is the value. HTMLParser reports these as None.
_BOOLEAN_ATTRS = {"checked", "disabled", "open", "readonly", "selected", "multiple"}

# Attributes kept per tag. Nothing global: an allowlist per tag is the only way
# to keep `style` and `onerror` off elements that would use them.
ALLOWED_ATTRS: Dict[str, set] = {
    "*": {"class"},
    "button": {"class", "data-action", "disabled", "type"},
    "input": {"class", "type", "name", "value", "placeholder", "disabled", "checked"},
    "td": {"class", "colspan", "rowspan"},
    "th": {"class", "colspan", "rowspan"},
    "details": {"class", "open"},
}

# `onclick="sendAction('id')"`, tolerant of quote style and spacing.
_ACTION_RE = re.compile(
    r"""^\s*sendAction\(\s*(['"])(?P<id>[^'"]{1,%d})\1\s*\)\s*$""" % MAX_ACTION_ID
)
_UI_OPEN_RE = re.compile(r"<ui\b[^>]*>", re.IGNORECASE)
_UI_CLOSE_RE = re.compile(r"</ui\s*>", re.IGNORECASE)


@dataclass
class UIAction:
    """An interaction the user can trigger."""

    id: str
    label: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass
class UINode:
    """One allowed element, already stripped of anything not allowlisted."""

    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["UINode"] = field(default_factory=list)
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tag": self.tag}
        if self.attrs:
            out["attrs"] = dict(self.attrs)
        if self.text:
            out["text"] = self.text
        if self.children:
            out["children"] = [child.to_dict() for child in self.children]
        return out


@dataclass
class OutputSegment:
    """One piece of a split reply: markdown text or a UI block."""

    # "markdown" | "ui" | "ui-error"
    kind: str
    text: str = ""
    nodes: List[UINode] = field(default_factory=list)
    actions: List[UIAction] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        if self.kind == "ui":
            return {
                "kind": "ui",
                "nodes": [node.to_dict() for node in self.nodes],
                "actions": [action.to_dict() for action in self.actions],
            }
        if self.kind == "ui-error":
            return {"kind": "ui-error", "error": self.error}
        return {"kind": "markdown", "text": self.text}


@dataclass
class RenderedOutput:
    """A whole reply, ready for the GUI."""

    segments: List[OutputSegment] = field(default_factory=list)
    actions: List[UIAction] = field(default_factory=list)

    @property
    def has_ui(self) -> bool:
        return any(segment.kind == "ui" for segment in self.segments)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segments": [segment.to_dict() for segment in self.segments],
            "actions": [action.to_dict() for action in self.actions],
            "has_ui": self.has_ui,
        }

    def plain_text(self) -> str:
        """Everything that is not markup, for history replay and logging."""
        parts = [segment.text for segment in self.segments if segment.kind == "markdown"]
        return "\n\n".join(part for part in parts if part).strip()


class _NodeBuilder(HTMLParser):
    """Builds an allowlisted node tree, recording action ids as it goes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = UINode(tag="root")
        self._stack: List[UINode] = [self.root]
        self.actions: List[UIAction] = []
        self.dropped: List[str] = []

    # -- tag handling ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            # Rejected tags are dropped flat, never pushed as containers. If a
            # rejected tag became a parent, everything after it - including valid
            # buttons - would nest inside it and be lost with it.
            self.dropped.append(tag)
            return

        node = UINode(tag=tag, attrs=self._clean_attrs(tag, dict(attrs)))
        self._stack[-1].children.append(node)

        action_id = self._action_from(attrs)
        if action_id:
            node.attrs["data-action"] = action_id
            label = ""
            # The visible label is the button's text, filled in when it closes.
            self.actions.append(UIAction(id=action_id, label=label))

        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            # The matching start tag was dropped, so this end tag is a no-op.
            return
        # Pop back to the matching real tag, so unbalanced markup cannot corrupt
        # the tree beyond the element that was wrong.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return
        # An unmatched end tag is ignored rather than fatal.

    def handle_data(self, data: str) -> None:
        if not data:
            return
        # A bare text run becomes its own node instead of accumulating on the
        # parent. Without this, `<p>Press <b>OK</b> now</p>` collapsed both text
        # runs onto the <p> and the rendered output read "Press now OK".
        if data.strip():
            self._stack[-1].children.append(UINode(tag="#text", text=data))
        elif self._stack[-1].text and not self._stack[-1].children:
            # Preserve meaningful inner whitespace only when there is no structure
            # around it; otherwise it is just the model's indentation.
            self._stack[-1].text += data

    # -- helpers -----------------------------------------------------------
    def _clean_attrs(self, tag: str, attrs: Dict[str, Optional[str]]) -> Dict[str, str]:
        allowed = set(ALLOWED_ATTRS.get("*", set())) | set(ALLOWED_ATTRS.get(tag, set()))
        out: Dict[str, str] = {}
        for name, value in attrs.items():
            lowered = name.lower()
            if lowered not in allowed:
                continue
            if value is None:
                # HTML boolean attributes carry no value: `<details open>` and
                # `<input checked>` reach the parser as None. Dropping them made a
                # spoken-for attribute silently disappear, so an "already open"
                # section collapsed and a checked box showed unchecked.
                if lowered in _BOOLEAN_ATTRS:
                    out[lowered] = ""
                continue
            out[lowered] = value
        return out

    @staticmethod
    def _action_from(attrs: List[Tuple[str, Optional[str]]]) -> str:
        for name, value in attrs:
            if name.lower() not in ("onclick", "data-onclick"):
                continue
            match = _ACTION_RE.match(value or "")
            if match:
                return match.group("id").strip()
        return ""


def split_output(text: str) -> List[Tuple[str, str]]:
    """Split a reply into alternating ``("markdown"|"ui", payload)`` pieces."""
    if not text:
        return []

    pieces: List[Tuple[str, str]] = []
    cursor = 0
    blocks = 0

    while True:
        open_match = _UI_OPEN_RE.search(text, cursor)
        if not open_match:
            break
        close_match = _UI_CLOSE_RE.search(text, open_match.end())
        if not close_match:
            # An unclosed block: treat the rest as markdown so nothing is lost.
            break
        if blocks >= MAX_UI_BLOCKS:
            break

        before = text[cursor : open_match.start()]
        if before.strip():
            pieces.append(("markdown", before))
        inner = text[open_match.end() : close_match.start()]
        pieces.append(("ui", inner))
        blocks += 1
        cursor = close_match.end()

    tail = text[cursor:]
    if tail.strip():
        pieces.append(("markdown", tail))
    return pieces


def parse_ui_block(inner: str) -> Tuple[List[UINode], List[UIAction], str]:
    """Parse one ``<ui>`` body. Returns ``(nodes, actions, error)``."""
    if len(inner) > MAX_HTML_CHARS:
        return [], [], f"UI block is too large (limit {MAX_HTML_CHARS} characters)"

    builder = _NodeBuilder()
    try:
        builder.feed(inner)
        builder.close()
    except Exception as exc:  # noqa: BLE001 - a bad block must not lose the reply
        return [], [], f"could not parse the UI block: {exc}"

    real_nodes = [node for node in builder.root.children if node.tag != "dropped"]

    # Fill action labels from the button text so the GUI can show a fallback.
    labels = _collect_action_labels(real_nodes)
    for action in builder.actions:
        action.label = labels.get(action.id, action.id)

    if builder.dropped:
        # Not an error: dropping is the designed behaviour. Recorded so the model
        # can be told, in the retry message, which tags are not allowed.
        pass

    return real_nodes, builder.actions, ""


def _node_text(node: UINode) -> str:
    """All text under a node, in document order.

    Needed now that prose lives in ``#text`` children rather than on the parent:
    reading ``node.text`` alone produced an empty label, so a button fell back to
    showing its action id instead of the words the model wrote.
    """
    if node.tag == "#text":
        return node.text
    parts = [node.text] if node.text else []
    for child in node.children:
        parts.append(_node_text(child))
    return " ".join(part for part in parts if part)


def _collect_action_labels(nodes: List[UINode]) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for node in nodes:
        if node.tag == "#text":
            continue
        action_id = node.attrs.get("data-action")
        if action_id:
            text = " ".join(_node_text(node).split())
            if text:
                labels[action_id] = text[:80]
        if node.children:
            labels.update(_collect_action_labels(node.children))
    return labels


def render(text: str) -> RenderedOutput:
    """Parse a whole assistant message into renderable segments."""
    output = RenderedOutput()
    seen_actions: set[str] = set()

    for kind, payload in split_output(text):
        if kind == "markdown":
            output.segments.append(OutputSegment(kind="markdown", text=payload.strip()))
            continue

        nodes, actions, error = parse_ui_block(payload)
        if error:
            output.segments.append(OutputSegment(kind="ui-error", error=error))
            continue
        if not nodes:
            continue
        output.segments.append(
            OutputSegment(kind="ui", nodes=nodes, actions=actions)
        )
        for action in actions:
            if action.id not in seen_actions:
                seen_actions.add(action.id)
                output.actions.append(action)

    if not output.segments and text.strip():
        output.segments.append(OutputSegment(kind="markdown", text=text.strip()))
    return output


def escape_text(value: str) -> str:
    """Escape for the rare case a caller needs to embed text in generated HTML."""
    return html.escape(value or "", quote=True)


__all__ = [
    "render",
    "split_output",
    "parse_ui_block",
    "RenderedOutput",
    "OutputSegment",
    "UINode",
    "UIAction",
    "ALLOWED_TAGS",
    "ALLOWED_ATTRS",
    "MAX_UI_BLOCKS",
]