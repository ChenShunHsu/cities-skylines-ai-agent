"""HTTP-only transport for the local Cities: Skylines Agent Bridge."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class BridgeRequestError(RuntimeError):
    """A structured non-success response returned by the Bridge."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(json.dumps(detail, ensure_ascii=False))


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """Send one request without retries or policy decisions."""

    url = base_url.rstrip("/") + path
    if query:
        clean = {
            key: (str(value).lower() if isinstance(value, bool) else value)
            for key, value in query.items()
            if value is not None
        }
        url += "?" + urllib.parse.urlencode(clean)
    data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    bridge_request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    open_request = opener or urllib.request.urlopen
    try:
        with open_request(bridge_request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(payload)
        except json.JSONDecodeError:
            detail = {"ok": False, "error": payload or str(error)}
        raise BridgeRequestError(detail) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Cities: Skylines Agent Bridge is unavailable at {base_url.rstrip('/')}: "
            f"{error.reason}"
        ) from error
