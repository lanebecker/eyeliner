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
        "xauthority": str(base_output.parent / "Xauthority"),
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
        "xauthority": str(Path(base_output).parent / "Xauthority"),
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
Environment=\"XAUTHORITY={xauthority}\"
ExecStart={app_dir}/venv/bin/python3 {app_dir}/main.py
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=15
TimeoutStopSec=30

[Install]
WantedBy=graphical.target
""".format(app_dir=app_dir, xauthority=output.parent / "Xauthority")
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


def test_renderer_rejects_a_double_slash_alias_to_config_before_reading_it(
    tmp_path, monkeypatch
):
    """A lexical `//` alias cannot replace or expose the credential file."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    original = config.read_bytes()
    original_mode = config.stat().st_mode & 0o777
    output = "//" + str(config).lstrip("/")
    renderer = _load_renderer_module()

    monkeypatch.setattr(
        renderer.os,
        "read",
        lambda *_args: pytest.fail("renderer read the config through an output alias"),
    )
    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert config.read_bytes() == original
    assert config.stat().st_mode & 0o777 == original_mode == 0o600


def test_renderer_rejects_a_double_slash_alias_to_its_template(tmp_path, monkeypatch):
    """A noncanonical template path cannot be atomically replaced as output."""
    app_dir = _make_app(tmp_path)
    renderer = _load_renderer_module()
    template = tmp_path / "template.service.in"
    template.write_text(
        """[Unit]
Description=vinyl-now-playing
User=@SERVICE_USER@
WorkingDirectory=@APP_DIR@
Environment=\"DISPLAY=@DISPLAY@\"
Environment=\"XAUTHORITY=@XAUTHORITY@\"
ExecStart=@APP_DIR@/venv/bin/python3 @APP_DIR@/main.py
"""
    )
    original = template.read_bytes()
    monkeypatch.setattr(renderer, "TEMPLATE", template)
    monkeypatch.setattr(
        renderer.os,
        "read",
        lambda *_args: pytest.fail("renderer read the template through an output alias"),
    )

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, "//" + str(template).lstrip("/"))

    assert template.read_bytes() == original


@pytest.mark.parametrize("target_name", ("config", "template"))
def test_renderer_rejects_an_existing_output_hardlink_before_reading_it(
    tmp_path, monkeypatch, target_name
):
    """A hardlink cannot turn the output read into a credential/template read."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    renderer = _load_renderer_module()
    target = config if target_name == "config" else renderer.TEMPLATE
    output = tmp_path / "vinyl-now-playing.service"
    os.link(target, output)
    original = target.read_bytes()

    monkeypatch.setattr(
        renderer.os,
        "read",
        lambda *_args: pytest.fail("renderer read an identity-colliding output"),
    )
    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert target.read_bytes() == original


def _template_with_secret_marker(marker: str) -> str:
    return SCRIPT.parent.parent.joinpath("deploy", "vinyl-now-playing.service.in").read_text() + marker


@pytest.mark.parametrize("swap_kind", ("regular", "config-symlink"))
def test_renderer_rejects_template_swaps_before_the_template_descriptor_is_read(
    tmp_path, monkeypatch, swap_kind
):
    """A post-check template swap cannot publish attacker or config bytes."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    config.write_text(_template_with_secret_marker("\n# config-secret-marker\n"))
    os.chmod(config, 0o600)
    renderer = _load_renderer_module()
    template = tmp_path / "template.service.in"
    template.write_text(_template_with_secret_marker("\n# trusted-template\n"))
    output = tmp_path / "vinyl-now-playing.service"
    monkeypatch.setattr(renderer, "TEMPLATE", template)
    original_open = renderer.os.open
    swapped = False

    def swap_before_template_open(path, flags, mode=0o777):
        nonlocal swapped
        if Path(path) == template and not swapped:
            swapped = True
            template.unlink()
            if swap_kind == "regular":
                template.write_text(_template_with_secret_marker("\n# raced-template\n"))
            else:
                template.symlink_to(config)
        return original_open(path, flags, mode)

    monkeypatch.setattr(renderer.os, "open", swap_before_template_open)

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert swapped
    assert not output.exists()
    assert config.read_text().endswith("# config-secret-marker\n")
    assert config.stat().st_mode & 0o777 == 0o600


def test_renderer_rejects_a_template_that_is_already_a_hardlink_to_config(tmp_path, monkeypatch):
    """The template descriptor itself can never be the private config inode."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    config.write_text(_template_with_secret_marker("\n# config-secret-marker\n"))
    os.chmod(config, 0o600)
    renderer = _load_renderer_module()
    template = tmp_path / "template.service.in"
    os.link(config, template)
    output = tmp_path / "vinyl-now-playing.service"
    monkeypatch.setattr(renderer, "TEMPLATE", template)

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert not output.exists()
    assert config.read_text().endswith("# config-secret-marker\n")
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target_name", ("config", "template"))
def test_renderer_rejects_canonical_output_collisions_after_identity_races(
    tmp_path, monkeypatch, target_name
):
    """A path-string collision remains forbidden if the target inode changes."""
    app_dir = _make_app(tmp_path)
    config = app_dir / "config.yaml"
    renderer = _load_renderer_module()
    template = tmp_path / "template.service.in"
    template.write_text(_template_with_secret_marker("\n# trusted-template\n"))
    monkeypatch.setattr(renderer, "TEMPLATE", template)
    output = config if target_name == "config" else template
    replacement = _template_with_secret_marker("\n# replacement-must-not-be-overwritten\n").encode()
    original_template_metadata = renderer._template_metadata

    def replace_target_after_first_identity_check():
        metadata = original_template_metadata()
        output.unlink()
        output.write_bytes(replacement)
        os.chmod(output, 0o600)
        return metadata

    monkeypatch.setattr(renderer, "_template_metadata", replace_target_after_first_identity_check)

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output)

    assert output.read_bytes() == replacement


def test_renderer_reads_the_template_only_from_its_verified_descriptor(tmp_path, monkeypatch):
    """A path-level `read_text()` call would reintroduce the template TOCTOU gap."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    monkeypatch.setattr(
        renderer.Path,
        "read_text",
        lambda *_args, **_kwargs: pytest.fail("renderer reopened the template by path"),
    )

    assert _render_direct(renderer, app_dir, output) is True
    assert output.exists()


