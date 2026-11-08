"""Hybrid rendering: Markdown interleaved with validated, interactive <ui> blocks.

The security assertions carry the weight here. Model output is untrusted, and the
whole design claim is that a hostile reply can produce *inert markup*, never
executed code. Each test below is an attempt to make something dangerous survive.
"""

from __future__ import annotations

import unittest

from onion_harness.core.hybrid import (
    ALLOWED_TAGS,
    MAX_UI_BLOCKS,
    escape_text,
    parse_ui_block,
    render,
    split_output,
)


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


class SplitTest(unittest.TestCase):
    def test_plain_text_is_one_markdown_segment(self):
        pieces = split_output("just text")
        self.assertEqual(pieces, [("markdown", "just text")])

    def test_text_around_a_block(self):
        pieces = split_output("before\n<ui><p>x</p></ui>\nafter")
        self.assertEqual([kind for kind, _ in pieces], ["markdown", "ui", "markdown"])

    def test_multiple_blocks(self):
        pieces = split_output("<ui><p>a</p></ui>mid<ui><p>b</p></ui>")
        self.assertEqual([kind for kind, _ in pieces], ["ui", "markdown", "ui"])

    def test_unclosed_block_falls_back_to_markdown(self):
        # Losing the widget is acceptable; losing the message is not.
        pieces = split_output("hello <ui><p>oops")
        self.assertEqual([kind for kind, _ in pieces], ["markdown"])
        self.assertIn("oops", pieces[0][1])

    def test_block_limit(self):
        text = "".join(f"<ui><p>{i}</p></ui>" for i in range(MAX_UI_BLOCKS + 5))
        blocks = [kind for kind, _ in split_output(text) if kind == "ui"]
        self.assertEqual(len(blocks), MAX_UI_BLOCKS)

    def test_case_insensitive_tags(self):
        pieces = split_output("<UI><p>x</p></UI>")
        self.assertEqual([kind for kind, _ in pieces], ["ui"])

    def test_empty_input(self):
        self.assertEqual(split_output(""), [])


class ActionCaptureTest(unittest.TestCase):
    def test_button_action_is_captured(self):
        nodes, actions, error = parse_ui_block(
            "<button onclick=\"sendAction('approve_flash')\">确认刷机</button>"
        )
        self.assertEqual(error, "")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].id, "approve_flash")
        self.assertEqual(actions[0].label, "确认刷机")
        self.assertEqual(nodes[0].attrs["data-action"], "approve_flash")

    def test_single_quotes_and_spacing(self):
        _, actions, _ = parse_ui_block("""<button onclick='sendAction( "go" )'>Go</button>""")
        self.assertEqual(actions[0].id, "go")

    def test_action_id_length_is_capped(self):
        long_id = "a" * 200
        _, actions, _ = parse_ui_block(f'<button onclick="sendAction(\'{long_id}\')">x</button>')
        # Over the cap: not captured at all, rather than captured truncated.
        self.assertEqual(actions, [])

    def test_multiple_actions_keep_order(self):
        _, actions, _ = parse_ui_block(
            '<button onclick="sendAction(\'b\')">B</button>'
            '<button onclick="sendAction(\'a\')">A</button>'
        )
        self.assertEqual([action.id for action in actions], ["b", "a"])


