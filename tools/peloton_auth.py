#!/usr/bin/env python3
"""Stay signed in to Peloton without copying a token every two days.

Peloton's website signs in through Auth0 (auth.onepeloton.com). Besides the 48-hour access token it
keeps a refresh token, which can be swapped for a new access token whenever one is needed. Peloton
hands out a new refresh token with each swap (rotation); this saves it for the next time.

    python3 tools/peloton_auth.py setup     once: paste your sign-in (hidden), checked and saved
    python3 tools/peloton_auth.py test      check it: shows your Peloton username
    python3 tools/peloton_auth.py forget    delete the saved sign-in

Getting the sign-in to paste: open a PRIVATE browser window, sign in at members.onepeloton.com,
press F12, open the Console and paste this line (Chrome may ask you to type "allow pasting" first):

    copy(Object.entries(localStorage).filter(([k]) => k.startsWith("@@auth0spajs@@") && k.includes("api.onepeloton")).map(([k, v]) => { const b = JSON.parse(v).body; return b.client_id + " " + b.refresh_token; })[0])

It copies two values (the website's client id and your refresh token) to the clipboard. Paste them
at the prompt, then CLOSE the private window without signing out: signing out cancels the refresh
token, and if that window renewed its own copy later, Auth0 would treat both as stolen and cancel them.

The saved sign-in is in ~/.config/smartrow-bridge/peloton-auth.json, readable by you only, never
printed, and left out of the Pi's backups. It is Peloton's unofficial API: it can change.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://auth.onepeloton.com/oauth/token"
API = "https://api.onepeloton.com"
AUTH_FILE = os.environ.get("SRB_PELOTON_AUTH") or str(Path.home() / ".config" / "smartrow-bridge" / "peloton-auth.json")
RENEW_BEFORE_S = 3600  # renew an access token with less than an hour left
SETUP = "python3 tools/peloton_auth.py setup"


class SignInNeeded(Exception):
    """No saved sign-in, or Peloton no longer accepts it: run setup again."""


def parse_entry(text: str) -> tuple[str, str]:
    """(client id, refresh token) from what was pasted: the console line's "client_id refresh_token",
    or the whole Local Storage entry (its value, or key and value)."""
    text = text.strip()
    if "{" in text:
        start, end = text.find("{"), text.rfind("}")
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            raise ValueError("That looks like JSON but isn't complete; use the console line instead.") from None
        body = obj.get("body", obj) if isinstance(obj, dict) else {}
        refresh, client = body.get("refresh_token"), body.get("client_id")
        if not client:
            m = re.search(r"@@auth0spajs@@::([^:]+)::", text)
            client = m.group(1) if m else None
        if not refresh:
            raise ValueError("No refresh token in it: the website may not keep one for this sign-in.")
        if not client:
            raise ValueError("No client id in it: paste the console line's output instead.")
        return client, refresh
    parts = text.split()
    if len(parts) != 2 or parts[0] in ("undefined", "null") or parts[1] in ("undefined", "null"):
        raise ValueError("Expected two values (client id, refresh token). If the console line printed "
                         "'undefined', the website keeps no refresh token: tell Claude.")
    return parts[0], parts[1]


def _load(path: str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("refresh_token") and data.get("client_id") else None


def _save(path: str, data: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)


@contextlib.contextmanager
def _locked(path: str):
    """One renewal at a time (the after-row and weekly jobs may meet): rotation makes a race costly."""
    lock = Path(path).with_name(Path(path).name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def exchange(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> dict:
    """Swap a refresh token for a new access token (and, with rotation, a new refresh token)."""
    body = json.dumps({"grant_type": "refresh_token", "client_id": client_id,
                       "refresh_token": refresh_token}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener(req, timeout=30) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):  # invalid_grant: expired, revoked or already used
            raise SignInNeeded(f"Peloton no longer accepts the saved sign-in (HTTP {exc.code}); "
                               f"run: {SETUP}") from None
        raise
    if not reply.get("access_token"):
        raise SignInNeeded(f"Peloton's sign-in reply had no access token; run: {SETUP}")
    return reply


def access_token(path: str = AUTH_FILE, now: float | None = None, opener=urllib.request.urlopen) -> str:
    """A valid access token: the saved one while it has an hour left, else a renewed one (saved first,
    with the new refresh token, before it is used)."""
    with _locked(path):
        data = _load(path)
        if not data:
            raise SignInNeeded(f"No saved Peloton sign-in; run: {SETUP}")
        now = time.time() if now is None else now
        if data.get("access_token") and data.get("expires_at", 0) - now > RENEW_BEFORE_S:
            return data["access_token"]
        reply = exchange(data["client_id"], data["refresh_token"], opener)
        data.update({"access_token": reply["access_token"],
                     "refresh_token": reply.get("refresh_token") or data["refresh_token"],
                     "expires_at": now + float(reply.get("expires_in") or 86400), "renewed_at": now})
        _save(path, data)
        return data["access_token"]


def _username(token: str) -> str:
    req = urllib.request.Request(f"{API}/api/me", headers={"Authorization": f"Bearer {token}",
                                                           "Peloton-Platform": "web", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        me = json.loads(r.read().decode("utf-8"))
    return me.get("username") or me.get("email") or "(no username)"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=["setup", "test", "forget"])
    p.add_argument("--file", default=AUTH_FILE)
    args = p.parse_args(argv)
    if args.cmd == "forget":
        with contextlib.suppress(FileNotFoundError):
            os.remove(args.file)
        print("Saved Peloton sign-in deleted.")
        return
    if args.cmd == "setup":
        try:
            client, refresh = parse_entry(getpass.getpass("Paste the two values (hidden): "))
        except ValueError as exc:
            sys.exit(str(exc))
        with _locked(args.file):
            try:
                reply = exchange(client, refresh)
            except SignInNeeded:
                sys.exit("Peloton didn't accept that refresh token. Copy it again from a fresh private-window "
                         "sign-in, and paste it straight away.")
            _save(args.file, {"client_id": client, "refresh_token": reply.get("refresh_token") or refresh,
                              "access_token": reply["access_token"],
                              "expires_at": time.time() + float(reply.get("expires_in") or 86400),
                              "renewed_at": time.time()})
        print(f"Saved. Signed in to Peloton as {_username(reply['access_token'])}.")
        print("Close the private browser window now (without signing out).")
        return
    try:
        token = access_token(args.file)
    except SignInNeeded as exc:
        sys.exit(str(exc))
    print(f"Peloton sign-in works: {_username(token)}.")


if __name__ == "__main__":
    main()
