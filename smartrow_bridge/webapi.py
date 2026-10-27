# SPDX-License-Identifier: GPL-3.0-only
"""Small HTTPS helper for the optional uploads (intervals.icu, Strava). Standard library only."""

from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 30
# intervals.icu sits behind Cloudflare, which can challenge unknown clients.
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) SmartRowBridge/1.0"


class Rejected(Exception):
    """The service turned the session down for good (trying again won't help)."""


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but never carries our credentials to another site."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_Redirect())


def multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes,
              content_type: str = "application/octet-stream") -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type header."""
    boundary = f"----SmartRowBridge{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request(method: str, url: str, headers: dict[str, str] | None = None, json_body: dict | None = None,
            form: dict[str, str] | None = None, body: bytes | None = None, content_type: str | None = None,
            timeout: float = TIMEOUT_S) -> dict:
    """Send a request and return its JSON answer ({} for an empty one). HTTP errors raise HTTPError."""
    if json_body is not None:
        body, content_type = json.dumps(json_body).encode(), "application/json"
    elif form is not None:
        body, content_type = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if content_type:
        req.add_header("Content-Type", content_type)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def error_body(exc: urllib.error.HTTPError) -> str:
    """What the service said along with an HTTP error, briefly (never includes our credentials)."""
    try:
        text = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(text.split())[:200]
