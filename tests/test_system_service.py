"""Contracts for the versioned system-service renderer (#419)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "render_system_service.py"
PYTHON = sys.executable


def _make_app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "vinyl-now-playing"
    app_dir.mkdir()
    config = app_dir / "config.yaml"
    config.write_text("discogs:\n  token: secret-not-for-output\n")
    os.chmod(config, 0o600)
    return app_dir


def _arguments(base_app_dir: Path, base_output: Path, **overrides: str) -> list[str]:
    values = {
        "user": "pi",
        "app_dir": str(base_app_dir),
        "display": ":0",
        "xauthority": "/home/pi/.Xauthority",
        "output": str(base_output),
    }
    values.update(overrides)
    return [
        "--user",
        values["user"],
        "--app-dir",
        values["app_dir"],
        "--display",
        values["display"],
        "--xauthority",
        values["xauthority"],
        "--output",
        values["output"],
    ]


def _run_renderer(
    base_app_dir: Path, base_output: Path, **overrides: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(SCRIPT), *_arguments(base_app_dir, base_output, **overrides)],
        text=True,
        capture_output=True,
        check=False,
    )


def _load_renderer_module():
    spec = importlib.util.spec_from_file_location("render_system_service", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_direct(
    renderer, base_app_dir: Path | str, base_output: Path | str, **overrides: str
):
    values = {
        "user": "pi",
        "app_dir": base_app_dir,
        "display": ":0",
        "xauthority": "/home/pi/.Xauthority",
        "output": base_output,
    }
    values.update(overrides)
    return renderer.render_system_service(**values)


def test_renderer_emits_the_supported_system_unit_without_reading_or_chmodding_config(
    tmp_path,
):
    """Changing any required unit directive makes the rendered deployment unsafe."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    output = tmp_path / "vinyl-now-playing.service"
    mode_before = config.stat().st_mode & 0o777

    completed = _run_renderer(app_dir, output)

    assert completed.returncode == 0, completed.stderr
    assert config.stat().st_mode & 0o777 == mode_before == 0o600
    assert "secret-not-for-output" not in output.read_text()
    assert output.read_text() == """[Unit]
Description=vinyl-now-playing
Wants=network-online.target
After=network-online.target time-sync.target graphical.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=pi
WorkingDirectory={app_dir}
Environment=\"DISPLAY=:0\"
Environment=\"XAUTHORITY=/home/pi/.Xauthority\"
ExecStart={app_dir}/venv/bin/python3 {app_dir}/main.py
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=15
TimeoutStopSec=30

[Install]
WantedBy=graphical.target
""".format(app_dir=app_dir)
    assert output.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("user", "pi\nExecStart=/tmp/evil"),
        ("user", "pi user"),
        ("app_dir", "relative/app"),
        ("app_dir", "{app_dir}\t"),
        ("display", ":0\nEnvironment=EVIL=yes"),
        ("display", ":0 display"),
        ("xauthority", "relative/.Xauthority"),
        ("xauthority", "/home/pi/.Xauthority\r"),
        ("output", "relative.service"),
        ("output", "{output}\nother.service"),
    ),
)
def test_renderer_rejects_relative_control_and_whitespace_injection_values(
    tmp_path, field, value
):
    """An unsafe substitution must not create a second systemd directive."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    value = value.format(app_dir=app_dir, output=output)
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output, **{field: value})

    assert not output.exists()


def test_renderer_requires_an_existing_application_directory(tmp_path):
    """A typo in the deployment path must fail before any unit is written."""
    missing_app = tmp_path / "missing"
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, missing_app, output)

    assert not output.exists()


def test_renderer_requires_a_config_file(tmp_path):
    """A runnable-looking unit without the operator config must not be emitted."""
    app_dir = tmp_path / "vinyl-now-playing"
    app_dir.mkdir()
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert not output.exists()


@pytest.mark.parametrize("mode", (0o640, 0o644, 0o660, 0o700))
def test_renderer_rejects_any_config_mode_other_than_0600(tmp_path, mode):
    """Loosening or over-restricting the operator credential file blocks deploy."""
    app_dir = _make_app(tmp_path)
    os.chmod(app_dir / "config.yaml", mode)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert not output.exists()
    assert app_dir.joinpath("config.yaml").stat().st_mode & 0o777 == mode


def test_renderer_rejects_a_symlinked_config_descriptor(tmp_path):
    """A path swap cannot make the renderer validate one config and deploy another."""
    app_dir = _make_app(tmp_path)
    real_config = app_dir / "real-config.yaml"
    real_config.write_text("safe: true\n")
    os.chmod(real_config, 0o600)
    config = app_dir / "config.yaml"
    config.unlink()
    config.symlink_to(real_config)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert not output.exists()


def test_renderer_rerender_is_byte_and_inode_identical_for_identical_inputs(tmp_path):
    """A no-op re-render avoids unnecessary service-file churn."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    first = _run_renderer(app_dir, output)
    before = output.stat()

    second = _run_renderer(app_dir, output)
    after = output.stat()

    assert first.returncode == second.returncode == 0
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_renderer_leaves_the_previous_complete_output_on_atomic_replace_failure(
    tmp_path, monkeypatch
):
    """An interrupted install may retain the old unit but must never truncate it."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    previous = b"[Unit]\nDescription=previous-complete-unit\n"
    output.write_bytes(previous)
    renderer = _load_renderer_module()

    def fail_replace(source, destination):
        raise OSError("simulated replace interruption")

    monkeypatch.setattr(renderer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace interruption"):
        renderer.render_system_service(
            user="pi",
            app_dir=app_dir,
            display=":0",
            xauthority=Path("/home/pi/.Xauthority"),
            output=output,
        )

    assert output.read_bytes() == previous
    assert not list(tmp_path.glob(".vinyl-now-playing.service.*.tmp"))


def test_renderer_refuses_to_replace_an_output_symlink(tmp_path):
    """The explicit output path cannot be used to replace a symlinked target."""
    app_dir = _make_app(tmp_path)
    protected = tmp_path / "protected.service"
    protected.write_text("do-not-replace\n")
    output = tmp_path / "vinyl-now-playing.service"
    output.symlink_to(protected)
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert protected.read_text() == "do-not-replace\n"
    assert output.is_symlink()


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is required only for Linux system-unit parsing",
)
def test_rendered_unit_passes_systemd_analyze_verify_when_available(tmp_path):
    """The real parser, not source text, accepts the representative unit."""
    app_dir = _make_app(tmp_path)
    python_path = app_dir / "venv" / "bin" / "python3"
    python_path.parent.mkdir(parents=True)
    python_path.symlink_to(Path(sys.executable))
    (app_dir / "main.py").write_text("print('representative app')\n")
    xauthority = tmp_path / "Xauthority"
    xauthority.write_text("")
    output = tmp_path / "vinyl-now-playing.service"

    completed = _run_renderer(app_dir, output, xauthority=str(xauthority))
    verified = subprocess.run(
        ["systemd-analyze", "verify", str(output)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert verified.returncode == 0, verified.stderr
