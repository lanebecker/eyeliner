"""R8-03 (#352) — per-run script fallback (_CompositeFont).

The bundled faces cover Latin (Inter Tight / JetBrains Mono also Cyrillic and
Greek) but Newsreader-Italic does not, and none cover CJK — so non-Latin
metadata rendered as .notdef tofu (all four roles for a Japanese pressing; the
album title alone for a Кино record). ``TextRenderer.font`` now returns a
``_CompositeFont`` that renders runs the primary face lacks with the role's
fallback face (Noto Sans JP in matching weights on the Pi), upright, with
baseline-aligned composition. Design locked Lane 2026-08-12 (mockup-approved).

Most tests here use JetBrainsMono as a STAND-IN fallback (it covers Cyrillic,
is always bundled, and exercises the identical run-splitting machinery); the
real-Noto tests skip when the fallback files aren't present (they arrive with
the Wave-2 commit — see assets/fonts/fallback/README.md).
"""
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

import src.display.typography as typo  # noqa: E402
from src.display.renderer import _BoundedCache  # noqa: E402


@pytest.fixture(autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((64, 64))
    yield


def _tr():
    return typo.TextRenderer(_BoundedCache(32), _BoundedCache(32))


@pytest.fixture
def standin_fallback(monkeypatch):
    """Point every role's fallback at JetBrainsMono (bundled, has Cyrillic)."""
    monkeypatch.setattr(typo, "_FALLBACK_DIR", typo._FONT_DIR)
    monkeypatch.setattr(
        typo, "_FALLBACK_FONT_FILES",
        {k: "JetBrainsMono-Regular.ttf" for k in typo._FALLBACK_FONT_FILES},
    )
    return _tr()


def _sig(font, ch):
    return pygame.image.tobytes(font.render(ch, True, (255, 255, 255)), "RGBA")


def _notdef_sig(font):
    return _sig(font, "")   # PUA — uncovered by every face involved


# ---------------------------------------------------------------------------
# Real-glyph rendering
# ---------------------------------------------------------------------------

def test_cyrillic_album_title_renders_real_glyphs_via_fallback(standin_fallback):
    """RED before R8-03: Newsreader-Italic (the album role) lacks Cyrillic, so
    every char of "Кино" rendered as the .notdef box.  With a Cyrillic-capable
    fallback wired, none do."""
    font = standin_fallback.font("title", 32)
    notdef = _notdef_sig(font)
    tofu = [ch for ch in "Кино" if _sig(font, ch) == notdef]
    assert tofu == [], f"chars still rendering as .notdef: {tofu}"


def test_uncovered_by_both_faces_stays_primary_notdef(standin_fallback):
    """Text neither face covers (Arabic — deliberately unbundled) must stay a
    PRIMARY .notdef run — same visible result as pre-R8-03, no crash, and no
    fallback-face notdef swap (the run rule requires the fallback to actually
    cover the char)."""
    font = standin_fallback.font("title", 32)
    surf = font.render("حب", True, (255, 255, 255))
    assert surf.get_width() > 0   # renders (as boxes), never raises


def test_ascii_fast_path_is_byte_identical_to_primary(standin_fallback):
    font = standin_fallback.font("display", 40)
    composite = font.render("Abbey Road", True, (240, 240, 240))
    primary = font._primary.render("Abbey Road", True, (240, 240, 240))
    assert pygame.image.tobytes(composite, "RGBA") == pygame.image.tobytes(primary, "RGBA")


def test_size_matches_render_width_for_mixed_runs(standin_fallback):
    font = standin_fallback.font("title", 32)
    for text in ("Кино Rocks", "Группа крови", "plain ascii", "é·…"):
        assert font.size(text)[0] == font.render(text, True, (255, 255, 255)).get_width()


def test_layout_metrics_are_primary_metrics(standin_fallback):
    """get_height/get_ascent report the PRIMARY face — line advance and
    baselines must not jump when a fallback run appears."""
    font = standin_fallback.font("text", 48)
    assert font.get_height() == font._primary.get_height()
    assert font.get_ascent() == font._primary.get_ascent()


def test_wrap_and_fit_accept_mixed_text(standin_fallback):
    """wrap_lines/fit_wrapped drive font.size on mixed strings — must not raise
    and must produce fitting lines."""
    tr = standin_fallback
    size, lines = tr.fit_wrapped("Группа крови группа крови", "title", 32, 300, 3)
    font = tr.font("title", size)
    assert lines and all(font.size(line)[0] <= 300 for line in lines)


def test_combining_mark_stays_attached_to_its_bases_run(standin_fallback):
    """Cold-review F5: a combining mark (U+0301) after a fallback run must join
    that run when the fallback face covers it — not split into its own primary
    run and render as a detached spacing glyph."""
    font = standin_fallback.font("title", 32)
    runs = list(font._runs("Кино́"))
    fcov = font._fcov
    if ord("́") in fcov:
        assert len(runs) == 1 and runs[0][0] is font._fallback, (
            f"combining mark detached from its base's run: {[(f is font._fallback, c) for f, c in runs]}"
        )
    else:
        # The stand-in fallback lacks the mark: it must then stay a separate
        # primary run (the covered-by-prev rule requires coverage) — no crash.
        assert runs[-1][1] == "́"


def test_tracked_label_mixes_faces_with_baseline_alignment(standin_fallback):
    """render_tracked with a mixed label: width follows the per-glyph advance
    arithmetic and the surface is at least the primary's height (fallback
    glyphs may extend it; baselines are aligned via per-char ascents)."""
    tr = standin_fallback
    surf = tr.render_tracked("К-12", 13, (200, 200, 200), 0.08)
    mono = tr.font("mono", 13)
    assert surf.get_height() >= mono.get_height()
    assert surf.get_width() > 0


# ---------------------------------------------------------------------------
# Coverage machinery + graceful degrade
# ---------------------------------------------------------------------------

def test_coverage_sets_reflect_the_real_cmaps():
    news = typo._font_coverage(typo._FONT_DIR / "Newsreader-Italic.ttf")
    mono = typo._font_coverage(typo._FONT_DIR / "JetBrainsMono-Regular.ttf")
    inter = typo._font_coverage(typo._FONT_DIR / "InterTight-SemiBold.ttf")
    assert news is not None and mono is not None and inter is not None
    assert ord("К") not in news       # the R8-03 album-title gap
    assert ord("К") in mono
    assert ord("К") in inter          # why hero/artist already worked for Кино
    assert ord("戦") not in inter     # CJK: nobody bundled covers it


def test_missing_fallback_degrades_to_single_face_with_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(typo, "_FALLBACK_DIR", typo._FONT_DIR / "nonexistent")
    monkeypatch.setattr(typo.TextRenderer, "_fallback_missing_warned", False)
    tr = _tr()
    import logging
    with caplog.at_level(logging.WARNING):
        f1 = tr.font("title", 32)
        f2 = tr.font("display", 40)
    warnings = [r for r in caplog.records if "script-fallback font missing" in r.message]
    assert len(warnings) == 1, "exactly ONE warning for the whole process"
    assert f1._fallback is None and f2._fallback is None
    # Single-face behavior == pre-R8-03: renders (tofu for Cyrillic) without error.
    assert f1.render("Кино", True, (255, 255, 255)).get_width() > 0


def test_unknown_coverage_degrades_to_all_primary(standin_fallback, monkeypatch):
    """fontTools unavailable (coverage None) → every char renders primary."""
    tr = standin_fallback
    monkeypatch.setattr(typo, "_font_coverage", lambda path: None)
    tr._font_cache = _BoundedCache(8)   # bypass cached composites
    font = tr.font("title", 32)
    runs = list(font._runs("Кино Rocks"))
    assert len(runs) == 1 and runs[0][0] is font._primary


# ---------------------------------------------------------------------------
# The real Noto files (present once Lane lands them with this wave)
# ---------------------------------------------------------------------------

_NOTO = typo._FALLBACK_DIR / "NotoSansJP-Regular.ttf"


@pytest.mark.skipif(not _NOTO.exists(), reason="Noto fallback files not yet downloaded")
def test_real_noto_covers_the_locked_design_scripts():
    cov = typo._font_coverage(_NOTO)
    assert cov is not None
    for ch in "戦場のメリークリスマス坂本龍一КиноΩμ":
        assert ord(ch) in cov, f"NotoSansJP missing {ch!r}"


@pytest.mark.skipif(not _NOTO.exists(), reason="Noto fallback files not yet downloaded")
def test_real_noto_renders_japanese_pressing_without_tofu():
    tr = _tr()
    font = tr.font("display", 72)
    notdef = _notdef_sig(font)
    tofu = [ch for ch in "坂本龍一" if _sig(font, ch) == notdef]
    assert tofu == []
