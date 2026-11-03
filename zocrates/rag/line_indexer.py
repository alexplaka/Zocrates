"""Precomputed character-offset indexing utilities for line mapping.

Provides fast, deterministic, 1-indexed line number resolution for converted
Markdown documents, shared across ToC generation and chunking pipelines.
"""

import bisect


class LineIndex:
    """Precomputed character-offset index for fast, deterministic character-to-line mapping."""

    def __init__(self, text: str) -> None:
        """Construct a line index from source text.

        Args:
            text: Raw input text.
        """
        lines = text.splitlines(keepends=True)
        if not lines:
            lines = [""]
        self.total_lines = len(lines)
        self.offsets: list[int] = [0]
        for line in lines[:-1]:
            self.offsets.append(self.offsets[-1] + len(line))

    def char_to_line(self, char_pos: int) -> int:
        """Map a 0-indexed character position to a 1-indexed line number.

        Args:
            char_pos: Character position in source text.

        Returns:
            1-indexed line number clamped to ``[1, total_lines]``.
        """
        idx = bisect.bisect_right(self.offsets, char_pos) - 1
        return max(1, min(self.total_lines, idx + 1))

    def span_to_lines(self, start_char: int, end_char: int) -> tuple[int, int]:
        """Map a half-open character range ``[start_char, end_char)`` to 1-indexed line bounds.

        Args:
            start_char: 0-indexed inclusive start character position.
            end_char: 0-indexed exclusive end character position.

        Returns:
            Tuple of ``(line_start, line_end)`` where ``line_start <= line_end``.
        """
        if start_char > end_char:
            start_char, end_char = end_char, start_char
        l_start = self.char_to_line(start_char)
        l_end = self.char_to_line(max(start_char, end_char - 1))
        return l_start, max(l_start, l_end)


def char_offset_to_line(
    offsets: list[int],
    char_pos: int,
    total_lines: int | None = None,
) -> int:
    """Binary search character offset to 1-indexed line number.

    Args:
        offsets: List of character start offsets for each line (0-indexed).
        char_pos: Character index in document.
        total_lines: Optional total number of lines to clamp the maximum line number.

    Returns:
        1-indexed line number.
    """
    idx = bisect.bisect_right(offsets, char_pos) - 1
    res = max(1, idx + 1)
    if total_lines is not None:
        res = min(total_lines, res)
    return res
