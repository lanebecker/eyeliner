# Script-fallback fonts (R8-03 / #352)

This directory holds the Noto Sans JP faces the display uses to render text
the primary faces don't cover — CJK, plus the Cyrillic/Greek gaps in
Newsreader-Italic (album titles). Approved design (Lane, 2026-08-12):
role-weight mapping hero→SemiBold, artist→Medium, album+mono→Regular; fallback
runs render **upright** (no CJK/Cyrillic italic exists; faux-oblique rejected).

Expected files (see `_FALLBACK_FONT_FILES` in `src/display/typography.py`):

| File | Role(s) |
|---|---|
| `NotoSansJP-SemiBold.ttf` | hero (track title) |
| `NotoSansJP-Medium.ttf` | artist |
| `NotoSansJP-Regular.ttf` | album title, mono labels |
| `OFL-NotoSansJP.txt` | the SIL Open Font License, shipped alongside like the other bundled faces |

**Source:** Noto Sans JP by Google, SIL OFL 1.1 — download the static TTFs from
the official Google Fonts family page (fonts.google.com/noto/specimen/Noto+Sans+JP,
"Get font" → the static `NotoSansJP-*.ttf` files inside), or the
`notofonts`/`google/fonts` GitHub repositories. Copy the three weights above
plus the license file here.

**Graceful degrade:** if these files (or the `fonttools` dependency that reads
their coverage) are missing, the display logs ONE warning and renders exactly
as before R8-03 — uncovered scripts show as boxes. Nothing crashes.

Arabic/Hebrew are deliberately not bundled (rare in vinyl metadata; the
shaping gate routes them to a single run that would also need a shaping
engine). Revisit if a pressing shows up — see #352.
