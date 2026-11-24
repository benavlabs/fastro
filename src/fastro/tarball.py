"""A ``gh:`` tarball read as ``git archive`` writes it and nothing else, and written into a directory as it's read
(spec 1.10). Each header is checked against that subset before anything in it is used, so what's read and kept stays
within the limits it's given."""

from __future__ import annotations

import errno
import os
import posixpath
import zlib
from collections.abc import Iterable
from pathlib import Path

BLOCK = 512
RECORD = 10_240
CHUNK = 1 << 16
EXTENDED = 32_768
MEMBERS = 50_000
LENGTH_DIGITS = len(str(EXTENDED))
HOPS = 40
ESCAPES = -1
PAST = -2
GLOBAL = b"g"
EXTENSION = b"x"
KINDS = {b"0": "file", b"5": "directory", b"2": "symlink"}
REFUSED_KINDS = {b"1": "is a hard link", b"3": "is a device", b"4": "is a device", b"6": "is a FIFO"}
NUMBERS = ((100, 108), (108, 116), (116, 124), (124, 136), (136, 148), (148, 156))
WHOLE = "isn't a whole gzipped tar."


class Refused(Exception):
    """A tarball fastro keeps none of. The message completes ``<source>'s tarball `` or ``<source>'s archive ``."""


def unpack(body: bytes | Iterable[bytes], commit: str, tree: Path, extracted: int, over: str) -> None:
    """Write the tarball ``body`` (its bytes, or its bytes in pieces as they come) of ``commit`` into ``tree``,
    past its one top directory, refusing it as soon as it holds anything ``git archive`` doesn't write, more than
    ``MEMBERS`` members, or files whose sizes add up past ``extracted`` (the refusal then says ``over``).

    Raises:
        Refused: the tarball isn't one fastro keeps (spec 1.10).
        OSError: the system won't let fastro write ``tree``.
    """
    stream = _Stream(body)
    kind, size, _, _, _ = _header(stream.read(BLOCK))
    records = _records(stream, size, (b"comment",), "global header") if kind == GLOBAL else {}
    if records.get("comment") != commit:
        raise Refused("isn't of that commit.")
    written = _Tree(tree)
    try:
        _members(stream, written, extracted, over)
    finally:
        written.close()
    _end(stream)
    written.finish()


def _members(stream: _Stream, written: _Tree, extracted: int, over: str) -> None:
    """Read and write each member up to the archive's first zero block, as ``unpack`` describes."""
    members = total = 0
    extended: dict[str, str] | None = None
    while (block := stream.read(BLOCK)) != bytes(BLOCK):
        kind, size, mode, name, linkname = _header(block)
        if kind == EXTENSION and extended is None:
            extended = _records(stream, size, (b"path", b"linkpath"), "extended header")
            continue
        if kind in (GLOBAL, EXTENSION):
            raise Refused(f"has {'a second global header' if kind == GLOBAL else 'two extended headers in a row'}, {_NEVER}")
        records, extended = extended or {}, None
        path = records["path"] if "path" in records else _text(name)
        target = records["linkpath"] if "linkpath" in records else _text(linkname) if kind == b"2" else ""
        if "linkpath" in records and kind != b"2":
            raise Refused(f"has {path!r}, with a linkpath record but not a symlink, {_NEVER}")
        if kind in REFUSED_KINDS:
            raise _refused(path, REFUSED_KINDS[kind])
        if kind not in KINDS:
            raise Refused(f"has {path!r}, of type {kind.decode('latin-1')!r}, {_NEVER}")
        members += 1
        if members > MEMBERS:
            raise Refused(f"has more than {MEMBERS:,} members, the most fastro keeps (1.10).")
        if size and KINDS[kind] != "file":
            raise Refused(f"has {path!r}, a {KINDS[kind]} with a size, {_NEVER}")
        total += size
        if total > extracted:
            raise Refused(over)
        written.add(path, KINDS[kind], mode, target, stream, size)
    if extended is not None:
        raise Refused(f"has a pax extended header with no member after it, {_NEVER}")


_NEVER = "which git archive never writes, so fastro keeps none of it (1.10)."


_UNRESOLVED = "is a symlink fastro can't resolve inside the tree through what the archive lists"
_TOO_MANY = f"is a symlink that follows more than {HOPS} symlinks, the most fastro follows"


class _Unresolved(Exception):
    """A symlink target that doesn't reach a path inside the tree; the message is why."""


def _refused(path: str, reason: str) -> Refused:
    return Refused(f"has {path!r}, which {reason}, so fastro keeps none of it (1.10).")


