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
_SAFE_ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9._/-]*\Z")


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
    if not _SAFE_ABSOLUTE_PATH.fullmatch(text):
        raise RenderError(f"{name} contains characters unsafe for a systemd unit")
    if "//" in text:
        raise RenderError(f"{name} must not contain noncanonical double slashes")
    path = Path(text)
    if not path.is_absolute():
        raise RenderError(f"{name} must be an absolute path")
    if any(part in {".", ".."} for part in text.split("/")[1:]):
        raise RenderError(f"{name} must not contain relative path components")
    return path


def _directory(path: Path, name: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise RenderError(f"{name} does not exist: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RenderError(f"{name} must be a real directory: {path}")


def _reject_symlink_ancestors(path: Path, name: str) -> None:
    """Reject every existing caller-controlled component, not only the leaf."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise RenderError(f"cannot inspect {name}: {current}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise RenderError(f"{name} must not contain a symlink: {current}")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_config(config_path: Path) -> os.stat_result:
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
    return metadata


def _template_metadata() -> os.stat_result:
    _reject_symlink_ancestors(TEMPLATE, "template")
    try:
        metadata = os.lstat(TEMPLATE)
    except OSError as error:
        raise RenderError(f"cannot inspect system-service template: {TEMPLATE}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RenderError(f"system-service template must be a regular file: {TEMPLATE}")
    return metadata


def _validate_output(
    output: Path, config: os.stat_result, template: os.stat_result
) -> os.stat_result | None:
    _directory(output.parent, "output parent")
    try:
        metadata = os.lstat(output)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RenderError(f"cannot inspect output: {output}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise RenderError(f"output must not be a symlink: {output}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RenderError(f"output must be a regular file: {output}")
    if _same_file(metadata, config):
        raise RenderError("output must not refer to config.yaml")
    if _same_file(metadata, template):
        raise RenderError("output must not refer to the unit template")
    return metadata


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


def _existing_output_matches(
    output: Path,
    rendered: bytes,
    expected: os.stat_result | None,
    config: os.stat_result,
    template: os.stat_result,
) -> bool:
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
        if _same_file(metadata, config):
            raise RenderError("output must not refer to config.yaml")
        if _same_file(metadata, template):
            raise RenderError("output must not refer to the unit template")
        if expected is not None and not _same_file(metadata, expected):
            raise RenderError(f"output changed while it was being rendered: {output}")
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            return False
        chunks = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks) == rendered
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(output: Path, rendered: bytes) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    write_error: OSError | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o644)
        unit_file = os.fdopen(descriptor, "wb")
        descriptor = None
        with unit_file:
            unit_file.write(rendered)
            unit_file.flush()
            os.fsync(unit_file.fileno())
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    except OSError as error:
        write_error = error
        raise RenderError(f"cannot atomically write output: {output}") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                if write_error is None:
                    raise RenderError(f"cannot close temporary output: {output}") from error
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as error:
                if write_error is None:
                    raise RenderError(f"cannot remove temporary output: {output}") from error


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

    _reject_symlink_ancestors(app_dir, "app-dir")
    _reject_symlink_ancestors(xauthority, "xauthority")
    _reject_symlink_ancestors(output, "output")
    _directory(app_dir, "app-dir")
    config_path = app_dir / "config.yaml"
    config = _validate_config(config_path)
    template = _template_metadata()
    existing_output = _validate_output(output, config, template)

    rendered = _render_template(
        {
            "@SERVICE_USER@": user,
            "@APP_DIR@": str(app_dir),
            "@DISPLAY@": display,
            "@XAUTHORITY@": str(xauthority),
        }
    )
    if _existing_output_matches(output, rendered, existing_output, config, template):
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
