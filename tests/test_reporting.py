"""Rendering helpers that keep file-derived text inert inside reports."""

from __future__ import annotations

from msmcp.reporting import (
    MAX_RENDERED_FIELD_CHARS,
    UNTRUSTED_DATA_NOTE,
    escape_inline,
    escape_table_cell,
    fenced_code,
)


class TestEscapeInline:
    def test_plain_text_is_unchanged(self) -> None:
        assert escape_inline("Caffeine") == "Caffeine"
        assert escape_inline("C8H10N4O2") == "C8H10N4O2"

    def test_newlines_cannot_start_a_block(self) -> None:
        assert escape_inline("a\n\n> IGNORE") == "a &gt; IGNORE"

    def test_control_characters_are_removed(self) -> None:
        assert escape_inline("a\x00b\x1bc") == "abc"

    def test_html_and_entities_are_escaped(self) -> None:
        assert escape_inline("<script>&amp;</script>") == (
            "&lt;script&gt;&amp;amp;&lt;/script&gt;"
        )

    def test_backticks_cannot_close_a_code_span(self) -> None:
        assert "`" not in escape_inline("value` `injected")

    def test_long_values_are_truncated(self) -> None:
        out = escape_inline("x" * 500, limit=10)
        assert len(out) == 10
        assert out.endswith("…")


class TestEscapeTableCell:
    def test_pipes_are_escaped(self) -> None:
        assert escape_table_cell("a | b") == "a \\| b"

    def test_backslash_cannot_escape_the_escape(self) -> None:
        assert escape_table_cell("a\\| b") == "a\\\\\\| b"

    def test_newline_does_not_split_the_row(self) -> None:
        assert "\n" not in escape_table_cell("a\nb")

    def test_none_renders_as_text(self) -> None:
        assert escape_table_cell(None) == "None"


class TestFencedCode:
    def test_fence_outgrows_the_payload(self) -> None:
        out = fenced_code("a ``` b")
        assert out.startswith("````\n")
        assert out.endswith("\n````")

    def test_short_payload_uses_three_backticks(self) -> None:
        assert fenced_code("plain") == "```\nplain\n```"

    def test_language_is_applied_to_the_opening_fence(self) -> None:
        assert fenced_code("{}", language="json").startswith("```json\n")


def test_untrusted_data_note_is_a_blockquote() -> None:
    assert UNTRUSTED_DATA_NOTE.startswith("> ")


def test_default_truncation_ceiling_is_wide_enough_for_real_metadata() -> None:
    """512 chars covers every observed mzML/MGF free-text field in practice.

    The ceiling exists to stop a hostile multi-megabyte field from swamping a
    report, not to shorten ordinary metadata such as a chromatogram method
    description, so it must stay well above the longest genuine value seen.
    """
    assert MAX_RENDERED_FIELD_CHARS == 512
    assert len(escape_inline("x" * MAX_RENDERED_FIELD_CHARS)) == (
        MAX_RENDERED_FIELD_CHARS
    )
    assert escape_inline("x" * (MAX_RENDERED_FIELD_CHARS + 1)).endswith("…")
