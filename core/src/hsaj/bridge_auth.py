from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BRIDGE_TOKEN_ENV = "HSAJ_BRIDGE_TOKEN"
BRIDGE_TOKEN_HEADER = "X-HSAJ-Token"


def bridge_token() -> str | None:
    token = os.environ.get(BRIDGE_TOKEN_ENV)
    if token is None:
        return None
    cleaned = token.strip()
    return cleaned or None


def build_bridge_headers(*, accept: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if accept is not None:
        headers["Accept"] = accept
    token = bridge_token()
    if token is not None:
        headers[BRIDGE_TOKEN_HEADER] = token
    return headers


def append_bridge_token(url: str) -> str:
    token = bridge_token()
    if token is None:
        return url

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
