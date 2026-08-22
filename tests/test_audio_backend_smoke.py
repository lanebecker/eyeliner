"""Unit checks for the CI sounddevice import boundary (#156)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_audio_backend.py"
REQUIRED_APIS = (
    "query_devices",
    "InputStream",
    "_terminate",
    "_initialize",
)


def _backend_module():
    assert SCRIPT.exists(), "CI audio backend smoke script is missing"
    spec = importlib.util.spec_from_file_location("check_audio_backend", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _complete_backend():
    return SimpleNamespace(**{name: lambda: None for name in REQUIRED_APIS})


def test_validate_backend_accepts_all_production_sounddevice_apis():
    backend = _backend_module()

    assert backend.validate_backend(_complete_backend()) == ()


@pytest.mark.parametrize("missing_api", REQUIRED_APIS)
def test_validate_backend_reports_each_missing_or_noncallable_api(missing_api):
    backend = _backend_module()
    module = _complete_backend()
    setattr(module, missing_api, None)

    assert backend.validate_backend(module) == (missing_api,)


def test_main_redacts_import_error_and_gives_portaudio_remediation(monkeypatch, capsys):
    backend = _backend_module()

    def raise_import_error(_name):
        raise ImportError("token=not-for-output")

    monkeypatch.setattr(backend.importlib, "import_module", raise_import_error)

    assert backend.main() == 1
    output = capsys.readouterr().err
    assert "sounddevice" in output
    assert "libportaudio2" in output
    assert "token=not-for-output" not in output


def test_successful_import_is_retained_without_a_conftest_owned_stub():
    import conftest

    external_module = SimpleNamespace(__name__="sounddevice")
    modules = {"sounddevice": external_module}

    module, owned_stub = conftest._load_sounddevice_or_install_stub(
        importer=lambda _name: external_module,
        modules=modules,
    )

    assert module is external_module
    assert owned_stub is None
    assert modules["sounddevice"] is external_module

    conftest._remove_owned_sounddevice_stub(modules, owned_stub)

    assert modules["sounddevice"] is external_module


def test_import_failure_installs_and_removes_only_the_conftest_owned_stub():
    import conftest

    def raise_missing_backend(_name):
        raise OSError("PortAudio unavailable")

    modules = {}
    module, owned_stub = conftest._load_sounddevice_or_install_stub(
        importer=raise_missing_backend,
        modules=modules,
    )

    assert module is owned_stub
    assert modules["sounddevice"] is owned_stub

    conftest._remove_owned_sounddevice_stub(modules, owned_stub)

    assert "sounddevice" not in modules


def test_conftest_teardown_does_not_remove_a_third_party_replacement():
    import conftest

    def raise_missing_backend(_name):
        raise OSError("PortAudio unavailable")

    modules = {}
    _module, owned_stub = conftest._load_sounddevice_or_install_stub(
        importer=raise_missing_backend,
        modules=modules,
    )
    third_party_module = SimpleNamespace(__name__="sounddevice")
    modules["sounddevice"] = third_party_module

    conftest._remove_owned_sounddevice_stub(modules, owned_stub)

    assert modules["sounddevice"] is third_party_module
