"""A ``gh:`` tarball is read as ``git archive`` writes it and nothing else (spec 1.10): every header is checked against
that subset before it's acted on, so nothing outside it reaches a parser or the disk."""

import gzip
import io
import os
import subprocess
import tarfile
import zlib
from pathlib import Path

import pytest

from fastro import tarball
from fastro.tarball import Refused, unpack

COMMIT = "c" * 40
TOP = "acme-bp-1234567"
OVER = "is over the limit."
NEVER = "which git archive never writes, so fastro keeps none of it (1.10)."


def _put(header: bytearray, start: int, length: int, value: bytes) -> None:
    assert len(value) <= length
    header[start : start + length] = value.ljust(length, b"\0")


def _header(
    name: str | bytes,
    kind: bytes = b"0",
    size: int = 0,
    linkname: str | bytes = b"",
    prefix: bytes = b"",
    mode: int = 0o644,
    fields: dict[int, bytes] | None = None,
    checksum: bytes | None = None,
) -> bytes:
    """A ustar header as git writes it, with ``fields`` (start offset to bytes) written over it before the checksum."""
    header = bytearray(512)
    _put(header, 0, 100, name.encode() if isinstance(name, str) else name)
    _put(header, 100, 8, b"%07o\0" % mode)
    _put(header, 108, 8, b"0000000\0")
    _put(header, 116, 8, b"0000000\0")
    _put(header, 124, 12, b"%011o\0" % size)
    _put(header, 136, 12, b"15301124640\0")
    header[156:157] = kind
    _put(header, 157, 100, linkname.encode() if isinstance(linkname, str) else linkname)
    header[257:265] = b"ustar\x0000"
    _put(header, 265, 32, b"root")
    _put(header, 297, 32, b"root")
    _put(header, 329, 8, b"0000000\0")
    _put(header, 337, 8, b"0000000\0")
    _put(header, 345, 155, prefix)
    for start, value in (fields or {}).items():
        header[start : start + len(value)] = value
    header[148:156] = b" " * 8
    header[148:156] = checksum if checksum is not None else b"%07o\0" % sum(header)
    return bytes(header)


def _padded(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 512)


def _record(keyword: str, value: str | bytes) -> bytes:
    body = b" " + keyword.encode() + b"=" + (value.encode() if isinstance(value, str) else value) + b"\n"
    length = len(body) + 1
    while len(str(length)) + len(body) != length:
        length += 1
    return str(length).encode() + body


def _extended(records: bytes, kind: bytes = b"x") -> bytes:
    return _header("pax_header", kind, len(records)) + _padded(records)


def _dir(name: str) -> bytes:
    return _header(f"{name}/", b"5", mode=0o775)


def _file(name: str, data: bytes = b"", mode: int = 0o664) -> bytes:
    return _header(name, b"0", len(data), mode=mode) + _padded(data)


def _link(name: str, target: str) -> bytes:
    return _header(name, b"2", linkname=target, mode=0o777)


def _archive(*members: bytes, commit: str = COMMIT, end: bytes | None = None) -> bytes:
    """The tar stream ``git archive`` writes: the global header, ``members`` and its zero padding, or ``end``."""
    raw = _extended(_record("comment", commit), b"g") + b"".join(members)
    return raw + (end if end is not None else bytes(1024 + -(len(raw) + 1024) % 10240))


def _unpack(raw: bytes, tmp_path: Path, extracted: int = 10**9) -> Path:
    tree = tmp_path / "tree"
    unpack(gzip.compress(raw), COMMIT, tree, extracted, OVER)
    return tree


def _refused(raw: bytes, tmp_path: Path, extracted: int = 10**9) -> str:
    with pytest.raises(Refused) as caught:
        _unpack(raw, tmp_path, extracted)
    return str(caught.value)


def _git(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True).stdout


