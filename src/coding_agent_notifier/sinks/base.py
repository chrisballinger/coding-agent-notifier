from __future__ import annotations

import json
from typing import Protocol
from urllib import request as _urlreq
from urllib.error import URLError

from ..event import Event


class SinkError(RuntimeError):
    pass


class Sink(Protocol):
    name: str

    def send(self, event: Event) -> None: ...


def http_post_json(
    url: str,
    body: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = _urlreq.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - controlled URLs
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except URLError as e:
        raise SinkError(f"HTTP POST to {url} failed: {e}") from e
