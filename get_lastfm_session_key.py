"""One-time helper: generate a Last.fm session key via the desktop auth flow.

Run this once, approve access in the browser, then paste the printed session key
into config.yaml under `lastfm.session_key`. You never need to run it again —
session keys don't expire.

Usage:
    python get_lastfm_session_key.py

Requirements:
    pip install pylast   (or: already listed in requirements.txt for v1.3.0+)
"""

import getpass
import os
import stat
import sys
import webbrowser

try:
    import pylast
except ImportError:
    print("pylast is not installed. Run: pip install pylast")
    sys.exit(1)

# Where we drop the generated session key.  Written with 0600 perms instead of
# printed to stdout: the key grants write access to the user's Last.fm account,
# and anything echoed to a terminal lingers in scrollback, terminal logs, and
# screen recordings (finding S-3).
_KEY_OUTPUT_FILE = "lastfm_session_key.txt"


def main():
    print("=" * 60)
    print("  Last.fm Session Key Generator — vinyl-now-playing")
    print("=" * 60)
    print()
    print("You'll need the API key and shared secret from your Last.fm")
    print("API account: https://www.last.fm/api/account/create")
    print()

    # The API key is a public identifier, so a plain prompt is fine.  The shared
    # secret is a credential — read it with getpass so it is NOT echoed to the
    # screen or captured in shell scrollback (finding S-3).
    api_key    = input("API key:       ").strip()
    api_secret = getpass.getpass("Shared secret (hidden): ").strip()

    if not api_key or not api_secret:
        print("\nError: both fields are required.")
        sys.exit(1)

    try:
        network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
        skg = pylast.SessionKeyGenerator(network)
        url = skg.get_web_auth_url()
    except Exception as e:
        print(f"\nFailed to reach Last.fm API: {e}")
        sys.exit(1)

    print()
    print("Opening Last.fm authorisation page in your browser...")
    print(f"If it doesn't open automatically, visit:\n  {url}")
    webbrowser.open(url)

    print()
    input("After approving access on the Last.fm page, press Enter here... ")

    try:
        session_key = skg.get_web_auth_session_key(url)
    except pylast.WSError as e:
        print(f"\nLast.fm returned an error: {e}")
        print("Make sure you clicked 'Allow access' on the Last.fm page before pressing Enter.")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error retrieving session key: {e}")
        sys.exit(1)

    # Write the key to a 0600 file rather than printing it.  A session key is a
    # write-scope credential; echoing it to stdout would leave it in scrollback,
    # terminal logs, and screen recordings (finding S-3).
    try:
        # Create (or truncate) the file with owner-only read/write from the start,
        # so the secret is never briefly world-readable between write and chmod.
        fd = os.open(
            _KEY_OUTPUT_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,  # 0600
        )
        with os.fdopen(fd, "w") as f:
            f.write(session_key + "\n")
        # Re-assert perms in case the file already existed with looser modes.
        os.chmod(_KEY_OUTPUT_FILE, stat.S_IRUSR | stat.S_IWUSR)
        key_path = os.path.abspath(_KEY_OUTPUT_FILE)
    except OSError as e:
        print(f"\nGenerated the session key but could not write it to disk: {e}")
        print("Re-run this script, or capture the key from a secure prompt.")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  ✅  Success!")
    print("=" * 60)
    print()
    print(f"Your session key was written (0600, owner-only) to:\n  {key_path}")
    print()
    print("Add it to config.yaml under the `lastfm` section, e.g.:")
    print()
    print("lastfm:")
    print('  session_key: "<paste the contents of the file above>"')
    print()
    print("Then delete the file — and note that the key grants WRITE access to")
    print("your Last.fm account, so keep it out of shell history and backups.")


if __name__ == "__main__":
    main()
