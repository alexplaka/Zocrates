"""Regression tests for the ReDoS / polynomial-ReDoS regex hardening (CWE-1333/1335).

Three patterns are covered here:

1. ``_HEADING_RE`` and ``_SENTENCE_BOUNDARY_RE`` (sentence_attribution.py) were
   hardened with possessive quantifiers. Possessive quantifiers preserve the
   matched language exactly (give-back of the hardened runs can never change
   the outcome because the hardened classes are disjoint from what follows),
   while eliminating the quadratic backtracking CodeQL flags (CWE-1333).
   ``_SENTENCE_BOUNDARY_RE`` keeps its group indices (1 = punctuation,
   2 = whitespace) and match extents, which the segmentation logic depends on.

2. ``_AUTHOR_ONLY_RE`` (citation_verifier.py) is intentionally UNCHANGED. The
   separator alternation shares whitespace runs between adjacent separators
   (e.g. ", " + " et al" vs "," + " et al"), so its match set depends on
   partial backtracking that any atomic/possessive rewrite provably changes —
   differential testing showed realistic inputs like "Smith, et al" flipping
   from match to no-match. The four CodeQL py/redos alerts on this pattern are
   intentionally left open as reviewed formal-only findings: no measurable
   super-linear behavior on any supported CPython under the detector's own
   pumping inputs, and the span passed to .match() is bounded by
   MAX_CLAIM_CHARS in _find_backward_claim_boundary(). The characterization
   tests below pin that actual behavior so the pattern cannot drift silently.

The ``*_ORIG`` patterns embedded in the fuzz tests are verbatim copies of the
pre-hardening regexes, kept only as test oracles for differential comparison.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from zocrates.agents.library_search.citation_verifier import _AUTHOR_ONLY_RE
from zocrates.agents.library_search.sentence_attribution import (
    _HEADING_RE,
    _SENTENCE_BOUNDARY_RE,
    segment_source_text,
)

TIME_BUDGET_SEC = 5.0

# Pre-hardening reference patterns (test oracles only — do not import into
# production code).
SENTENCE_BOUNDARY_ORIG = re.compile(r'([.?!]+)(\s+)(?=[A-Z0-9"\'\(\[])')
HEADING_ORIG = re.compile(r"^\s*#{1,6}\s+.*$")


# ---------------------------------------------------------------------------
# _HEADING_RE (sentence_attribution.py)
# ---------------------------------------------------------------------------


class TestHeadingCharacterization:
    @pytest.mark.parametrize(
        "line",
        [
            "# Title",
            "## Section",
            "### Subsection",
            "###### Deep heading",
            "###   Extra   spaces   allowed",
        ],
    )
    def test_matches_headings(self, line: str) -> None:
        assert _HEADING_RE.match(line), line

    @pytest.mark.parametrize(
        "line",
        [
            "Plain text",
            "###NoSpace",  # original requires whitespace after hashes
            "####### Seven hashes",  # more than 6 hashes
            "###",  # hashes without trailing whitespace do not match
            "",
        ],
    )
    def test_rejects_non_headings(self, line: str) -> None:
        assert not _HEADING_RE.match(line), line


class TestHeadingPerformance:
    def test_long_space_run_is_fast(self) -> None:
        line = "###" + " " * 100000 + "Title"
        start = time.perf_counter()
        assert _HEADING_RE.match(line)
        assert time.perf_counter() - start < TIME_BUDGET_SEC

    def test_failing_multiline_input_is_fast(self) -> None:
        text = "###" + " " * 50000 + "\nx\ny"
        start = time.perf_counter()
        assert not _HEADING_RE.match(text)
        assert time.perf_counter() - start < TIME_BUDGET_SEC


# ---------------------------------------------------------------------------
# _SENTENCE_BOUNDARY_RE (sentence_attribution.py)
# ---------------------------------------------------------------------------


class TestSentenceBoundaryCharacterization:
    def test_groups_and_extents(self) -> None:
        m = _SENTENCE_BOUNDARY_RE.search("Hello world. Next sentence")
        assert m is not None
        assert m.group(1) == "."  # group 1: punctuation run
        assert m.group(2) == " "  # group 2: whitespace run
        assert m.end(1) == m.string.index(".") + 1

    def test_punctuation_runs_captured_whole(self) -> None:
        m = _SENTENCE_BOUNDARY_RE.search("Really?! Next")
        assert m is not None
        assert m.group(1) == "?!"

    def test_no_match_without_following_boundary_char(self) -> None:
        # Lowercase after punctuation+whitespace: not a sentence boundary.
        assert not _SENTENCE_BOUNDARY_RE.search("Pause... then lowercase")
        # Digits, quotes and brackets ARE boundary chars.
        for follower in ("9", '"', "'", "(", "["):
            assert _SENTENCE_BOUNDARY_RE.search(f"Done. {follower}tail"), follower

    def test_newline_boundary(self) -> None:
        # \s must keep matching newlines (raw prose is multi-line).
        m = _SENTENCE_BOUNDARY_RE.search("End.\nNext")
        assert m is not None
        assert m.group(1) == "."


class TestSentenceBoundaryPerformance:
    def test_long_punctuation_run_is_fast(self) -> None:
        prose = "!" * 30000 + " x"
        start = time.perf_counter()
        list(_SENTENCE_BOUNDARY_RE.finditer(prose))
        assert time.perf_counter() - start < TIME_BUDGET_SEC

    def test_segmentation_on_adversarial_prose_is_fast(self) -> None:
        # Full segment_source_text path: punctuation wall inside a long block.
        prose = "Figure shows " + "! " * 15000 + "result"
        start = time.perf_counter()
        segment_source_text(prose)
        assert time.perf_counter() - start < TIME_BUDGET_SEC


# ---------------------------------------------------------------------------
# Differential fuzz: possessive patterns vs pre-hardening oracles
# ---------------------------------------------------------------------------


class TestPossessiveEquivalenceFuzz:
    """Seeded randomized differential tests against the original patterns.

    The possessive quantifiers must not change WHAT is matched — only how
    fast the engine gets there. For finditer, the full match spans, group
    spans, and group texts must be identical for every input.
    """

    SEED = 20261119
    N_INPUTS = 30000
    MAX_LEN = 50

    ALPHABET = list(".!? \nxXN9\"'([]#\n\t|ab-") + [
        "...", "?!", "Title", "###", "### ", "Word. Next", "e.g.",
    ]

    def _random_inputs(self) -> list[str]:
        rng = random.Random(self.SEED)
        inputs = []
        for _ in range(self.N_INPUTS):
            n = rng.randint(0, 12)
            s = "".join(
                rng.choice(self.ALPHABET) if rng.random() < 0.8
                else rng.choice(".!? \nx#(\"9'[\t\n-")
                for _ in range(n)
            )
            inputs.append(s[: self.MAX_LEN])
        return inputs

    def test_sentence_boundary_fuzz_equivalence(self) -> None:
        for s in self._random_inputs():
            orig = [
                (m.span(), m.span(1), m.span(2), m.group(1), m.group(2))
                for m in SENTENCE_BOUNDARY_ORIG.finditer(s)
            ]
            new = [
                (m.span(), m.span(1), m.span(2), m.group(1), m.group(2))
                for m in _SENTENCE_BOUNDARY_RE.finditer(s)
            ]
            assert orig == new, f"divergence on {s!r}: {orig} vs {new}"

    def test_heading_fuzz_equivalence(self) -> None:
        for s in self._random_inputs():
            orig = HEADING_ORIG.match(s)
            new = _HEADING_RE.match(s)
            assert (orig is None) == (new is None), f"divergence on {s!r}"
            if orig is not None:
                assert orig.span() == new.span(), f"span divergence on {s!r}"


# ---------------------------------------------------------------------------
# _AUTHOR_ONLY_RE (citation_verifier.py) — intentionally unchanged
# ---------------------------------------------------------------------------


class TestAuthorOnlyCharacterization:
    """Pins the ACTUAL behavior of the unchanged _AUTHOR_ONLY_RE.

    Notable real behaviors (verified against the original pattern):
    - the name token needs at least 2 characters ("a" alone is not a name);
    - a single name plus trailing separator junk matches ("Smith, et al");
    - anything requiring a SECOND name token does not match ("Jones, Smith"),
      because the outer group is not looped and the tail cannot consume words.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Smith et al.",
            "Smith et al. (2020)",
            "Smith, et al",
            "Smith, et al. (2019)",
            "Smith et al. (2019a)",
            "Ab,",
            "and,",
            "Smith",
            "smith et al.",
            "SMITH, ET AL.",
            "According to Smith et al.",
            "As shown by Smith et al. (2019)",
            "in Smith et al.",
        ],
    )
    def test_matches_author_only_fragments(self, text: str) -> None:
        assert _AUTHOR_ONLY_RE.match(text), text

    @pytest.mark.parametrize(
        "text",
        [
            "",  # empty
            "Smith2",  # digits are not name characters
            "et al",  # no name token before the separator
            "et al.",
            "et  al. ",
            "al.",
            "al.,",
            "Smith et al. 2019",  # year without parens is not consumed
            "a &",  # single-char name is not a valid name token
            "a&,",
            "a&&",
            "a, and ",  # multi-char name required before separators chain
            "a and  and  ",
            "a and\nand ",
            "and ,",
            "Jones, Smith",  # second name token cannot be consumed
            "A, B",
            "Smith and Jones",
            "Smith, Jones & Brown (2020)",
            "Smith et al. showed that the model converges.",  # claim continues
            "The protein folds into a beta barrel.",  # plain sentence
        ],
    )
    def test_rejects_non_author_fragments(self, text: str) -> None:
        assert not _AUTHOR_ONLY_RE.match(text), text
