import struct

from jsm.scanner.moviehash import CHUNK_SIZE, compute_moviehash


def reference_hash(path):
    """Independent straight-from-the-spec implementation for cross-checking."""
    size = path.stat().st_size
    data = path.read_bytes()
    value = size
    for chunk in (data[:CHUNK_SIZE], data[-CHUNK_SIZE:]):
        for i in range(0, CHUNK_SIZE, 8):
            value = (value + struct.unpack_from("<Q", chunk, i)[0]) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % value


def test_small_file_returns_none(tmp_path):
    f = tmp_path / "tiny.mkv"
    f.write_bytes(b"x" * 1000)
    assert compute_moviehash(f) is None


def test_zero_file_hash_equals_size(tmp_path):
    f = tmp_path / "zeros.mkv"
    f.write_bytes(b"\0" * (CHUNK_SIZE * 2))
    # all-zero content: hash == file size
    assert compute_moviehash(f) == "%016x" % (CHUNK_SIZE * 2)


def test_matches_reference_implementation(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(bytes(range(256)) * 1024)  # 256 KiB patterned
    assert compute_moviehash(f) == reference_hash(f)


def test_hash_changes_with_content(tmp_path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"a" * 200_000)
    b.write_bytes(b"b" * 200_000)
    assert compute_moviehash(a) != compute_moviehash(b)
