import pytest

from records.storage import MirroredStorage


@pytest.fixture
def disks(tmp_path):
    return [tmp_path / f"disk{i}" for i in (1, 2, 3)]


def test_every_copy_is_written(disks):
    storage = MirroredStorage([str(d) for d in disks])

    storage.write("1/a.json", b"data")

    assert all((d / "1" / "a.json").read_bytes() == b"data" for d in disks)


def test_one_corrupted_copy_is_outvoted_and_repaired(disks):
    storage = MirroredStorage([str(d) for d in disks])
    storage.write("1/a.json", b"good")
    (disks[0] / "1" / "a.json").write_bytes(b"bad!")

    assert storage.read("1/a.json") == b"good"
    assert (disks[0] / "1" / "a.json").read_bytes() == b"good"


def test_a_missing_copy_is_restored(disks):
    storage = MirroredStorage([str(d) for d in disks])
    storage.write("1/a.json", b"good")
    (disks[1] / "1" / "a.json").unlink()

    assert storage.read("1/a.json") == b"good"
    assert (disks[1] / "1" / "a.json").read_bytes() == b"good"


def test_two_bad_copies_cannot_be_outvoted(disks):
    storage = MirroredStorage([str(d) for d in disks])
    storage.write("1/a.json", b"good")
    (disks[0] / "1" / "a.json").write_bytes(b"bad1")
    (disks[1] / "1" / "a.json").write_bytes(b"bad2")

    with pytest.raises(OSError, match="no majority"):
        storage.read("1/a.json")


def test_scrub_repairs_every_file(disks):
    storage = MirroredStorage([str(d) for d in disks])
    for n in range(5):
        storage.write(f"{n}/doc.json", f"doc {n}".encode())
    for n in range(5):
        (disks[2] / str(n) / "doc.json").write_bytes(b"flipped")

    assert storage.scrub() == (5, 0)
    assert all(
        (disks[2] / str(n) / "doc.json").read_bytes() == f"doc {n}".encode() for n in range(5)
    )


def test_write_fails_without_a_majority_of_disks(disks, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    storage = MirroredStorage([str(disks[0]), str(blocker), str(blocker)])

    with pytest.raises(OSError, match="only 1 of 3"):
        storage.write("1/a.json", b"data")
