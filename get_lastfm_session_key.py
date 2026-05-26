"""One-time helper: generate a Last.fm session key via the desktop auth flow.

Run this once, approve access in the browser, then paste the printed session key
into config.yaml under `lastfm.session_key`. You never need to run it again —
session keys don't expire.

Usage:
    python get_lastfm_session_key.py

Requirements:
    pip install pylast   (or: already listed in requirements.txt for v1.3.0+)
"""

import sys
import webbrowser

try:
    import pylast
except ImportError:
    print("pylast is not installed. Run: pip install pylast")
    sys.exit(1)


def main():
    print("=" * 60)
    print("  Last.fm Session Key Generator — vinyl-now-playing")
    print("=" * 60)
    print()
    print("You'll need the API key and shared secret from your Last.fm")
    print("API account: https://www.last.fm/api/account/create")
    print()

    api_key    = input("API key:       ").strip()
    api_secret = input("Shared secret: ").strip()

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

    print()
    print("=" * 60)
    print("  ✅  Success!")
    print("=" * 60)
    print(f"\nYour session key:\n  {session_key}")
    print()
    print("Add the following to config.yaml:")
    print()
    print("lastfm:")
    print(f'  session_key: "{session_key}"')
    print()
    print("(Keep this key private — it grants write access to your Last.fm account.)")


if __name__ == "__main__":
    main()
