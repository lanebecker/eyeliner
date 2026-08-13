"""R8 Wave 4 (#361, #363) — make the CI legs able to FAIL.

R8-08/#361: the only place the suite touched the REAL shazamio was
``pytest.importorskip``, which raises ``Skipped`` — reported as a SKIP, not a
failure — so the Python-3.13 matrix leg could not catch the exact #198 class it
exists for (audioop removed from the stdlib → ``import shazamio`` breaks at
runtime while ``pip install`` succeeds). A broken recognition import is the
most probable SILENT bring-up failure: the display latches NO MATCH FOUND
while systemd sees a healthy service. These tests hard-import on the running
interpreter, so each matrix leg can genuinely FAIL on an import breakage —
on 3.13 that exercises the audioop-lts backport path specifically (3.11/3.12
have stdlib audioop and exercise the plain import).

R8-22/#363: R7-22 asked for a single-sourced release-version regex; v1.5.25
shipped two byte-identical copies with "keep in sync" comments — the exact
drift mechanism the finding described, one edit from recurring. Per the issue's
accept-with-a-stronger-tripwire option: this test extracts both patterns and
diffs them, going RED the moment they drift.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_shazamio_imports_on_this_interpreter():
    """R8-08 (#361): the real import, no importorskip — this is the line that
    makes the 3.13 leg able to fail on the #198 class."""
    import shazamio

    assert shazamio is not None   # the import IS the assertion (keeps linters quiet)


def test_recognition_probe_passes_with_the_real_importer():
    """R8-08 (#361): the startup probe (which every other test exercises only
    with a MOCK ``_import``) must pass with its real default importer on this
    interpreter — pinning that ``verify_recognition_backend_importable`` and
    the actual environment agree."""
    from main import verify_recognition_backend_importable
    from src.config import AppConfig
    from tests.test_config import _valid_raw

    cfg = AppConfig.from_dict(_valid_raw())
    verify_recognition_backend_importable(cfg)   # must not raise


def _version_regexes() -> dict:
    """Extract the release-semver grep pattern from each workflow that carries
    one.  Anchored single-quoted grep -Eq patterns starting ^[0-9]."""
    out = {}
    for wf in ("release-consistency.yml", "sync-version-badge.yml"):
        text = (REPO / ".github" / "workflows" / wf).read_text()
        found = re.findall(r"grep -Eq '(\^\[0-9\][^']*)'", text)
        assert found, f"{wf}: expected a version regex (did it move?)"
        out[wf] = set(found)
    return out


def test_r8_22_version_regexes_have_not_drifted():
    """R8-22 (#363): the two workflows carry deliberately-duplicated version
    regexes ("keep in sync" comments) — this tripwire fails the suite the
    moment one is edited without the other."""
    regexes = _version_regexes()
    all_patterns = set().union(*regexes.values())
    assert len(all_patterns) == 1, (
        f"the release-version regexes have DRIFTED between workflows "
        f"(R7-22/R8-22): {regexes}"
    )


def test_r8_22_the_shared_regex_still_accepts_release_and_prerelease():
    """Control: the shared pattern accepts what the release flow produces —
    plain semver and the pre-release form — and rejects the known evils."""
    (pattern,) = set().union(*_version_regexes().values())
    rx = re.compile(pattern)
    for good in ("1.5.29", "10.0.1", "1.6.0-rc.1", "2.0.0-beta2"):
        assert rx.search(good), f"{pattern!r} must accept {good!r}"
    for bad in ("1x5x29", "v1.5.29", "1.5", "1.5.29 ", ""):
        assert not rx.search(bad), f"{pattern!r} must reject {bad!r}"
