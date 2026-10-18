"""Packaging the addon for clients to install. A release is identified by a content hash of its
files, so any change to the shipped files is an update even if `## Version:` wasn't bumped.

The client (desktop/addon.py) computes the same hash over installed copies to tell whether they're
current; both sides are pinned to the same test vector (HASH_TEST_VECTOR in the tests), so keep
content_hash identical in both files.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

NAME = "Goldmine"
GENERATED = {"Data.lua"}  # written per user by the client; never shipped
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)  # fixed timestamps so the same files always zip to the same bytes


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


def package(src: Path) -> bytes:
    """Zip the addon as Goldmine/<files>, deterministically."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in addon_files(src):
            info = zipfile.ZipInfo(f"{NAME}/{rel.as_posix()}", _ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (Path(src) / rel).read_bytes())
    return buf.getvalue()
