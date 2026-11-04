"""Find and delete orphan files of an Iceberg table (PyIceberg has no remove_orphan_files).

A file is an orphan only when ALL of these hold:
  * it lives under `table.location()` (nothing outside the table directory is ever considered);
  * it is not referenced by any snapshot still present in the table metadata, whichever ref
    (main, branch, tag, or no ref at all) points at it: manifest lists, manifests, and live data
    or delete files are collected from every remaining snapshot, and the current metadata file,
    the metadata log, and statistics files are always treated as referenced;
  * it was last modified before `older_than`, so files of in-flight commits are never touched.

Files that a snapshot's manifest only marks as DELETED are not references: they are exactly the
data that expiring snapshots leaves behind, and `expire_snapshots` + `delete_orphans` is what
turns metadata-only history loss into physical data loss.

`find_orphans` is read-only and is the dry run; `delete_orphans` re-verifies every file against
fresh table metadata before removing it.

Paths are compared exactly as the FileIO resolves them: the URI path, never percent-decoded.
PyIceberg writes partition directories percent-encoded (`city=H%C3%A0+N%E1%BB%99i`) and keeps
those characters literal on disk, so decoding only one side would make live files look orphaned.
As a second line of defense, any candidate whose file name equals the name of a referenced file
(names embed a UUID) aborts the operation with `OrphanSafetyError`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import pyarrow.fs as pafs
from pyiceberg.table import Table


class OrphanSafetyError(RuntimeError):
    """The orphan computation looks wrong (a referenced file would be deleted); nothing was done."""


@dataclass(frozen=True)
class OrphanFile:
    location: str  # full URI, e.g. file:///warehouse/db/orders/data/x.parquet
    size: int
    modified: datetime  # timezone-aware UTC

    def to_dict(self) -> dict[str, object]:
        return {"location": self.location, "size": self.size, "modified": self.modified.isoformat()}


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def path_of(uri: str) -> str:
    """Scheme-less path (not percent-decoded) used to compare listed files with references."""
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        return os.path.normpath(parsed.path)
    return f"{parsed.netloc}{parsed.path}".rstrip("/")


def locations_overlap(first: str, second: str) -> bool:
    """True when two table locations are equal or one is nested inside the other."""
    a, b = path_of(first).rstrip("/") + "/", path_of(second).rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


def _is_local(uri: str) -> bool:
    return urlparse(uri).scheme in ("", "file")


def _uri_for(path: str, location: str) -> str:
    parsed = urlparse(location)
    return f"file://{path}" if _is_local(location) else f"{parsed.scheme}://{path}"


def referenced_paths(table: Table) -> set[str]:
    """Normalized paths of every file the table metadata still refers to."""
    metadata = table.metadata
    refs: set[str] = {table.metadata_location}
    refs.update(entry.metadata_file for entry in metadata.metadata_log)
    refs.update(stat.statistics_path for stat in metadata.statistics)
    refs.update(stat.statistics_path for stat in metadata.partition_statistics)
    for snapshot in metadata.snapshots:
        refs.add(snapshot.manifest_list)
        for manifest in snapshot.manifests(table.io):
            refs.add(manifest.manifest_path)
            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                refs.add(entry.data_file.file_path)
    return {path_of(ref) for ref in refs if ref}


def _contained(path: str, location_path: str) -> bool:
    return path.startswith(location_path.rstrip("/") + "/")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def find_orphans(table: Table, older_than: datetime) -> list[OrphanFile]:
    """Dry run: unreferenced files under the table location last modified before `older_than`.

    Raises OrphanSafetyError if a candidate shares its file name with a referenced file.
    """
    location = table.location()
    location_path = path_of(location)
    cutoff = _aware(older_than)
    referenced = referenced_paths(table)

    filesystem, root = pafs.FileSystem.from_uri(location)
    try:
        infos = filesystem.get_file_info(pafs.FileSelector(root, recursive=True))
    except FileNotFoundError:
        return []

    orphans: list[OrphanFile] = []
    for info in infos:
        if info.type != pafs.FileType.File or info.mtime is None:
            continue
        path = os.path.normpath(info.path) if _is_local(location) else info.path.rstrip("/")
        if not _contained(path, location_path) or path in referenced:
            continue
        if _is_local(location) and not _safe_local_file(path, location_path):
            continue
        modified = _aware(info.mtime)
        if modified >= cutoff:
            continue
        orphans.append(OrphanFile(_uri_for(path, location), int(info.size or 0), modified))
    _refuse_name_clashes([path_of(o.location) for o in orphans], referenced)
    return sorted(orphans, key=lambda f: f.location)


def _refuse_name_clashes(candidates: Iterable[str], referenced: set[str]) -> None:
    live_names = {_basename(r) for r in referenced}
    clashes = sorted(c for c in candidates if _basename(c) in live_names)
    if clashes:
        raise OrphanSafetyError(
            f"{len(clashes)} orphan candidate(s) share a file name with a referenced file, so "
            f"the reference comparison cannot be trusted; nothing was deleted: {clashes[:3]}"
        )


def _safe_local_file(path: str, location_path: str) -> bool:
    """Reject symlinks and anything whose real path escapes the table directory."""
    if os.path.islink(path):
        return False
    return _contained(os.path.realpath(path), os.path.realpath(location_path))


def delete_orphans(
    table: Table, files: Iterable[OrphanFile], older_than: datetime
) -> list[OrphanFile]:
    """Delete `files` (normally the output of `find_orphans`), re-verifying each one first.

    The orphan set is recomputed from the current metadata, so a file that became referenced
    since the dry run, moved outside the table directory, or is no longer old enough is skipped.
    Returns the files actually removed.
    """
    requested = list(files)
    still_orphans = {f.location: f for f in find_orphans(table, older_than)}
    _refuse_name_clashes([path_of(f.location) for f in requested], referenced_paths(table))
    removed: list[OrphanFile] = []
    for candidate in requested:
        verified = still_orphans.get(candidate.location)
        if verified is None:
            continue
        table.io.delete(verified.location)
        removed.append(verified)
    return removed