class _Stream:
    """The tar archive inside one gzip stream, decompressed only as far as it's read."""

    def __init__(self, body: bytes | Iterable[bytes]) -> None:
        self.inflate = zlib.decompressobj(zlib.MAX_WBITS | 16)
        self.chunks = iter([body] if isinstance(body, bytes) else body)
        self.pending = b""
        self.buffer = b""
        self.offset = 0

    def read(self, size: int) -> bytes:
        """The next ``size`` bytes of the archive, ``size`` no more than ``CHUNK``."""
        parts, have = [self.buffer], len(self.buffer)
        while have < size:
            chunk = self._inflate(CHUNK)
            if not chunk:
                raise Refused(WHOLE)
            parts.append(chunk)
            have += len(chunk)
        data = b"".join(parts)
        self.buffer = data[size:]
        self.offset += size
        return data[:size]

    def more(self) -> bool:
        """Whether the archive goes on past what's been read."""
        if not self.buffer:
            self.buffer = self._inflate(1)
        return bool(self.buffer)

    def ended(self) -> bool:
        """Whether the gzip stream ends where the archive does, its checksum and length matching, with nothing
        after it."""
        return self.inflate.eof and not self.inflate.unused_data and not any(self.chunks)

    def _inflate(self, most: int) -> bytes:
        while not self.inflate.eof:
            if not self.pending:
                piece = next(self.chunks, None)
                if piece is None:
                    return b""
                self.pending = piece
                continue
            try:
                chunk = self.inflate.decompress(self.pending, most)
            except zlib.error:
                raise Refused(WHOLE) from None
            self.pending = self.inflate.unconsumed_tail
            if chunk:
                return chunk
        return b""


def _header(block: bytes) -> tuple[bytes, int, int, bytes, bytes]:
    """A ustar header's type, size, mode, path (``prefix`` and ``name``) and ``linkname``, once its magic, numeric
    fields and checksum are what ``git archive`` writes."""
    if block[257:265] != b"ustar\x0000":
        raise Refused(f"has a header that isn't ustar, {_NEVER}")
    numbers = [_octal(block[start:end]) for start, end in NUMBERS]
    if None in numbers:
        raise Refused(f"has a header whose numeric fields aren't octal digits, {_NEVER}")
    mode, _, _, size, _, checksum = numbers
    if sum(block[:148]) + 8 * ord(" ") + sum(block[156:]) != checksum:
        raise Refused(f"has a header whose checksum doesn't match, {_NEVER}")
    prefix, name = _field(block[345:500]), _field(block[:100])
    assert mode is not None and size is not None
    return block[156:157], size, mode, prefix + b"/" + name if prefix else name, _field(block[157:257])


def _octal(field: bytes) -> int | None:
    digits = field.rstrip(b"\0 ")
    if not digits or digits.strip(b"01234567"):
        return None
    return int(digits, 8)


def _field(field: bytes) -> bytes:
    return field.partition(b"\0")[0]


def _text(name: bytes) -> str:
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"has a path that isn't UTF-8, {name!r}, so fastro keeps none of it (1.10).") from None


def _records(stream: _Stream, size: int, keywords: tuple[bytes, ...], header: str) -> dict[str, str]:
    """The pax records of a ``size``-byte header, each one of ``keywords``, read only when ``size`` is within
    ``EXTENDED``."""
    if size > EXTENDED:
        raise Refused(f"has a pax {header} over {EXTENDED:,} bytes, the most fastro reads (1.10).")
    data = b"".join(stream.read(min(CHUNK, size - start)) for start in range(0, size, CHUNK))
    stream.read(-size % BLOCK)
    records: dict[str, str] = {}
    start = 0
    while start < size:
        space = data.find(b" ", start, start + LENGTH_DIGITS + 1)
        digits = data[start:space]
        end = start + int(digits) if space > start and digits.isdigit() and not digits.startswith(b"0") else 0
        keyword, equals, value = data[space + 1 : end - 1].partition(b"=")
        if end <= space or data[end - 1 : end] != b"\n" or not equals:
            raise Refused(f"has a pax {header} whose records don't read, {_NEVER}")
        if keyword not in keywords or keyword.decode() in records:
            shown = keyword.decode("utf-8", "backslashreplace")
            raise Refused(f"has a pax {header} with a {shown!r} record, {_NEVER}")
        if b"\0" in value:
            raise Refused(f"has a pax {header} whose {keyword.decode()!r} record holds a NUL, {_NEVER}")
        try:
            records[keyword.decode()] = value.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(f"has a pax {header} whose {keyword.decode()!r} record isn't UTF-8, {_NEVER}") from None
        start = end
    return records


def _end(stream: _Stream) -> None:
    """Refuse anything after the archive's first zero block but zero blocks to the first whole record at least one
    more block on, and the end of the gzip stream."""
    end = stream.offset + BLOCK
    padding = -end % RECORD + end - stream.offset
    while padding:
        if stream.read(min(CHUNK, padding)).strip(b"\0"):
            raise Refused(f"has bytes after its end, {_NEVER}")
        padding -= min(CHUNK, padding)
    if stream.more():
        raise Refused(f"has bytes after its end, {_NEVER}")
    if not stream.ended():
        raise Refused(WHOLE)


