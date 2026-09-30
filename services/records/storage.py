"""Storage for generated transcript files."""

import logging
import os
from collections import Counter
from pathlib import Path

logger = logging.getLogger("records.storage")


class LocalStorage:
    """Baseline: a single directory on one volume, no redundancy and no integrity check."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def write(self, key: str, data: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return (self._root / key).read_bytes()


class MirroredStorage:
    """RAID-1 style mirroring over several disks (volumes) with a majority voter (TMR).

    Every file is written to all disks; a write succeeds if a majority of the copies was
    written. A read takes all copies and returns the content a majority agrees on, so one
    corrupted or missing copy is masked; the bad copy is rewritten on the spot (read
    repair). ``scrub`` checks every file the same way, like a RAID scrub, so that a damaged
    copy is repaired before a second disk fails.
    """

    def __init__(self, roots: list[str]) -> None:
        self._roots = [Path(root) for root in roots]
        self._quorum = len(self._roots) // 2 + 1

    def write(self, key: str, data: bytes) -> None:
        written = sum(self._write_copy(root, key, data) for root in self._roots)
        if written < self._quorum:
            raise OSError(f"only {written} of {len(self._roots)} copies of {key} written")

    def read(self, key: str) -> bytes:
        copies = [self._read_copy(root, key) for root in self._roots]
        present = [copy for copy in copies if copy is not None]
        if not present:
            raise FileNotFoundError(key)
        data, votes = Counter(present).most_common(1)[0]
        if votes < self._quorum:
            raise OSError(f"no majority for {key}: {votes} of {len(self._roots)} copies agree")
        for root, copy in zip(self._roots, copies, strict=True):
            if copy != data and self._write_copy(root, key, data):
                logger.warning("copy_repaired", extra={"key": key, "disk": str(root)})
        return data

    def keys(self) -> set[str]:
        found = set()
        for root in self._roots:
            if root.is_dir():
                found.update(p.relative_to(root).as_posix() for p in root.rglob("*.json"))
        return found

    def scrub(self) -> tuple[int, int]:
        """Read every file through the voter. Returns (files checked, files unreadable)."""
        unreadable = 0
        keys = self.keys()
        for key in keys:
            try:
                self.read(key)
            except OSError:
                unreadable += 1
        return len(keys), unreadable

    @staticmethod
    def _read_copy(root: Path, key: str) -> bytes | None:
        try:
            return (root / key).read_bytes()
        except OSError:
            return None

    @staticmethod
    def _write_copy(root: Path, key: str, data: bytes) -> bool:
        path = root / key
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file and rename, so a crash never leaves a torn copy.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            return False
        return True
