#!/usr/bin/env python3
"""Render the supported vinyl-now-playing systemd unit without installing it."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Mapping, Sequence


TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "vinyl-now-playing.service.in"
_PLACEHOLDER_COUNTS = {
    "@SERVICE_USER@": 1,
    "@APP_DIR@": 3,
    "@DISPLAY@": 1,
    "@XAUTHORITY@": 1,
}
_SERVICE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_DISPLAY = re.compile(r":[0-9]+(?:\.[0-9]+)?\Z")


class RenderError(ValueError):
    """Raised when a service unit cannot be rendered safely."""


def _safe_text(name: str, value: str | os.PathLike[str]) -> str:
    text = os.fspath(value)
    if not isinstance(text, str) or not text:
        raise RenderError(f"{name} must be a non-empty string")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        raise RenderError(f"{name} must not contain whitespace or control characters")
    return text


def _absolute_path(name: str, value: str | os.PathLike[str]) -> Path:
    text = _safe_text(name, value)
    path = Path(text)
    if not path.is_absolute():
        raise RenderError(f"{name} must be an absolute path")
    if any(part in {".", ".."} for part in path.parts):
        raise RenderError(f"{name} must not contain relative path components")
    return path


def _directory(path: Path, name: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise RenderError(f"{name} does not exist: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RenderError(f"{name} must be a real directory: {path}")


def _validate_config(config_path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RenderError("rendering requires POSIX O_NOFOLLOW support for config.yaml")

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(config_path, flags)
    except OSError as error:
        raise RenderError(f"cannot safely open config.yaml: {config_path}") from error
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if not stat.S_ISREG(metadata.st_mode):
        raise RenderError(f"config.yaml must be a regular file: {config_path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RenderError(f"config.yaml must have mode 0600: {config_path}")


def _validate_output(output: Path, config_path: Path) -> None:
    _directory(output.parent, "output parent")
    if output == config_path:
        raise RenderError("output must not replace config.yaml")
    if output == TEMPLATE:
        raise RenderError("output must not replace the unit template")
    try:
        metadata = os.lstat(output)
    except FileNotFoundError:
        return
    except OSError as error:
        raise RenderError(f"cannot inspect output: {output}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise RenderError(f"output must not be a symlink: {output}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RenderError(f"output must be a regular file: {output}")


def _render_template(values: Mapping[str, str]) -> bytes:
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
    except OSError as error:
        raise RenderError(f"cannot read system-service template: {TEMPLATE}") from error

    for placeholder, expected_count in _PLACEHOLDER_COUNTS.items():
        if template.count(placeholder) != expected_count:
            raise RenderError(f"invalid system-service template placeholder: {placeholder}")
        template = template.replace(placeholder, values[placeholder])
    if "@" in template:
        raise RenderError("system-service template contains an unknown placeholder")
    return template.encode("utf-8")


def _existing_output_matches(output: Path, rendered: bytes) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RenderError(f"cannot safely read output: {output}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RenderError(f"output must be a regular file: {output}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks) == rendered
    finally:
        os.close(descriptor)


def _atomic_write(output: Path, rendered: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as unit_file:
            unit_file.write(rendered)
            unit_file.flush()
            os.fsync(unit_file.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_system_service(
    *,
    user: str,
    app_dir: str | os.PathLike[str],
    display: str,
    xauthority: str | os.PathLike[str],
    output: str | os.PathLike[str],
) -> bool:
    """Render a validated unit and return whether it replaced the output file."""
    user = _safe_text("user", user)
    if not _SERVICE_USER.fullmatch(user):
        raise RenderError("user must be a safe system account name")
    display = _safe_text("display", display)
    if not _DISPLAY.fullmatch(display):
        raise RenderError("display must be an X display such as :0")
    app_dir = _absolute_path("app-dir", app_dir)
    xauthority = _absolute_path("xauthority", xauthority)
    output = _absolute_path("output", output)

    _directory(app_dir, "app-dir")
    config_path = app_dir / "config.yaml"
    _validate_config(config_path)
    _validate_output(output, config_path)

    rendered = _render_template(
        {
            "@SERVICE_USER@": user,
            "@APP_DIR@": str(app_dir),
            "@DISPLAY@": display,
            "@XAUTHORITY@": str(xauthority),
        }
    )
    if _existing_output_matches(output, rendered):
        return False
    _atomic_write(output, rendered)
    return True


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="system account that runs the service")
    parser.add_argument("--app-dir", required=True, help="absolute application directory")
    parser.add_argument("--display", required=True, help="X display such as :0")
    parser.add_argument("--xauthority", required=True, help="absolute Xauthority path")
    parser.add_argument("--output", required=True, help="absolute rendered unit path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        changed = render_system_service(
            user=arguments.user,
            app_dir=arguments.app_dir,
            display=arguments.display,
            xauthority=arguments.xauthority,
            output=arguments.output,
        )
    except RenderError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print("rendered system service" if changed else "system service already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