class _Tree:
    """The members written under ``root``, past their one top directory, each in a directory listed before it, so
    never through a symlink, refusing whatever spec 1.10 rules out for a fetch. Each path is a node: node 0 is the
    top directory, and ``children`` maps a node and a name to the node under it."""

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.root = str(root)
        self.top: str | None = None
        self.children: dict[tuple[int, str], int] = {}
        self.parents = [ESCAPES]
        self.kinds = ["directory"]
        self.links: dict[int, tuple[str, str]] = {}
        self.opened: tuple[int, int] | None = None

    def add(self, path: str, kind: str, mode: int, target: str, stream: _Stream, size: int) -> None:
        parts = path.split("/")
        if len(parts) > 1 and not parts[-1]:
            if kind != "directory":
                raise _refused(path, "ends in / but isn't a directory")
            parts.pop()
        if posixpath.isabs(path) or not {".", ".."}.isdisjoint(parts):
            raise _refused(path, "leads outside the tree")
        if "" in parts:
            raise _refused(path, "has an empty name in it")
        if self.top is None and kind == "directory" and len(parts) == 1 and path.endswith("/"):
            self.top = parts[0]
            return
        if parts[0] != self.top or len(parts) == 1:
            raise _refused(path, "isn't under the tarball's one top directory")
        parent = 0
        for part in parts[1:-1]:
            parent = self.children.get((parent, part), ESCAPES)
            if parent == ESCAPES or self.kinds[parent] != "directory":
                raise _refused(path, "isn't in a directory listed before it")
        if (parent, parts[-1]) in self.children:
            raise _refused(path, "is there twice")
        node = self._node(parent, parts[-1], kind)
        name = parts[-1]
        try:
            where = self._directory(parent, parts[1:-1])
            if kind == "directory":
                os.mkdir(name, dir_fd=where)
            elif kind == "symlink":
                if not target:
                    raise _refused(path, "is a symlink with an empty target")
                if posixpath.isabs(target):
                    raise _refused(path, "is a symlink that leads outside the tree")
                os.symlink(target, name, dir_fd=where)
                self.links[node] = (path, target)
            else:
                self._file(name, where, mode, stream, size)
        except OSError as error:
            if error.errno == errno.ENAMETOOLONG:
                raise _refused(path, "is too long a path for this system") from None
            if error.errno == errno.EEXIST:
                raise _refused(path, "is a path this file system can't keep apart from one listed before it") from None
            raise
        stream.read(-size % BLOCK)

    def close(self) -> None:
        """Close the directory the last member was written in."""
        if self.opened is not None:
            os.close(self.opened[1])
            self.opened = None

    def _directory(self, node: int, parents: list[str]) -> int:
        """A descriptor of the directory ``node``, at ``parents`` under the root, kept open for the members after it."""
        if self.opened is None or self.opened[0] != node:
            self.close()
            self.opened = (node, os.open("/".join([self.root, *parents]), os.O_RDONLY | os.O_DIRECTORY))
        return self.opened[1]

    def finish(self) -> None:
        """Refuse a symlink whose target doesn't reach a path inside the tree through the members listed, within
        ``HOPS`` symlinks (spec 1.10), each link resolved once."""
        resolved: dict[int, tuple[int, int]] = {}
        for node, (path, _) in self.links.items():
            try:
                self._resolve(node, resolved, 1)
            except _Unresolved as unresolved:
                raise _refused(path, str(unresolved)) from None

    def _node(self, parent: int, name: str, kind: str) -> int:
        node = len(self.parents)
        self.children[parent, name] = node
        self.parents.append(parent)
        self.kinds.append(kind)
        return node

    def _resolve(self, link: int, resolved: dict[int, tuple[int, int]], depth: int) -> tuple[int, int]:
        """The node the symlink ``link`` leads to, or ``PAST`` once a name goes past the end of what the archive
        lists, and how many symlinks that follows, itself included, kept in ``resolved``. ``depth`` only bounds the
        recursion: a link ``depth`` deep already follows more than ``depth`` symlinks, so the hop count refuses it.

        Raises:
            _Unresolved: the target leaves the tree, goes up from something the archive doesn't list as a directory,
                or follows more than ``HOPS`` symlinks.
        """
        if link in resolved:
            return resolved[link]
        if depth > HOPS:
            raise _Unresolved(_TOO_MANY)
        here, hops = self.parents[link], 1
        for part in self.links[link][1].split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if here == PAST or self.kinds[here] != "directory":
                    raise _Unresolved(_UNRESOLVED)
                here = self.parents[here]
                if here == ESCAPES:
                    raise _Unresolved("is a symlink that leads outside the tree")
                continue
            here = self.children.get((here, part), PAST)
            if here in self.links:
                here, more = self._resolve(here, resolved, depth + 1)
                hops += more
        if hops > HOPS:
            raise _Unresolved(_TOO_MANY)
        resolved[link] = (here, hops)
        return here, hops

    def _file(self, name: str, where: int, mode: int, stream: _Stream, size: int) -> None:
        handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o777 if mode & 0o100 else 0o666, dir_fd=where)
        with os.fdopen(handle, "wb") as out:
            for start in range(0, size, CHUNK):
                out.write(stream.read(min(CHUNK, size - start)))
