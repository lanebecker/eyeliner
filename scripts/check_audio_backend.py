"""Fail fast when the installed sounddevice backend cannot support production."""
import importlib
from importlib import metadata
from pathlib import Path
import sys


REQUIRED_APIS = (
    "query_devices",
    "InputStream",
    "_terminate",
    "_initialize",
)


def validate_backend(module):
    """Return production-required sounddevice APIs that are absent or unusable."""
    return tuple(
        api for api in REQUIRED_APIS if not callable(getattr(module, api, None))
    )


def _resolve_file(path):
    """Resolve a readable file without letting provenance diagnostics raise."""
    try:
        return Path(path).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None


def validate_distribution_provenance(module, distribution_loader=metadata.distribution):
    """Return a fail-closed reason unless ``module`` is from sounddevice's wheel."""
    module_file = _resolve_file(getattr(module, "__file__", None))
    if module_file is None:
        return "imported module has no readable file origin"

    try:
        distribution = distribution_loader("sounddevice")
    except metadata.PackageNotFoundError:
        return "installed sounddevice distribution metadata is unavailable"
    except Exception:
        return "installed sounddevice distribution metadata could not be read"

    try:
        distribution_files = distribution.files
        if not distribution_files:
            return "installed sounddevice distribution file manifest is unavailable"
        declared_files = {
            path
            for distribution_file in distribution_files
            if (path := _resolve_file(distribution.locate_file(distribution_file)))
            is not None
        }
    except Exception:
        return "installed sounddevice distribution file manifest could not be read"
    if module_file not in declared_files:
        return "imported module is not declared by the installed distribution"

    return None


def main():
    """Import and validate sounddevice without touching an audio device."""
    try:
        backend = importlib.import_module("sounddevice")
    except Exception as error:
        print(
            "ERROR: unable to import the sounddevice audio backend "
            f"({type(error).__name__}). Install libportaudio2 and rerun this check.",
            file=sys.stderr,
        )
        return 1

    provenance_error = validate_distribution_provenance(backend)
    if provenance_error:
        print(
            "ERROR: cannot verify sounddevice backend provenance: "
            f"{provenance_error}. Remove local shadows and repair the installed "
            "sounddevice package before rerunning this check.",
            file=sys.stderr,
        )
        return 1

    missing_apis = validate_backend(backend)
    if missing_apis:
        print(
            "ERROR: installed sounddevice backend is missing required callable APIs: "
            f"{', '.join(missing_apis)}. Pin or repair the sounddevice package before "
            "running the application.",
            file=sys.stderr,
        )
        return 1

    version = getattr(backend, "__version__", "unknown")
    print(f"sounddevice {version}: required audio backend APIs are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
