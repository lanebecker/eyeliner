"""R5-17 (#237) — fold_text must strip Unicode format (Cf) characters.

NFKC does not remove zero-width / format characters, so a single invisible
character in a community-edited Discogs title or a Shazam string made an owned
album permanently unmatchable — and undiagnosable, since both sides print
identically in a log. Stripping Cf only widens lossless folding (an invisible
char carries no matching intent), so it is refusal-safe.
"""
import pytest

from src.metadata.normalize import fold_text


@pytest.mark.parametrize("cf, name", [
    ("​", "ZERO WIDTH SPACE"),
    ("­", "SOFT HYPHEN"),
    ("﻿", "ZERO WIDTH NO-BREAK SPACE / BOM"),
    ("⁠", "WORD JOINER"),
    ("‎", "LEFT-TO-RIGHT MARK"),
    ("‏", "RIGHT-TO-LEFT MARK"),
])
def test_cf_character_does_not_defeat_the_fold(cf, name):
    assert fold_text("Rumours") == fold_text("Rumours" + cf)
    assert fold_text("Rumours") == fold_text(cf + "Rum" + cf + "ours")


@pytest.mark.parametrize("keep, name", [
    ("‌", "ZERO WIDTH NON-JOINER"),
    ("‍", "ZERO WIDTH JOINER"),
])
def test_zwnj_and_zwj_are_kept_not_stripped(keep, name):
    """R5-17 cold-review LOW: ZWNJ/ZWJ are lexically load-bearing in some
    scripts, so they are KEPT — folding them away could merge two genuinely
    different titles (the phantom-credit direction). A title contaminated with
    one still misses (the fail-safe direction), which is the accepted tradeoff."""
    assert fold_text("Rumours") != fold_text("Rumours" + keep)


def test_cf_strip_leaves_ordinary_titles_unchanged():
    assert fold_text("Kind of Blue") == "kind of blue"
    # a real, visible title with punctuation still folds to itself
    assert fold_text("Sign O’ the Times") == fold_text("Sign O’ the Times")


def test_non_cf_whitespace_still_collapses():
    # NBSP is a separator (Zs), not Cf; whitespace collapse still handles it
    assert fold_text("A B") == fold_text("A B")
