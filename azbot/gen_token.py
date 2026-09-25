#!/usr/bin/env python3
"""One-time helper: create a Drive refresh token for the bot from your PC.

    python gen_token.py <name> [client_id] [client_secret]

Uses the out-of-browser (loopback-less) copy/paste flow that works with ANY
desktop-type OAuth client — including ones that reject device sign-in codes.
Then either:
  • paste the printed refresh token to the bot via /tokenup <name>, or
  • add it to .env as GDrive_<name>_REFRESH_TOKEN.
"""
import sys, json, urllib.parse, urllib.request

def main():
    if len(sys.argv) < 2:
        print("usage: python gen_token.py <name> [client_id] [client_secret]")
        sys.exit(1)
    name = sys.argv[1].strip().lower()
    # client creds: args > env-style prompt
    cid = sys.argv[2] if len(sys.argv) > 2 else input("Client ID: ").strip()
    csec = sys.argv[3] if len(sys.argv) > 3 else input("Client secret: ").strip()

    # Google shut down the legacy urn:ietf:wg:oauth:2.0:oob redirect
    # ("Error 400: redirect_uri_mismatch"). Same copy/paste workaround the
    # bot itself uses: redirect to a throwaway localhost port; the browser
    # lands on a dead page whose ADDRESS BAR holds ?code=... — copy that.
    redirect = "http://localhost:1/"
    auth_url = ("https://accounts.google.com/o/oauth2/auth?"
                + urllib.parse.urlencode({
                    "client_id": cid,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": "https://www.googleapis.com/auth/drive",
                    "access_type": "offline",
                    "prompt": "consent",
                  }))
    print("\n1. Open this URL in your browser:\n\n" + auth_url)
    print("\n2. Approve, then you'll land on a page that WON'T LOAD — that's normal.")
    raw = input("   Copy the full URL from the address bar (contains ?code=...) and paste it here: ").strip()
    import re as _re
    m = _re.search(r"[?&]code=([\w\-]+)", raw)
    code = m.group(1) if m else raw.split()[0]

    data = urllib.parse.urlencode({
        "code": code, "client_id": cid, "client_secret": csec,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data) as r:
        tok = json.loads(r.read())
    if "refresh_token" not in tok:
        print("\nNo refresh_token in response:", tok)
        sys.exit(1)
    print(f"\n✅ Success! Add this to .env as:\n"
          f"GDrive_{name}_REFRESH_TOKEN={tok['refresh_token']}\n"
          f"(or /tokenup {name} on the bot and paste it there)")

if __name__ == "__main__":
    main()
