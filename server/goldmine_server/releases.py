"""Updates for clients: the addon (packaged from the copy shipped in this image) and desktop app
builds (uploaded by an admin, since they're built on a Mac/PC, not in this container)."""

from __future__ import annotations

import hashlib
import io
import json
import plistlib
import re
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import addon, settings

PLATFORMS = ("macos", "windows")
MAX_APP = 200 * 1024 * 1024
_addon_cache: dict[str, bytes] = {}


def addon_zip() -> tuple[dict, bytes]:
    """The shipped addon's release info and zip. Re-hashed each call (a handful of small files),
    so a redeploy or an edit in a dev checkout shows up without a restart."""
    digest = addon.content_hash(settings.ADDON_DIR)
    if digest is None:
        raise HTTPException(404, "this server has no addon to serve")
    data = _addon_cache.get(digest)
    if data is None:
        data = addon.package(settings.ADDON_DIR)
        _addon_cache.clear()
        _addon_cache[digest] = data
    info = {"version": addon.toc_version(settings.ADDON_DIR), "hash": digest,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": "/api/v1/updates/addon.zip"}
    return info, data


def _app_dir(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform must be one of {', '.join(PLATFORMS)}")
    return settings.RELEASES / "app" / platform


def app_release(platform: str) -> dict | None:
    try:
        info = json.loads((_app_dir(platform) / "latest.json").read_text())
    except (OSError, ValueError):
        return None
    return info | {"url": f"/api/v1/updates/app/{platform}.zip"}


def app_file(platform: str) -> Path:
    info = app_release(platform)
    path = _app_dir(platform) / info["file"] if info else None
    if not path or not path.is_file():
        raise HTTPException(404, f"no {platform} app release on this server")
    return path


def manifest() -> dict:
    try:
        addon_info = addon_zip()[0]
    except HTTPException:
        addon_info = None
    return {"addon": addon_info, "app": {p: r for p in PLATFORMS if (r := app_release(p))}}


def _macos_info(zf: zipfile.ZipFile) -> tuple[str, int]:
    plists = [n for n in zf.namelist() if re.fullmatch(r"[^/]+\.app/Contents/Info\.plist", n)]
    if len(plists) != 1:
        raise HTTPException(400, "zip must contain one .app bundle at its top level")
    try:
        info = plistlib.loads(zf.read(plists[0]))
        build = int(info["CFBundleVersion"])
    except (ValueError, KeyError, plistlib.InvalidFileException) as exc:
        raise HTTPException(400, f"can't read the app's Info.plist: {exc}") from exc
    if info.get("CFBundleIdentifier") != settings.APP_BUNDLE_ID:
        raise HTTPException(400, f"bundle id must be {settings.APP_BUNDLE_ID}")
    return str(info.get("CFBundleShortVersionString", "")), build


def _windows_info(zf: zipfile.ZipFile) -> tuple[str, int]:
    # A Windows build carries its version in goldmine-release.json at the zip root.
    try:
        meta = json.loads(zf.read("goldmine-release.json"))
        return str(meta["version"]), int(meta["build"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "zip must contain goldmine-release.json with version and build") from exc


def save_app_release(platform: str, data: bytes, notes: str = "") -> dict:
    """Store an uploaded app build (a zip) as the latest release for its platform. The version
    and build come from the build itself. The previous build is kept; older ones are deleted."""
    folder = _app_dir(platform)
    if not data or len(data) > MAX_APP:
        raise HTTPException(400, "empty or oversized upload")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, f"not a zip: {exc}") from exc
    with zf:
        bad = [n for n in zf.namelist() if n.startswith(("/", "\\")) or ".." in Path(n).parts]
        if bad:
            raise HTTPException(400, f"unsafe path in zip: {bad[0]}")
        version, build = _macos_info(zf) if platform == "macos" else _windows_info(zf)
    current = app_release(platform)
    if current and build <= int(current["build"]):
        raise HTTPException(409, f"build {build} isn't newer than the current release ({current['build']})")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"Goldmine-{platform}-{build}.zip"
    (folder / name).write_bytes(data)
    info = {"version": version, "build": build, "file": name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "notes": notes.strip()[:2000], "uploaded": int(time.time())}
    tmp = folder / "latest.json.tmp"
    tmp.write_text(json.dumps(info))
    tmp.replace(folder / "latest.json")
    keep = {name, current["file"]} if current else {name}
    for old in folder.glob("Goldmine-*.zip"):
        if old.name not in keep:
            old.unlink()
    return info