class SecurityTest(unittest.TestCase):
    """Each case is an attempt to smuggle something executable through."""

    def test_script_tag_is_dropped(self):
        nodes, actions, _ = parse_ui_block("<script>alert(1)</script>")
        self.assertNotIn("script", [node.tag for node in _walk(nodes)])
        self.assertEqual(actions, [])

    def test_event_handler_other_than_sendaction_is_not_captured(self):
        _, actions, _ = parse_ui_block('<button onclick="steal()">x</button>')
        self.assertEqual(actions, [], "an arbitrary handler must not become an action")

    def test_img_with_onerror_is_dropped(self):
        # The classic XSS vector: an image whose load failure runs code.
        nodes, _, _ = parse_ui_block('<img src=x onerror="alert(1)">')
        self.assertNotIn("img", [node.tag for node in _walk(nodes)])

    def test_style_tag_is_dropped(self):
        nodes, _, _ = parse_ui_block("<style>body{display:none}</style>")
        self.assertNotIn("style", [node.tag for node in _walk(nodes)])

    def test_iframe_is_dropped(self):
        nodes, _, _ = parse_ui_block('<iframe src="http://evil"></iframe>')
        self.assertNotIn("iframe", [node.tag for node in _walk(nodes)])

    def test_style_attribute_is_stripped(self):
        nodes, _, _ = parse_ui_block('<div style="position:fixed;inset:0">x</div>')
        self.assertNotIn("style", nodes[0].attrs)

    def test_javascript_href_is_not_kept(self):
        # `a` is not in the allowlist at all, so there is no path to an href.
        nodes, _, _ = parse_ui_block('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn("a", [node.tag for node in _walk(nodes)])

    def test_rejected_tag_does_not_swallow_later_content(self):
        """A dropped tag must not become a container for valid siblings."""
        nodes, actions, _ = parse_ui_block(
            '<script>x</script><button onclick="sendAction(\'ok\')">go</button>'
        )
        self.assertIn("button", [node.tag for node in _walk(nodes)])
        self.assertEqual([action.id for action in actions], ["ok"])

    def test_unknown_attributes_are_dropped(self):
        nodes, _, _ = parse_ui_block('<button formaction="/x" onclick="sendAction(\'a\')">x</button>')
        self.assertNotIn("formaction", nodes[0].attrs)

    def test_allowlist_is_closed(self):
        self.assertNotIn("script", ALLOWED_TAGS)
        self.assertNotIn("iframe", ALLOWED_TAGS)
        self.assertNotIn("img", ALLOWED_TAGS)
        self.assertNotIn("a", ALLOWED_TAGS)


class RenderingTest(unittest.TestCase):
    def test_full_render_shape(self):
        text = (
            "intro **bold**\n"
            "<ui><p>Confirm?</p>"
            '<button onclick="sendAction(\'approve_flash\')">确认刷机</button></ui>\n'
            "outro"
        )
        out = render(text)
        self.assertTrue(out.has_ui)
        self.assertEqual([seg.kind for seg in out.segments], ["markdown", "ui", "markdown"])
        self.assertEqual([action.id for action in out.actions], ["approve_flash"])
        self.assertIn("intro", out.plain_text())
        self.assertIn("outro", out.plain_text())
        self.assertNotIn("sendAction", out.plain_text())

    def test_to_dict_is_json_safe(self):
        import json

        out = render("<ui><p>x</p><button onclick=\"sendAction('a')\">y</button></ui>")
        json.dumps(out.to_dict())

    def test_text_only_reply_has_no_ui(self):
        out = render("no widgets here")
        self.assertFalse(out.has_ui)
        self.assertEqual(out.segments[0].kind, "markdown")

    def test_empty_reply(self):
        out = render("")
        self.assertEqual(out.segments, [])
        self.assertFalse(out.has_ui)

    def test_empty_ui_block_is_skipped(self):
        self.assertFalse(render("<ui></ui>").has_ui)

    def test_nested_allowed_tags(self):
        out = render("<ui><section><h2>Title</h2><p>body</p></section></ui>")
        self.assertTrue(out.has_ui)
        section = out.segments[0].nodes[0]
        self.assertEqual(section.tag, "section")
        self.assertEqual([child.tag for child in section.children], ["h2", "p"])

    def test_table_structure_survives(self):
        out = render("<ui><table><tr><th>a</th><td>b</td></tr></table></ui>")
        self.assertTrue(out.has_ui)

    def test_escape_text(self):
        self.assertEqual(escape_text('<a href="x">'), "&lt;a href=&quot;x&quot;&gt;")


class ParseErrorTest(unittest.TestCase):
    def test_oversized_block_is_labelled_not_crashing(self):
        nodes, actions, error = parse_ui_block("<p>" + "x" * 30_000 + "</p>")
        self.assertTrue(error)
        self.assertIn("too large", error)

    def test_unbalanced_tags_do_not_corrupt_siblings(self):
        nodes, actions, _ = parse_ui_block(
            '<div><p>unclosed<span>a</span></div>'
            '<button onclick="sendAction(\'z\')">z</button>'
        )
        self.assertIn("button", [node.tag for node in _walk(nodes)])
        self.assertEqual([action.id for action in actions], ["z"])

    def test_stray_end_tag_is_ignored(self):
        nodes, _, _ = parse_ui_block("</div><p>ok</p>")
        self.assertEqual(nodes[0].tag, "p")


if __name__ == "__main__":
    unittest.main()