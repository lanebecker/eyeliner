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

R8-22/#363's duplicated release-version regexes were retired by R10-02/#415.
All three workflows now execute ``scripts/check_version_metadata.py``; its
supported and rejected release forms are behavior-tested in
``test_version_metadata_r10.py`` instead of source-text-diffed here.
"""


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
