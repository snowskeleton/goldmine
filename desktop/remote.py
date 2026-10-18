"""Client for a Goldmine server. Used when config.toml has [server] url = "https://gm_KEY@host".

The client key lives in the URL's user part; it's sent as a Bearer token, never in the request
line, so it doesn't end up in proxy logs.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "goldmine-client/0.2"


class RemoteError(RuntimeError):
    pass


class Remote:
    def __init__(self, url: str):
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise RemoteError("server URL must look like https://gm_KEY@your.server")
        self.key = urllib.parse.unquote(parts.username or "")
        if not self.key:
            raise RemoteError("server URL has no client key; generate one on the server's Clients page")
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        self.base = f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"

    def request(self, method: str, path: str, body: bytes | None = None, params: dict | None = None,
                headers: dict | None = None, timeout: float = 300) -> bytes:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.key}", "User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise RemoteError(f"server said {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"can't reach {self.base}: {exc.reason}") from exc

    def get_json(self, path: str, params: dict | None = None):
        return json.loads(self.request("GET", path, params=params))

    def text(self, path: str, params: dict | None = None) -> str:
        return self.request("GET", path, params=params).decode()

    def upload_saved_variables(self, data: bytes, source: str) -> dict:
        return json.loads(self.request("POST", "/api/v1/game-data", body=gzip.compress(data), params={"source": source},
                                       headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}))
