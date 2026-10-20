"""Client for a Goldmine server: config.toml's [server] url (the server's address) and key (an
API key from the server's Clients page).

The key is sent as a Bearer token, never in the request line, so it doesn't end up in proxy logs.
Older configs had the key in the URL's user part (https://gm_KEY@host); that still works and
is split into url + key the next time the config is saved.
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


def split_url(url: str) -> tuple[str, str]:
    """-> (address without credentials, key from an old-style https://gm_KEY@host URL or "")."""
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteError("server address must look like https://gold.example.com")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = host + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}", urllib.parse.unquote(parts.username or "")


class Remote:
    def __init__(self, url: str, key: str | None = None):
        self.base, legacy_key = split_url(url)
        self.key = (key or "").strip() or legacy_key
        if not self.key:
            raise RemoteError("no API key; generate one on the server's Clients page")
        if not self.key.startswith("gm_"):
            raise RemoteError("that isn't a Goldmine API key (they start with gm_; an invite link won't work)")

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
