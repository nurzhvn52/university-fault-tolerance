"""Storage for generated transcript files."""

from pathlib import Path


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