def _listing(root: Path) -> dict[str, tuple[str, str | bytes, bool]]:
    found: dict[str, tuple[str, str | bytes, bool]] = {}
    for directory, names, files in os.walk(root):
        for name in [*names, *files]:
            path = Path(directory, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[relative] = ("symlink", os.readlink(path), False)
            elif path.is_dir():
                found[relative] = ("directory", "", False)
            else:
                found[relative] = ("file", path.read_bytes(), os.access(path, os.X_OK))
    return found


def test_what_git_archive_writes_unpacks_to_the_tree_git_checks_out(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    deep = repo / ("d" * 120) / "sub"
    deep.mkdir(parents=True)
    (deep / ("f" * 150 + ".txt")).write_text("deep\n")
    (repo / "café.txt").write_text("é\n")
    (repo / "日本").mkdir()
    (repo / "日本" / "語.md").write_text("x\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "empty").mkdir()
    (repo / "empty" / ".keep").write_text("")
    (repo / "short").symlink_to("café.txt")
    (repo / "long").symlink_to("x" * 200)
    (repo / "blueprint.toml").write_bytes(b"\xef\xbb\xbfname = 'x'\n")
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    body = _git(repo, "archive", "--format=tar.gz", f"--prefix={TOP}/", "HEAD")
    tree = tmp_path / "tree"

    unpack(body, commit, tree, 10**9, OVER)

    expected = _listing(repo)
    del expected[".git"]
    assert {path: entry for path, entry in _listing(tree).items()} == {
        path: entry for path, entry in expected.items() if not path.startswith(".git/")
    }


def test_a_tarball_python_s_tarfile_writes_in_pax_format_unpacks(tmp_path: Path) -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": COMMIT}) as archive:
        for name, kind in ((TOP, tarfile.DIRTYPE), (f"{TOP}/{'é' * 60}", tarfile.REGTYPE)):
            info = tarfile.TarInfo(name)
            info.type = kind
            archive.addfile(info, io.BytesIO(b""))
    tree = tmp_path / "tree"

    unpack(out.getvalue(), COMMIT, tree, 10**9, OVER)

    assert os.listdir(tree) == ["é" * 60]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_archive(_dir(TOP), commit="d" * 40), "isn't of that commit."),
        (_archive(_dir(TOP), commit=""), "isn't of that commit."),
        (_dir(TOP) + bytes(10240 - 512), "isn't of that commit."),
        (
            _extended(_record("comment", COMMIT) + _record("mtime", "1"), b"g") + _dir(TOP) + bytes(9216),
            f"has a pax global header with a 'mtime' record, {NEVER}",
        ),
        (_archive(_dir(TOP), _extended(_record("comment", COMMIT), b"g")), f"has a second global header, {NEVER}"),
        (_archive(_header(f"{TOP}/", b"5", fields={257: b"ustar  \0"})), f"has a header that isn't ustar, {NEVER}"),
        (_archive(_header(f"{TOP}/", b"5", fields={263: b"01"})), f"has a header that isn't ustar, {NEVER}"),
        (_archive(_header(f"{TOP}/", b"5", fields={100: b"0000648\0"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={100: b" 000644\0"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={124: b"\x80" + bytes(10) + b"\x01"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={124: b"-0000000001\0"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={100: b"0000 644"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={108: bytes(8)})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", fields={136: b"0000000009\0\0"})), "aren't octal digits"),
        (_archive(_header(f"{TOP}/", b"5", checksum=b"0000000\0")), f"has a header whose checksum doesn't match, {NEVER}"),
        (_archive(_dir(TOP), _header(f"{TOP}/s", b"S")), f"has '{TOP}/s', of type 'S', {NEVER}"),
        (_archive(_dir(TOP), _header("././@LongLink", b"L", 0)), f"has '././@LongLink', of type 'L', {NEVER}"),
        (_archive(_dir(TOP), _header(f"{TOP}/c", b"7")), f"of type '7', {NEVER}"),
        (
            _archive(_dir(TOP), _extended(_record("GNU.sparse.major", "1")), _file(f"{TOP}/s")),
            f"has a pax extended header with a 'GNU.sparse.major' record, {NEVER}",
        ),
        (
            _archive(_dir(TOP), _extended(_record("size", "1")), _file(f"{TOP}/s")),
            f"has a pax extended header with a 'size' record, {NEVER}",
        ),
        (
            _archive(_dir(TOP), _extended(_record("path", f"{TOP}/a") + _record("path", f"{TOP}/b")), _file(f"{TOP}/s")),
            f"has a pax extended header with a 'path' record, {NEVER}",
        ),
        (
            _archive(_dir(TOP), _extended(_record("path", f"{TOP}/a\0b")), _file(f"{TOP}/s")),
            f"has a pax extended header whose 'path' record holds a NUL, {NEVER}",
        ),
        (
            _archive(_dir(TOP), _extended(_record("linkpath", "x")), _file(f"{TOP}/s")),
            f"has '{TOP}/s', with a linkpath record but not a symlink, {NEVER}",
        ),
        (
            _archive(
                _dir(TOP), _extended(_record("path", f"{TOP}/a")), _extended(_record("path", f"{TOP}/b")), _file(f"{TOP}/s")
            ),
            f"has two extended headers in a row, {NEVER}",
        ),
        (
            _archive(_dir(TOP), _extended(_record("path", f"{TOP}/a"))),
            f"has a pax extended header with no member after it, {NEVER}",
        ),
        (_archive(_dir(TOP), _header(f"{TOP}/d/", b"5", 1) + bytes(512)), f"has '{TOP}/d/', a directory with a size, {NEVER}"),
        (_archive(_dir(TOP), _header(f"{TOP}/l", b"2", 1, "x") + bytes(512)), f"has '{TOP}/l', a symlink with a size, {NEVER}"),
        (_archive(_dir(TOP), _header(f"{TOP}/\xff".encode("latin-1"))), "has a path that isn't UTF-8"),
        (
            _archive(_dir(TOP), _extended(_record("path", b"\xff")), _file(f"{TOP}/s")),
            f"has a pax extended header whose 'path' record isn't UTF-8, {NEVER}",
        ),
        (
            _extended(_record("comment", b"\xff"), b"g") + _dir(TOP) + bytes(9216),
            f"has a pax global header whose 'comment' record isn't UTF-8, {NEVER}",
        ),
        (_archive(_header(f"{TOP}/", b"5", fields={100: b"000644\t\0"})), "aren't octal digits"),
        (_archive(_dir(TOP), _header(f"{TOP}/l", b"2", linkname=b"\xff")), "has a path that isn't UTF-8"),
    ],
)
def test_a_header_git_archive_never_writes_is_refused(raw: bytes, message: str, tmp_path: Path) -> None:
    assert message in _refused(raw, tmp_path)


@pytest.mark.parametrize(
    "records",
    [
        b"8 path=a\n",
        b"013 path=abc\n",
        b"11 path=abc",
        b"11 pathabc\n",
        b"x path=a\n",
        b"999999 path=a\n",
        b"0" * 40 + b"9 path=a\n",
        b"7 path=\n7 path=\n"[:-1],
    ],
)
def test_an_extended_header_whose_records_don_t_read_is_refused(records: bytes, tmp_path: Path) -> None:
    raw = _archive(_dir(TOP), _extended(records), _file(f"{TOP}/s"))

    assert _refused(raw, tmp_path) == f"has a pax extended header whose records don't read, {NEVER}"


def _linkpath_of(size: int) -> str:
    """A target whose ``linkpath`` record, with a ``path`` record for ``TOP/l``, makes ``size`` bytes of records."""
    path = _record("path", f"{TOP}/l")
    target = "t" * (size - len(path) - len(" linkpath=\n") - len(str(size)))
    assert len(_record("linkpath", target) + path) == size
    return target


@pytest.mark.parametrize(("size", "kept"), [(32_768, True), (32_769, False)])
def test_an_extended_header_over_32_768_bytes_is_refused(size: int, kept: bool) -> None:
    target = _linkpath_of(size)
    records = _record("linkpath", target) + _record("path", f"{TOP}/l")
    stream = tarball._Stream(gzip.compress(_padded(records)))

    if kept:
        assert tarball._records(stream, size, (b"path", b"linkpath"), "extended header") == {
            "linkpath": target,
            "path": f"{TOP}/l",
        }
    else:
        with pytest.raises(Refused, match=r"^has a pax extended header over 32,768 bytes, the most fastro reads \(1\.10\)\.$"):
            tarball._records(stream, size, (b"path", b"linkpath"), "extended header")


def test_an_extended_header_over_32_768_bytes_is_refused_before_a_byte_of_it_is_read() -> None:
    raw = _extended(_record("comment", COMMIT), b"g") + _dir(TOP) + _header("pax_header", b"x", 8**11 - 1)
    stream = tarball._Stream(gzip.compress(raw + bytes(1 << 20)))
    stream.read(len(raw))

    with pytest.raises(Refused, match="over 32,768 bytes"):
        tarball._records(stream, 8**11 - 1, (b"path",), "extended header")

    assert stream.offset == len(raw)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([_file(f"{TOP}/d/x")], f"'{TOP}/d/x', which isn't in a directory listed before it"),
        ([_file(f"{TOP}/d/x"), _dir(f"{TOP}/d")], f"'{TOP}/d/x', which isn't in a directory listed before it"),
        ([_link(f"{TOP}/l", "."), _file(f"{TOP}/l/x")], f"'{TOP}/l/x', which isn't in a directory listed before it"),
        ([_file(f"{TOP}/f"), _file(f"{TOP}/f/x")], f"'{TOP}/f/x', which isn't in a directory listed before it"),
        ([_file(f"{TOP}/a"), _file(f"{TOP}/a")], f"'{TOP}/a', which is there twice"),
        ([_file(f"{TOP}/a"), _dir(f"{TOP}/a")], f"'{TOP}/a/', which is there twice"),
        ([_dir(f"{TOP}/a"), _file(f"{TOP}/a")], f"'{TOP}/a', which is there twice"),
        ([_dir(f"{TOP}/a"), _dir(f"{TOP}/a")], f"'{TOP}/a/', which is there twice"),
        ([_dir(f"{TOP}/a"), _link(f"{TOP}/a", "x")], f"'{TOP}/a', which is there twice"),
        ([_dir(TOP)], f"'{TOP}/', which isn't under the tarball's one top directory"),
        ([_file(f"{TOP}/x/../y")], f"'{TOP}/x/../y', which leads outside the tree"),
        ([_file(f"/{TOP}/x")], f"'/{TOP}/x', which leads outside the tree"),
        ([_file(f"{TOP}//x")], f"'{TOP}//x', which has an empty name in it"),
        ([_header(f"{TOP}/f/")], f"'{TOP}/f/', which ends in / but isn't a directory"),
        ([_link(f"{TOP}/l", "/etc")], f"'{TOP}/l', which is a symlink that leads outside the tree"),
        ([_link(f"{TOP}/l", "")], f"'{TOP}/l', which is a symlink with an empty target"),
        ([_link(f"{TOP}/l", "../x")], f"'{TOP}/l', which is a symlink that leads outside the tree"),
        (
            [_dir(f"{TOP}/d"), _link(f"{TOP}/d/s2", ".."), _link(f"{TOP}/s1", "d/s2/..")],
            f"'{TOP}/s1', which is a symlink that leads outside the tree",
        ),
        ([_extended(_record("path", f"{TOP}/" + "n" * 300)), _file("x")], "which is too long a path for this system"),
    ],
)
def test_a_member_outside_the_tree_git_archive_writes_is_refused(members: list[bytes], message: str, tmp_path: Path) -> None:
    assert message in _refused(_archive(_dir(TOP), *members), tmp_path)


@pytest.mark.parametrize("first", [_dir(f"{TOP}/sub"), _file(TOP), _link(TOP, "x"), _header(TOP, b"5")])
def test_the_first_member_is_a_directory_whose_path_is_one_name(first: bytes, tmp_path: Path) -> None:
    assert "which isn't under the tarball's one top directory" in _refused(_archive(first), tmp_path)


def test_a_text_field_ends_at_its_first_nul(tmp_path: Path) -> None:
    raw = _archive(_dir(TOP), _header(f"{TOP}/f\0junk".encode()), _header(f"{TOP}/l", b"2", linkname=b"f\0junk"))

    tree = _unpack(raw, tmp_path)

    assert (sorted(os.listdir(tree)), os.readlink(tree / "l")) == (["f", "l"], "f")


def test_a_path_split_across_prefix_and_name_is_joined_with_a_slash(tmp_path: Path) -> None:
    directory = _extended(_record("path", f"{TOP}/{'p' * 90}/")) + _header("ignored/", b"5")
    raw = _archive(_dir(TOP), directory, _header("n" * 100, prefix=f"{TOP}/{'p' * 90}".encode()))

    assert os.listdir(_unpack(raw, tmp_path) / ("p" * 90)) == ["n" * 100]


def test_a_member_with_an_executable_mode_is_kept_executable_and_others_aren_t(tmp_path: Path) -> None:
    tree = _unpack(_archive(_dir(TOP), _file(f"{TOP}/run", b"x", 0o775), _file(f"{TOP}/data", b"x", 0o664)), tmp_path)

    assert (os.access(tree / "run", os.X_OK), os.access(tree / "data", os.X_OK)) == (True, False)


@pytest.mark.parametrize(
    ("end", "message"),
    [
        (b"", "isn't a whole gzipped tar."),
        (bytes(512), "isn't a whole gzipped tar."),
        (bytes(1024), "isn't a whole gzipped tar."),
        (bytes(5000) + b"\1" + bytes(8704 - 5001), f"has bytes after its end, {NEVER}"),
        (bytes(8704 - 1) + b"\1", f"has bytes after its end, {NEVER}"),
        (bytes(8704) + b"\1", f"has bytes after its end, {NEVER}"),
        (bytes(8704 + 10240), f"has bytes after its end, {NEVER}"),
    ],
)
def test_the_archive_ends_with_zero_blocks_to_the_first_whole_record(end: bytes, message: str, tmp_path: Path) -> None:
    raw = _archive(_dir(TOP), end=end)
    assert len(_extended(_record("comment", COMMIT), b"g") + _dir(TOP)) == 1536

    assert _refused(raw, tmp_path) == message


@pytest.mark.parametrize(("size", "length"), [(7168, 10240), (7680, 20480)])
def test_two_zero_blocks_that_cross_a_record_take_the_padding_to_the_next_one(size: int, length: int, tmp_path: Path) -> None:
    raw = _archive(_dir(TOP), _file(f"{TOP}/f", b"x" * size))

    assert len(raw) == length
    assert (_unpack(raw, tmp_path) / "f").read_bytes() == b"x" * size


@pytest.mark.parametrize(
    "damage",
    ["cut", "wrong crc", "wrong length", "two gzip streams", "bytes after the gzip stream", "not gzip"],
)
def test_a_body_that_isn_t_one_whole_gzip_stream_is_refused(damage: str, tmp_path: Path) -> None:
    good = gzip.compress(_archive(_dir(TOP), _file(f"{TOP}/f", b"data")))
    body = {
        "cut": good[:-12],
        "wrong crc": good[:-8] + bytes(byte ^ 0xFF for byte in good[-8:-4]) + good[-4:],
        "wrong length": good[:-4] + bytes(byte ^ 0xFF for byte in good[-4:]),
        "two gzip streams": good + gzip.compress(b""),
        "bytes after the gzip stream": good + b"x",
        "not gzip": b"not a tarball",
    }[damage]

    with pytest.raises(Refused, match="^isn't a whole gzipped tar\\.$"):
        unpack(body, COMMIT, tmp_path / "tree", 10**9, OVER)


def test_a_file_cut_short_inside_the_stream_is_refused(tmp_path: Path) -> None:
    raw = _extended(_record("comment", COMMIT), b"g") + _dir(TOP) + _header(f"{TOP}/f", b"0", 4096) + b"x" * 100

    assert _refused(raw, tmp_path) == "isn't a whole gzipped tar."


def test_zeros_past_the_end_are_refused_without_decompressing_them() -> None:
    raw = _archive(_dir(TOP))
    compress = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    body = compress.compress(raw) + b"".join(compress.compress(bytes(1 << 20)) for _ in range(64)) + compress.flush()
    stream = tarball._Stream(body)
    stream.read(2048)

    with pytest.raises(Refused, match="has bytes after its end"):
        tarball._end(stream)

    assert stream.offset == len(raw)
    assert len(stream.buffer) <= tarball.CHUNK


@pytest.mark.parametrize(("members", "kept"), [(3, True), (4, False)])
def test_more_members_than_the_limit_are_refused_counting_neither_global_nor_extended_headers(
    members: int, kept: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tarball, "MEMBERS", 3)
    files = [_extended(_record("path", f"{TOP}/{index}")) + _file("x") for index in range(members - 1)]
    raw = _archive(_dir(TOP), *files)

    if kept:
        assert sorted(os.listdir(_unpack(raw, tmp_path))) == [str(index) for index in range(members - 1)]
    else:
        assert _refused(raw, tmp_path) == "has more than 3 members, the most fastro keeps (1.10)."


@pytest.mark.parametrize(("sizes", "kept"), [((60, 40), True), ((60, 41), False)])
def test_files_whose_sizes_add_up_past_the_extracted_limit_are_refused(
    sizes: tuple[int, int], kept: bool, tmp_path: Path
) -> None:
    raw = _archive(_dir(TOP), *[_file(f"{TOP}/{index}", b"x" * size) for index, size in enumerate(sizes)])

    if kept:
        assert sorted(os.listdir(_unpack(raw, tmp_path, extracted=100))) == ["0", "1"]
    else:
        assert _refused(raw, tmp_path, extracted=100) == OVER


def test_a_file_s_size_is_counted_against_the_limit_before_its_data_is_read(tmp_path: Path) -> None:
    raw = _extended(_record("comment", COMMIT), b"g") + _dir(TOP) + _header(f"{TOP}/f", b"0", 8**11 - 1)

    assert _refused(raw, tmp_path, extracted=10**9) == OVER
    assert not (tmp_path / "tree" / "f").exists()


def test_a_regular_file_whose_linkname_isn_t_utf_8_is_kept(tmp_path: Path) -> None:
    tree = _unpack(_archive(_dir(TOP), _header(f"{TOP}/f", linkname=b"\xff")), tmp_path)

    assert os.listdir(tree) == ["f"]


@pytest.mark.parametrize("call", ["mkdir", "open", "symlink"])
def test_a_path_the_file_system_can_t_keep_apart_from_one_before_it_is_refused(
    call: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = {"mkdir": _dir(f"{TOP}/b"), "open": _file(f"{TOP}/b"), "symlink": _link(f"{TOP}/b", "a")}[call]
    raw = _archive(_dir(TOP), _file(f"{TOP}/a"), member)
    real = getattr(os, call)

    def clash(target: object, *arguments: object, **keywords: object) -> object:
        if (target if call != "symlink" else arguments[0]) == "b":
            raise FileExistsError(17, "File exists")
        return real(target, *arguments, **keywords)

    monkeypatch.setattr(os, call, clash)

    assert "which is a path this file system can't keep apart from one listed before it" in _refused(raw, tmp_path)


Layout = list[tuple[str, str, str]]
OUTSIDE = "which is a symlink that leads outside the tree"
UNRESOLVED = "which is a symlink fastro can't resolve inside the tree through what the archive lists"
TOO_MANY = "which is a symlink that follows more than 40 symlinks, the most fastro follows"
LAYOUTS: list[tuple[Layout, str | None]] = [
    ([("l", "symlink", "..")], OUTSIDE),
    ([("l", "symlink", ".")], None),
    ([("l", "symlink", "./..")], OUTSIDE),
    ([("d", "dir", ""), ("l", "symlink", "d/..")], None),
    ([("d", "dir", ""), ("l", "symlink", "d/./..")], None),
    ([("d", "dir", ""), ("l", "symlink", "d/../..")], OUTSIDE),
    ([("d", "dir", ""), ("d/l", "symlink", "../x")], None),
    ([("d", "dir", ""), ("d/l", "symlink", "../../x")], OUTSIDE),
    ([("d", "dir", ""), ("d/s2", "symlink", ".."), ("s1", "symlink", "d/s2/..")], OUTSIDE),
    ([("d", "dir", ""), ("d/s2", "symlink", ".."), ("s1", "symlink", "d/s2/d")], None),
    ([("l", "symlink", "x")], None),
    ([("d", "dir", ""), ("l", "symlink", "d//..")], None),
    ([("d", "dir", ""), ("l", "symlink", "d/")], None),
    ([("l", "symlink", ".//..")], OUTSIDE),
    ([("l", "symlink", "x/y")], None),
    ([("l", "symlink", "x/y/../..")], UNRESOLVED),
    ([("l", "symlink", "x/y/../../..")], UNRESOLVED),
    ([("f", "file", ""), ("l", "symlink", "f/x")], None),
    ([("f", "file", ""), ("l", "symlink", "f/..")], UNRESOLVED),
    ([("f", "file", ""), ("l", "symlink", "f/../..")], UNRESOLVED),
    ([("d", "dir", ""), ("d/e", "dir", ""), ("d/e/up", "symlink", "../.."), ("l", "symlink", "d/e/up/..")], OUTSIDE),
    ([("d", "dir", ""), ("d/e", "dir", ""), ("d/e/up", "symlink", "../.."), ("l", "symlink", "d/e/up/d")], None),
    ([("a", "symlink", "b/c"), ("b", "dir", ""), ("b/c", "symlink", "../..")], OUTSIDE),
    ([("a", "symlink", "b/c/.."), ("b", "dir", ""), ("b/c", "symlink", "..")], OUTSIDE),
    ([("a", "symlink", "f"), ("f", "file", ""), ("l", "symlink", "a/..")], UNRESOLVED),
    ([("a", "dir", ""), ("a/b", "dir", ""), ("a/b/S", "symlink", "../.."), ("a/b/L", "symlink", "s/../..")], UNRESOLVED),
    ([("a", "dir", ""), ("a/caf\u00e9", "symlink", ".."), ("a/L", "symlink", "cafe\u0301/../..")], UNRESOLVED),
    ([("a", "symlink", "b"), ("b", "symlink", "a")], TOO_MANY),
    ([("a", "symlink", "b/.."), ("b", "symlink", "a/..")], TOO_MANY),
    ([("a", "symlink", "a/..")], TOO_MANY),
]


def _members(layout: Layout) -> list[bytes]:
    return [
        _dir(f"{TOP}/{name}") if kind == "dir" else _file(f"{TOP}/{name}") if kind == "file" else _link(f"{TOP}/{name}", target)
        for name, kind, target in layout
    ]


@pytest.mark.parametrize(("layout", "message"), LAYOUTS)
def test_a_symlink_is_kept_only_when_it_reaches_inside_the_tree_through_what_the_archive_lists(
    layout: Layout, message: str | None, tmp_path: Path
) -> None:
    raw = _archive(_dir(TOP), *_members(layout))

    if message is None:
        assert _unpack(raw, tmp_path).is_dir()
    else:
        assert message in _refused(raw, tmp_path)


@pytest.mark.parametrize(("layout", "message"), [entry for entry in LAYOUTS if entry[1] is None])
def test_every_symlink_kept_resolves_inside_the_tree_on_this_system(layout: Layout, message: None, tmp_path: Path) -> None:
    tree = _unpack(_archive(_dir(TOP), *_members(layout)), tmp_path)
    inside = os.path.realpath(tree)

    for name, kind, _ in layout:
        if kind == "symlink":
            assert os.path.commonpath([inside, os.path.realpath(tree / name)]) == inside


def test_symlinks_are_judged_without_looking_at_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def looked(*arguments: object) -> object:
        raise AssertionError("looked at the disk")

    raw = _archive(_dir(TOP), _dir(f"{TOP}/d"), _link(f"{TOP}/d/s2", ".."), _link(f"{TOP}/s1", "d/s2/.."))
    monkeypatch.setattr(os, "lstat", looked)
    monkeypatch.setattr(os.path, "realpath", looked)

    with pytest.raises(Refused, match=f"'{TOP}/s1', {OUTSIDE}"):
        unpack(gzip.compress(raw), COMMIT, tmp_path / "tree", 10**9, OVER)


@pytest.mark.parametrize(("links", "kept"), [(40, True), (41, False)])
def test_a_symlink_that_follows_more_than_40_symlinks_is_refused(links: int, kept: bool, tmp_path: Path) -> None:
    chain = [_link(f"{TOP}/l{index}", f"l{index + 1}") for index in range(links - 1)] + [_link(f"{TOP}/l{links - 1}", ".")]
    raw = _archive(_dir(TOP), *chain)

    if kept:
        assert len(os.listdir(_unpack(raw, tmp_path))) == links
    else:
        assert f"has '{TOP}/l0', {TOO_MANY}" in _refused(raw, tmp_path)


def test_a_long_chain_is_refused_without_recursing_through_it(tmp_path: Path) -> None:
    chain = [_link(f"{TOP}/l{index}", f"l{index + 1}") for index in range(4999)] + [_link(f"{TOP}/l4999", ".")]

    assert f"has '{TOP}/l0', {TOO_MANY}" in _refused(_archive(_dir(TOP), *chain), tmp_path)


@pytest.mark.parametrize(("second", "kept"), [("a1", True), ("a0", False)])
def test_symlinks_followed_one_after_another_count_together_against_the_40(second: str, kept: bool, tmp_path: Path) -> None:
    chain = [_link(f"{TOP}/a{index}", f"a{index + 1}") for index in range(19)] + [_link(f"{TOP}/a19", ".")]
    raw = _archive(_dir(TOP), *chain, _link(f"{TOP}/l", f"a0/{second}"))

    if kept:
        assert len(os.listdir(_unpack(raw, tmp_path))) == 21
    else:
        assert f"has '{TOP}/l', {TOO_MANY}" in _refused(raw, tmp_path)


def test_members_in_one_directory_are_written_through_one_descriptor_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directories: list[str] = []
    real = os.open

    def counted(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if flags & os.O_DIRECTORY:
            directories.append(os.path.basename(path))
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", counted)
    files = [_file(f"{TOP}/d/{index}") for index in range(3)]
    raw = _archive(_dir(TOP), _dir(f"{TOP}/d"), *files, _file(f"{TOP}/top"), _file(f"{TOP}/d/again"))

    _unpack(raw, tmp_path)

    assert directories == ["tree", "d", "tree", "d"]


@pytest.mark.parametrize("last", [_file(f"{TOP}/d/g"), _header(f"{TOP}/d/h", b"6")])
def test_no_descriptor_is_left_open_whether_the_tarball_is_kept_or_refused(last: bytes, tmp_path: Path) -> None:
    before = len(os.listdir("/dev/fd"))
    raw = _archive(_dir(TOP), _dir(f"{TOP}/d"), _file(f"{TOP}/d/f", b"x"), last)

    try:
        _unpack(raw, tmp_path)
    except Refused:
        pass

    assert len(os.listdir("/dev/fd")) == before


def test_each_symlink_is_resolved_once_whatever_points_at_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = tarball._Tree._resolve

    def counted(tree: tarball._Tree, link: int, resolved: dict[int, tuple[int, int]], depth: int) -> tuple[int, int]:
        calls.append(link)
        return real(tree, link, resolved, depth)

    monkeypatch.setattr(tarball._Tree, "_resolve", counted)
    chain = [_link(f"{TOP}/l{index}", f"l{index + 1}") for index in range(39)] + [_link(f"{TOP}/l39", ".")]

    _unpack(_archive(_dir(TOP), *chain), tmp_path)

    assert len(calls) == 40 + 39


def test_names_past_the_end_of_what_the_archive_lists_add_nothing_to_what_s_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes: list[int] = []
    real = tarball._Tree.finish

    def counted(tree: tarball._Tree) -> None:
        before = len(tree.parents)
        real(tree)
        nodes.append(len(tree.parents) - before)

    monkeypatch.setattr(tarball._Tree, "finish", counted)
    links = [_link(f"{TOP}/l{index}", f"x{index}/" + "a/" * 40) for index in range(50)]

    _unpack(_archive(_dir(TOP), *links), tmp_path)

    assert nodes == [0]
