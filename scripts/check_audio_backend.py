"""Fail fast when the installed sounddevice backend cannot support production."""
import importlib
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
