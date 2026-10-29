# SPDX-License-Identifier: GPL-3.0-only
"""Optional: upload each session to Strava, with the coach's summary as its description.

Replaces the SmartRow app's own Strava link (switch that off once this works, or each
session arrives twice). Uses your own Strava API application: Strava gives every account
with a subscription one, in "single-player mode" (only your account can use it), which is
all this needs.

Set up once with tools/setup_strava.sh (python -m smartrow_bridge.strava --setup):
1. create the application at https://www.strava.com/settings/api (Authorization Callback
   Domain: localhost), note its Client ID and Client Secret;
2. paste them when asked, open the link it prints, press Authorize;
3. the browser then fails to load a "localhost" page: copy that page's address (it holds a
   one-time code) and paste it back.

The Client Secret and the tokens are kept in /etc/smartrow-bridge/strava.json, readable by
root only. Strava's access tokens last six hours; the Pi renews them itself.

Uploads go through Strava's upload API: the session as a TCX file (tcxfile.py), marked as
indoor (trainer). Like the SmartRow app's own uploads, the file names no device. Strava
doesn't show a personal API app's name on the activity, so the description starts with
"Rowed using SmartRow" (intervals.note_for). The activity type (SRB_STRAVA_SPORT_TYPE, Rowing) is set on the
activity once it is there. Strava itself turns away an exact duplicate; that counts as done.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

from . import webapi

log = logging.getLogger(__name__)

DEFAULT_FILE = "/etc/smartrow-bridge/strava.json"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
REDIRECT_URI = "http://localhost/exchange_token"
SCOPE = "read,activity:write"
REFRESH_MARGIN_S = 300  # renew a token this long before it expires
POLL_S = 2.0
POLL_TRIES = 20


def read_credentials(path: str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("refresh_token") and data.get("client_id") else None


def _save(path: str, data: dict) -> None:
    target = Path(path)
    if not target.parent.exists():
        target.parent.mkdir(parents=True, mode=0o700)  # an existing folder keeps its permissions
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, webapi.Rejected):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "Strava refused the authorization (HTTP 401); run tools/setup_strava.sh again"
        if exc.code == 429:
            return "Strava's rate limit was reached (HTTP 429); trying again later"
        detail = webapi.error_body(exc)
        return f"Strava answered HTTP {exc.code}" + (f": {detail}" if detail else "")
    if isinstance(exc, urllib.error.URLError):
        return f"can't reach Strava ({exc.reason})"
    return f"{type(exc).__name__}: {exc}"


def authorize_url(client_id: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "approval_prompt": "force", "scope": SCOPE})


def code_from(pasted: str) -> tuple[str | None, str]:
    """The one-time code (and granted scope) from the pasted localhost address, or a bare code."""
    pasted = pasted.strip()
    if "code=" not in pasted:
        return (pasted or None), ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    return (query.get("code") or [None])[0], (query.get("scope") or [""])[0]


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    return webapi.request("POST", TOKEN_URL, form={
        "client_id": client_id, "client_secret": client_secret, "code": code,
        "grant_type": "authorization_code"})


class StravaAccount:
    """The saved credentials, renewing the access token when needed (and saving the renewal)."""

    def __init__(self, path: str = DEFAULT_FILE) -> None:
        self.path = path

    @property
    def configured(self) -> bool:
        return read_credentials(self.path) is not None

    @property
    def since(self) -> float:
        """When it was set up: only sessions after this are uploaded by themselves."""
        creds = read_credentials(self.path) or {}
        return float(creds.get("since", 0))

    def token(self) -> str:
        creds = read_credentials(self.path)
        if creds is None:
            raise RuntimeError("Strava is not set up: run tools/setup_strava.sh")
        if creds.get("expires_at", 0) - REFRESH_MARGIN_S <= time.time() or not creds.get("access_token"):
            answer = webapi.request("POST", TOKEN_URL, form={
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
                "grant_type": "refresh_token", "refresh_token": creds["refresh_token"]})
            creds.update(access_token=answer["access_token"], expires_at=answer["expires_at"],
                         refresh_token=answer.get("refresh_token", creds["refresh_token"]))
            _save(self.path, creds)
        return creds["access_token"]

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _set_type(self, activity_id, sport_type: str, poll_s: float) -> None:
        """Set the new activity's type, waiting a little while Strava still says it isn't there."""
        for attempt in range(SET_TYPE_TRIES):
            try:
                webapi.request("PUT", f"{API}/activities/{activity_id}", self._auth(),
                               json_body={"sport_type": sport_type})
                return
            except Exception as exc:  # noqa: BLE001 - the activity is there; only its type is off
                if _is_not_found(exc) and attempt + 1 < SET_TYPE_TRIES:
                    time.sleep(poll_s * (attempt + 1))
                    continue
                log.warning("Strava activity %s uploaded, but its type wasn't set to %s: %s",
                            activity_id, sport_type, describe_error(exc))
                return

    def athlete_name(self) -> str:
        me = webapi.request("GET", f"{API}/athlete", self._auth())
        return " ".join(x for x in (me.get("firstname"), me.get("lastname")) if x) or str(me.get("id", "?"))

    def upload(self, data: bytes, external_id: str, name: str, description: str,
               sport_type: str = "Rowing", poll_s: float = POLL_S, data_type: str = "tcx") -> dict:
        """Upload an activity file and wait for Strava to process it. Returns the result for the session's record."""
        fields = {"data_type": data_type, "external_id": external_id, "name": name, "description": description,
                  "trainer": "1"}
        body, content_type = webapi.multipart(fields, "file", f"{external_id}.{data_type}", data)
        status = webapi.request("POST", f"{API}/uploads", self._auth(), body=body, content_type=content_type)
        for _ in range(POLL_TRIES):
            if status.get("activity_id"):
                activity_id = status["activity_id"]
                if sport_type and (data_type != "fit" or sport_type != "Rowing"):  # the upload can't set it
                    self._set_type(activity_id, sport_type, poll_s)
                return {"activity_id": activity_id}
            error = status.get("error")
            if error:
                if "duplicate" in error.lower():
                    return {"duplicate": error}  # already on Strava: nothing more to do
                raise webapi.Rejected(f"Strava couldn't process the file: {error}")
            time.sleep(poll_s)
            status = webapi.request("GET", f"{API}/uploads/{status['id']}", self._auth())
        raise RuntimeError("Strava is still processing the upload; trying again later")