def test_renderer_cli_normalizes_invalid_template_utf8_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """A corrupted checked-in unit template gives operators one concise error."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()
    template = tmp_path / "template.service.in"
    template.write_bytes(b"\xff\xfe")
    monkeypatch.setattr(renderer, "TEMPLATE", template)

    exit_code = renderer.main(_arguments(app_dir, output))
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err.startswith("error: cannot decode system-service template")
    assert "Traceback" not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("unsafe", ("%n", "$HOME", "'", '\"', "\\\\", "@", ";", "&", "|"))
def test_renderer_rejects_systemd_metacharacters_in_unit_bound_paths(tmp_path, unsafe):
    """Unit substitutions stay literal rather than invoking systemd expansion rules."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    xauthority = tmp_path / f"Xauthority{unsafe}"
    xauthority.write_text("")
    renderer = _load_renderer_module()

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output, xauthority=str(xauthority))

    assert not output.exists()


@pytest.mark.parametrize("kind", ("app", "output-parent", "xauthority-parent"))
def test_renderer_rejects_symlinks_in_existing_path_ancestors(tmp_path, kind):
    """Every existing caller-controlled path component is stable at render time."""
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    alias_root = tmp_path / "alias-root"
    alias_root.symlink_to(real_root, target_is_directory=True)
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    xauthority = tmp_path / "Xauthority"
    xauthority.write_text("")
    renderer = _load_renderer_module()

    if kind == "app":
        real_app = _make_app(real_root)
        app_dir = alias_root / real_app.name
    elif kind == "output-parent":
        output = alias_root / "vinyl-now-playing.service"
    else:
        xauthority = alias_root / "Xauthority"

    with pytest.raises(renderer.RenderError):
        _render_direct(renderer, app_dir, output, xauthority=str(xauthority))

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


@pytest.mark.parametrize("mode", (0o600, 0o666, 0o777))
def test_renderer_atomically_replaces_equal_content_with_an_unsafe_mode(tmp_path, mode):
    """Content alone is not current when the deployment artifact is writable."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()
    _render_direct(renderer, app_dir, output)
    before = output.stat()
    os.chmod(output, mode)

    changed = _render_direct(renderer, app_dir, output)
    after = output.stat()

    assert changed is True
    assert after.st_mode & 0o777 == 0o644
    assert after.st_ino != before.st_ino


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

    with pytest.raises(renderer.RenderError, match="cannot atomically write output"):
        renderer.render_system_service(
            user="pi",
            app_dir=app_dir,
            display=":0",
            xauthority=tmp_path / "Xauthority",
            output=output,
        )

    assert output.read_bytes() == previous
    assert not list(tmp_path.glob(".vinyl-now-playing.service.*.tmp"))


def test_renderer_fsyncs_the_containing_directory_after_atomic_replace(tmp_path, monkeypatch):
    """Power loss after rename cannot discard the otherwise-complete unit entry."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()
    fsync_calls = []

    monkeypatch.setattr(renderer.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))

    _render_direct(renderer, app_dir, output)

    assert len(fsync_calls) == 2


def test_renderer_cli_normalizes_atomic_write_failures_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Operators get one concise actionable failure if a write cannot complete."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()

    monkeypatch.setattr(
        renderer.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("simulated denied write")),
    )

    exit_code = renderer.main(_arguments(app_dir, output))
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err.startswith("error: cannot atomically write output")
    assert "Traceback" not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("failure", ("read", "close"))
def test_renderer_cli_normalizes_existing_output_io_failures_without_a_traceback(
    tmp_path, monkeypatch, capsys, failure
):
    """A verification read failure cannot escape the renderer's CLI boundary."""
    app_dir = _make_app(tmp_path)
    output = tmp_path / "vinyl-now-playing.service"
    renderer = _load_renderer_module()
    _render_direct(renderer, app_dir, output)

    original_open = renderer.os.open
    original_close = renderer.os.close
    output_descriptor = None

    def remember_output_descriptor(path, flags, mode=0o777):
        nonlocal output_descriptor
        descriptor = original_open(path, flags, mode)
        if Path(path) == output:
            output_descriptor = descriptor
        return descriptor

    monkeypatch.setattr(renderer.os, "open", remember_output_descriptor)

    if failure == "read":
        original_read = renderer.os.read

        def fail_output_read(descriptor, *args):
            if descriptor == output_descriptor:
                raise OSError("simulated read failure")
            return original_read(descriptor, *args)

        monkeypatch.setattr(
            renderer.os,
            "read",
            fail_output_read,
        )
    else:
        def fail_output_close(descriptor):
            if descriptor == output_descriptor:
                raise OSError("simulated close failure")
            return original_close(descriptor)

        monkeypatch.setattr(renderer.os, "close", fail_output_close)

    exit_code = renderer.main(_arguments(app_dir, output))
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err.startswith("error: cannot safely read output")
    assert "Traceback" not in captured.err


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
