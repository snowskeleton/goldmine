"""Background AH collection: a thread that runs market.collect on the configured interval, then
packs finished days into the compressed archive (archive.pack_due)."""

from __future__ import annotations

import threading
import time
from collections import deque

from . import archive, db, market, settings


def load_config() -> dict:
    if not settings.CONFIG.exists():
        return {}
    try:
        return market.load_config(settings.CONFIG)
    except Exception:  # a broken hand edit shouldn't kill the server
        return {}


class Collector:
    def __init__(self):
        self.log: deque[tuple[float, str]] = deque(maxlen=400)
        self.last_run: float | None = None
        self.last_ok: bool | None = None
        self.next_run: float | None = None
        self.running = False
        self._wake = threading.Event()
        self._force = False
        self._stop = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, message: str) -> None:
        self.log.append((time.time(), message))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def trigger(self) -> None:
        """Collect as soon as possible (from the dashboard's Collect Now button)."""
        self._force = True
        self._wake.set()

    def settings(self, cfg: dict) -> tuple[bool, int]:
        app = cfg.get("app") or {}
        api = cfg.get("api") or {}
        enabled = app.get("auto_collect", True) and bool(api.get("client_id") and api.get("client_secret"))
        return enabled, max(int(app.get("collect_minutes", 20)), 5)

    def _loop(self) -> None:
        while not self._stop:
            cfg = load_config()
            enabled, minutes = self.settings(cfg)
            due = self.last_run is None or time.time() - self.last_run >= minutes * 60
            if self._force or (enabled and due):
                self._force = False
                self.run_once(cfg)
            cfg = load_config()
            enabled, minutes = self.settings(cfg)
            self.next_run = (self.last_run or time.time()) + minutes * 60 if enabled else None
            wait = 30 if self.next_run is None else max(1, min(30, self.next_run - time.time()))
            self._wake.wait(wait)
            self._wake.clear()

    def run_once(self, cfg: dict | None = None) -> bool:
        cfg = cfg if cfg is not None else load_config()
        api = cfg.get("api") or {}
        if not (api.get("client_id") and api.get("client_secret")):
            self.add("collection skipped: set the Blizzard API client id and secret in Settings")
            return False
        if not cfg.get("market"):
            self.add("collection skipped: no markets configured")
            return False
        with self._lock:
            self.running = True
            try:
                conn = db.market_conn()
                try:
                    client = market.Client(api["client_id"], api["client_secret"], cache_dir=settings.CACHE_DIR)
                    errors = market.collect(conn, client, cfg, log=self.add)
                    try:
                        archive.pack_due(conn, log=self.add)
                    except Exception as exc:  # the rows stay in the hot tables; next run retries
                        self.add(f"archiving failed: {exc}")
                    try:
                        market.probe_unpublished(conn, client, {m["region"] for m in cfg["market"]}, log=self.add)
                    except Exception as exc:  # a check, nothing depends on it
                        self.add(f"API check failed: {exc}")
                    try:
                        from . import account  # late: account -> marketdata -> services -> collector
                        account.refresh_item_info(conn, client, cfg, log=self.add)
                    except Exception as exc:  # item details are nice to have; don't fail the run
                        self.add(f"item info failed: {exc}")
                finally:
                    conn.close()
                self.last_ok = errors == 0
            except Exception as exc:  # keep the thread alive; show the problem on the dashboard
                self.add(f"collection failed: {exc}")
                self.last_ok = False
            finally:
                self.running = False
                self.last_run = time.time()
        return bool(self.last_ok)


collector = Collector()
