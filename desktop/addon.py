"""Install and update the Goldmine addon in WoW's AddOns folders, from releases the server hands out.

A release is identified by a content hash of the addon's files, so any change to the shipped files
is an update even if nobody bumped `## Version:`. content_hash must match the server's
(server/goldmine_server/addon.py); both are pinned to the same test vector. Data.lua is written
per machine by `push`, never shipped, and survives updates.

Layout: <WoW>/<_flavor_>/Interface/AddOns/Goldmine. A Goldmine folder that is a symlink (a
development checkout) is reported as linked and left alone unless forced.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

NAME = "Goldmine"
GENERATED = {"Data.lua"}  # written by push, per machine; never shipped


class AddonError(RuntimeError):
    pass


def addon_files(src: Path) -> list[Path]:
    """Files that make up a release, relative to src, sorted."""
    out = []
    for p in sorted(Path(src).rglob("*")):
        rel = p.relative_to(src)
        if p.is_file() and rel.as_posix() not in GENERATED and not any(
                part.startswith(".") or part == "__pycache__" for part in rel.parts):
            out.append(rel)
    return out


def content_hash(src: Path) -> str | None:
    """sha256 over (path, bytes) of every release file, or None if src has no addon."""
    src = Path(src)
    if not (src / f"{NAME}.toc").is_file():
        return None
    h = hashlib.sha256()
    for rel in addon_files(src):
        h.update(rel.as_posix().encode() + b"\0")
        h.update((src / rel).read_bytes() + b"\0")
    return h.hexdigest()


def toc_version(src: Path) -> str | None:
    try:
        m = re.search(r"^## Version:\s*(.+?)\s*$", (Path(src) / f"{NAME}.toc").read_text(errors="replace"), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def describe(src: Path) -> dict | None:
    digest = content_hash(src)
    return None if digest is None else {"version": toc_version(src), "hash": digest}


# ---------------------------------------------------------------------------
# Installed copies
# ---------------------------------------------------------------------------

def flavors(wow_dirs: list[str]) -> list[Path]:
    """Game flavor folders (_retail_, _classic_, _forever_, ...) under each WoW install."""
    found = set()
    for base in wow_dirs:
        for p in glob.glob(os.path.join(base, "_*_")):
            if os.path.isdir(p):
                found.add(Path(p))
    return sorted(found)


def target_for(flavor: Path) -> Path:
    return Path(flavor) / "Interface" / "AddOns" / NAME


def installed(wow_dirs: list[str]) -> list[dict]:
    """Install state of the addon in every flavor folder."""
    out = []
    for fl in flavors(wow_dirs):
        dest = target_for(fl)
        linked = dest.is_symlink()
        info = describe(dest) if dest.exists() else None
        out.append({
            "flavor": fl.name,
            "wow_dir": str(fl.parent),
            "path": str(dest),
            "installed": info is not None,
            "linked": linked,
            "link_target": os.path.realpath(dest) if linked else None,
            "version": info and info["version"],
            "hash": info and info["hash"],
        })
    return out


def data_targets(wow_dirs: list[str]) -> list[Path]:
    """Where push should write Data.lua: every installed copy, each real folder once."""
    seen, out = set(), []
    for st in installed(wow_dirs):
        if st["installed"]:
            real = os.path.realpath(st["path"])
            if real not in seen:
                seen.add(real)
                out.append(Path(st["path"]) / "Data.lua")
    return out


def _extract(data: bytes, into: Path) -> Path:
    """Unzip a release into `into`, refusing anything outside Goldmine/. Returns the addon folder."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AddonError(f"addon download isn't a zip: {exc}") from exc
    with zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if (not parts or parts[0] != NAME or ".." in parts or info.filename.startswith(("/", "\\"))
                    or ":" in info.filename):
                raise AddonError(f"unexpected path in addon zip: {info.filename!r}")
            if info.is_dir():
                continue
            out = into.joinpath(*parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
    return into / NAME


def install(data: bytes, flavor: Path, expected_hash: str | None = None, force: bool = False) -> str:
    """Install or replace the addon in one flavor folder from a release zip. Keeps Data.lua.
    Returns what happened: installed | updated | current | linked."""
    dest = target_for(flavor)
    if dest.is_symlink() and not force:
        return "linked"
    current = content_hash(dest) if dest.exists() else None
    if expected_hash and current == expected_hash and not force:
        return "current"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Stage next to the destination so the final swap is a rename on the same volume.
    stage = Path(tempfile.mkdtemp(prefix=f".{NAME}-new-", dir=dest.parent))
    old = None
    try:
        new = _extract(data, stage)
        got = content_hash(new)
        if got is None:
            raise AddonError(f"addon zip has no {NAME}.toc")
        if expected_hash and got != expected_hash:
            raise AddonError("addon download doesn't match the server's checksum")
        for name in GENERATED:
            if (dest / name).is_file():
                shutil.copy2(dest / name, new / name)
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            old = dest.with_name(f".{NAME}-old-{os.getpid()}")
            if old.exists():
                shutil.rmtree(old)
            dest.rename(old)
        try:
            new.rename(dest)
        except OSError:
            if old is not None:
                old.rename(dest)  # put the previous version back
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    return "updated" if current else "installed"