SET_TYPE_TRIES = 5  # just after an upload Strava can still answer 404 for the new activity


def _is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 404


def setup(path: str) -> int:
    print("Strava setup. You need a Strava API application: create one at https://www.strava.com/settings/api")
    print('(any name, e.g. SmartRow; any website; "Authorization Callback Domain": localhost).\n')
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client Secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("Both are needed; nothing changed.", file=sys.stderr)
        return 1
    print("\nOpen this link in a browser where you're signed in to Strava, and press Authorize:\n")
    print("  " + authorize_url(client_id) + "\n")
    print('The browser then shows an error page for "localhost"; that is expected. Copy that page\'s full')
    print("address from the address bar and paste it here.\n")
    code, scope = code_from(input("Address: "))
    if not code:
        print("No code found in that address; nothing changed.", file=sys.stderr)
        return 1
    if scope and "activity:write" not in scope:
        print('Strava gave no permission to upload: tick "Upload your activities" when authorizing.',
              file=sys.stderr)
        return 1
    try:
        answer = exchange_code(client_id, client_secret, code)
    except Exception as exc:  # noqa: BLE001
        print(describe_error(exc), file=sys.stderr)
        return 1
    _save(path, {"client_id": client_id, "client_secret": client_secret,
                 "access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
                 "expires_at": answer["expires_at"], "since": time.time()})
    athlete = answer.get("athlete") or {}
    who = " ".join(x for x in (athlete.get("firstname"), athlete.get("lastname")) if x)
    print(f"\nStrava: connected{f' as {who}' if who else ''}. Saved in {path} (root only).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smartrow-strava", description=__doc__.splitlines()[0])
    p.add_argument("--file", default=os.environ.get("SRB_STRAVA_FILE", "").strip() or DEFAULT_FILE)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--setup", action="store_true", help="authorize uploads (asks for your app's details)")
    group.add_argument("--test", action="store_true", help="check the saved authorization")
    group.add_argument("--forget", action="store_true", help="delete the saved authorization")
    args = p.parse_args(argv)
    if args.setup:
        return setup(args.file)
    if args.forget:
        Path(args.file).unlink(missing_ok=True)
        print("Strava authorization deleted: no more uploads to Strava. (Revoke it on Strava too, under "
              "Settings, My Apps, if you like.)")
        return 0
    account = StravaAccount(args.file)
    if not account.configured:
        print("Strava is not set up: run tools/setup_strava.sh", file=sys.stderr)
        return 1
    try:
        print(f"Strava: connected as {account.athlete_name()}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(describe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
