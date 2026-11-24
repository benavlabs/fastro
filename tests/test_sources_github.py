"""Resolving a ``gh:`` source (spec 1.7): a given ref to its commit, or the release with the highest version whose
catalog accepts this fastro, through GitHub's API, with ``GITHUB_TOKEN`` sent as a header and never anywhere else."""

import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

from fastro import sources
from fastro.sources import FetchError, NotFound, Resolved, _SameHostAuthorization, http_get, parse, resolve_gh

TOOL = Version("0.1.0.dev1")
API = "https://api.github.com/repos/acme/bp"
SHA = "a" * 40
OLD = "b" * 40


def _catalog(requires: str = ">=0.1.0.dev0", schema: int = 1) -> bytes:
    return f'[blueprint]\nname = "bp"\nversion = "0.21.0"\nschema = {schema}\nrequires_fastro = "{requires}"\n'.encode()


class Fake:
    """GitHub's API as a mapping of URL to body; a URL it doesn't hold answers 404. It records every request."""

    def __init__(self, answers: Mapping[str, bytes | FetchError]) -> None:
        self.answers = dict(answers)
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.limits: list[int | None] = []

    def __call__(self, url: str, headers: Mapping[str, str], limit: int | None = None) -> bytes:
        self.requests.append((url, dict(headers)))
        self.limits.append(limit)
        answer = self.answers.get(url, NotFound(f"{url} answered 404 Not Found", 404))
        if isinstance(answer, FetchError):
            raise answer
        return answer


def _releases(*releases: object, page: int = 1) -> tuple[str, bytes]:
    return f"{API}/releases?per_page=100&page={page}", json.dumps(list(releases)).encode()


def _release(tag: str, draft: bool = False, prerelease: bool = False) -> dict[str, Any]:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


def _contents(tag: str, path: str = "blueprint.toml") -> str:
    return f"{API}/contents/{path}?ref={tag}"


def _commit(ref: str) -> str:
    return f"{API}/commits/{ref}"


def test_a_given_ref_resolves_to_its_full_commit() -> None:
    fake = Fake({_commit("v1.2.0"): f"{SHA}\n".encode()})

    assert resolve_gh(parse("gh:acme/bp@v1.2.0"), TOOL, fake, {}) == Resolved("v1.2.0", SHA)
    assert fake.requests[0][1]["Accept"] == "application/vnd.github.sha"


def test_a_ref_is_quoted_in_the_url() -> None:
    fake = Fake({_commit("feature%2Fx"): SHA.encode()})

    assert resolve_gh(parse("gh:acme/bp@feature/x"), TOOL, fake, {}).commit == SHA


def test_with_no_ref_the_highest_release_whose_catalog_accepts_this_fastro_is_taken() -> None:
    fake = Fake(
        dict(
            [
                _releases(
                    "not a release",
                    {"draft": False},
                    {"tag_name": None},
                    {"tag_name": 5},
                    {"tag_name": ["v1"]},
                    {"tag_name": ""},
                    _release("v0.23.0", draft=True),
                    _release("v0.22.5", prerelease=True),
                    _release("v0.20.0"),
                    _release("v0.21.0"),
                    _release("v0.22.0"),
                    _release("v0.21.1+x.y"),
                ),
                (_contents("v0.22.0"), _catalog(">=9")),
                (_contents("v0.21.1%2Bx.y"), _catalog().replace(b'"bp"', b'"b\xff"')),
                (_contents("v0.21.0"), _catalog()),
                (_commit("v0.21.0"), SHA.encode()),
            ]
        )
    )

    assert resolve_gh(parse("gh:acme/bp"), TOOL, fake, {}) == Resolved("v0.21.0", SHA)
    asked = [url for url, _ in fake.requests]
    assert asked == [
        f"{API}/releases?per_page=100&page=1",
        _contents("v0.22.0"),
        _contents("v0.21.1%2Bx.y"),
        _contents("v0.21.0"),
        _contents("v0.21.0", ".gitattributes"),
        _commit("v0.21.0"),
    ]
    listing, contents = fake.requests[0][1], fake.requests[1][1]
    assert (listing["Accept"], listing["X-GitHub-Api-Version"]) == ("application/vnd.github+json", "2022-11-28")
    assert contents["Accept"] == "application/vnd.github.raw+json"


def _highest(*tags: str, pages: int = 1) -> tuple[Resolved, list[str]]:
    """What resolving ``gh:acme/bp`` takes when every release in ``tags`` (listed in that order, newest published
    first) has a catalog this fastro reads, and the catalogs it asked for, in order."""
    listed = [_release(tag) for tag in tags]
    chunks = [listed[i::pages] for i in range(pages)]
    chunks = [chunk + [_release("not-a-version")] * (100 - len(chunk)) for chunk in chunks[:-1]] + chunks[-1:]
    answers = dict(_releases(*chunk, page=n) for n, chunk in enumerate(chunks, 1))
    for tag in tags:
        answers[_contents(tag)] = _catalog()
        answers[_commit(tag)] = SHA.encode()
    fake = Fake(answers)
    resolved = resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})
    prefix = f"{API}/contents/blueprint.toml?ref="
    return resolved, [url.removeprefix(prefix) for url, _ in fake.requests if url.startswith(prefix)]


def test_a_backport_published_after_a_newer_release_never_wins() -> None:
    assert _highest("v0.21.5", "v0.22.0", "v0.21.4") == (Resolved("v0.22.0", SHA), ["v0.22.0"])


@pytest.mark.parametrize("tag", ["v0.23.0rc1", "v0.23.0.dev1", "0.23.0-beta.2", "v0.23.0.post1.dev0"])
def test_a_pre_release_or_development_version_github_doesn_t_mark_is_never_picked(tag: str) -> None:
    assert _highest(tag, "v0.22.0") == (Resolved("v0.22.0", SHA), ["v0.22.0"])


@pytest.mark.parametrize("tag", ["latest", "release-2026", "vv0.23.0", "v 0.23.0", "0.23.0-final-2"])
def test_a_tag_that_doesn_t_parse_as_a_version_is_skipped(tag: str) -> None:
    assert _highest(tag, "v0.22.0") == (Resolved("v0.22.0", SHA), ["v0.22.0"])


def test_a_post_release_counts_as_a_release() -> None:
    assert _highest("v0.22.0", "v0.22.0.post1")[0].ref == "v0.22.0.post1"


def test_two_tags_with_the_same_version_go_by_name_the_higher_in_code_point_order_first() -> None:
    assert _highest("1.0", "V1.0", "v1.0", "1.0.0") == (Resolved("v1.0", SHA), ["v1.0"])


def test_releases_on_every_page_are_ordered_together_before_any_catalog_is_read() -> None:
    resolved, asked = _highest("v0.20.0", "v0.22.0", pages=2)

    assert (resolved.ref, asked) == ("v0.22.0", ["v0.22.0"])


def _walk(source: str, attributes: Mapping[str, str]) -> tuple[Resolved, list[str]]:
    """What resolving ``source`` takes when ``v0.22.0`` and ``v0.21.0`` both have a catalog this fastro reads under
    its ``#SUBDIR`` and ``v0.22.0`` has the ``.gitattributes`` files in ``attributes``, and the paths it asked for."""
    catalog = source.partition("#")[2]
    catalog = f"{catalog}/blueprint.toml" if catalog else "blueprint.toml"
    answers = dict([_releases(_release("v0.21.0"), _release("v0.22.0"))])
    for tag in ("v0.22.0", "v0.21.0"):
        answers[_contents(tag, catalog)] = _catalog()
        answers[_commit(tag)] = SHA.encode()
    for path, text in attributes.items():
        answers[_contents("v0.22.0", path)] = text.encode()
    fake = Fake(answers)
    resolved = resolve_gh(parse(source), TOOL, fake, {})
    prefix = f"{API}/contents/"
    return resolved, [url.removeprefix(prefix) for url, _ in fake.requests if url.startswith(prefix)]


@pytest.mark.parametrize(
    ("source", "attributes"),
    [
        ("gh:acme/bp", {".gitattributes": "blueprint.toml export-ignore\n"}),
        ("gh:acme/bp", {".gitattributes": "/blueprint.toml export-ignore\n"}),
        ("gh:acme/bp", {".gitattributes": "*.toml export-ignore\n"}),
        ("gh:acme/bp", {".gitattributes": "[attr]gone export-ignore\nblueprint.toml gone\n"}),
        ("gh:acme/bp#t/api", {".gitattributes": "t/ export-ignore\n"}),
        ("gh:acme/bp#t/api", {".gitattributes": "api/ export-ignore\n"}),
        ("gh:acme/bp#t/api", {".gitattributes": "t/api/blueprint.toml export-ignore\n"}),
        ("gh:acme/bp#t/api", {"t/.gitattributes": "api/ export-ignore\n"}),
        ("gh:acme/bp#t/api", {"t/api/.gitattributes": "blueprint.toml export-ignore\n"}),
        (
            "gh:acme/bp#t/api",
            {".gitattributes": "t/api/blueprint.toml -export-ignore\n", "t/.gitattributes": "api/** export-ignore\n"},
        ),
    ],
)
def test_with_no_ref_a_tag_whose_catalog_export_ignore_marks_is_skipped(source: str, attributes: Mapping[str, str]) -> None:
    resolved, _ = _walk(source, attributes)

    assert resolved == Resolved("v0.21.0", SHA)


@pytest.mark.parametrize(
    ("source", "attributes"),
    [
        ("gh:acme/bp", {}),
        ("gh:acme/bp", {".gitattributes": "docs/ export-ignore\nblueprint.toml export-subst\n"}),
        ("gh:acme/bp", {".gitattributes": "Blueprint.toml export-ignore\n"}),
        ("gh:acme/bp", {".gitattributes": "blueprint.toml -export-ignore\n"}),
        ("gh:acme/bp", {"docs/.gitattributes": "* export-ignore\n"}),
        ("gh:acme/bp#t/api", {".gitattributes": "/api/ export-ignore\n"}),
        (
            "gh:acme/bp#t/api",
            {
                ".gitattributes": "t/api/blueprint.toml export-ignore\n",
                "t/api/.gitattributes": "blueprint.toml -export-ignore\n",
            },
        ),
        ("gh:acme/bp#t/api", {"t/api/blueprint.toml/.gitattributes": "* export-ignore\n"}),
    ],
)
def test_with_no_ref_a_tag_whose_catalog_export_ignore_doesn_t_mark_is_picked(
    source: str, attributes: Mapping[str, str]
) -> None:
    resolved, _ = _walk(source, attributes)

    assert resolved == Resolved("v0.22.0", SHA)


def test_the_walk_reads_the_attributes_on_the_catalog_s_path_only_for_a_tag_whose_catalog_reads() -> None:
    answers = dict([_releases(_release("v0.21.0"), _release("v0.22.0"))])
    answers[_contents("v0.22.0", "t/api/blueprint.toml")] = _catalog(">=9")
    answers[_contents("v0.21.0", "t/api/blueprint.toml")] = _catalog()
    answers[_commit("v0.21.0")] = SHA.encode()
    fake = Fake(answers)

    resolve_gh(parse("gh:acme/bp#t/api"), TOOL, fake, {})

    prefix = f"{API}/contents/"
    assert [url.removeprefix(prefix) for url, _ in fake.requests if url.startswith(prefix)] == [
        "t/api/blueprint.toml?ref=v0.22.0",
        "t/api/blueprint.toml?ref=v0.21.0",
        ".gitattributes?ref=v0.21.0",
        "t/.gitattributes?ref=v0.21.0",
        "t/api/.gitattributes?ref=v0.21.0",
    ]
    assert all(headers["Accept"] == "application/vnd.github.raw+json" for _, headers in fake.requests[3:6])


def test_a_tag_with_no_gitattributes_on_the_catalog_s_path_is_picked_without_running_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_git(top: object, name: str) -> bool:
        raise AssertionError(f"git checked {name}")

    monkeypatch.setattr(sources, "export_ignored", no_git)

    assert _walk("gh:acme/bp#t/api", {})[0] == Resolved("v0.22.0", SHA)


def test_a_contents_read_asks_for_at_most_1_mb_and_nothing_else_does() -> None:
    fake = Fake(dict([_releases(_release("v1")), (_contents("v1"), _catalog()), (_commit("v1"), SHA.encode())]))
    fake.answers[_contents("v1", ".gitattributes")] = b"x"

    resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})

    assert [(url.startswith(f"{API}/contents/"), limit) for (url, _), limit in zip(fake.requests, fake.limits)] == [
        (False, None),
        (True, 10**6),
        (True, 10**6),
        (False, None),
    ]


@pytest.mark.parametrize(
    ("path", "size", "kept"),
    [
        ("blueprint.toml", 10**6, True),
        ("blueprint.toml", 10**6 + 1, False),
        (".gitattributes", 10**6, True),
        (".gitattributes", 10**6 + 1, False),
    ],
)
def test_a_file_read_through_the_contents_api_over_1_mb_fails_the_fetch(path: str, size: int, kept: bool) -> None:
    answers = dict([_releases(_release("v1")), (_contents("v1"), _catalog()), (_commit("v1"), SHA.encode())])
    answers[_contents("v1", ".gitattributes")] = b"# x\n"
    answers[_contents("v1", path)] = answers[_contents("v1", path)].ljust(size, b"\n")

    if kept:
        assert resolve_gh(parse("gh:acme/bp"), TOOL, Fake(answers), {}) == Resolved("v1", SHA)
    else:
        message = (
            rf"^gh:acme/bp's {re.escape(path)} at v1 is over 1 MB, the most fastro reads through GitHub's API \(1\.10\)\.$"
        )
        with pytest.raises(FetchError, match=message):
            resolve_gh(parse("gh:acme/bp"), TOOL, Fake(answers), {})


@pytest.mark.parametrize(("subdir", "quoted"), [("a%20b", "a%2520b"), ("\u00e9/t", "%C3%A9/t")])
def test_a_subdir_is_percent_quoted_in_a_contents_url(subdir: str, quoted: str) -> None:
    fake = Fake(
        dict(
            [
                _releases(_release("v1")),
                (_contents("v1", f"{quoted}/blueprint.toml"), _catalog()),
                (_commit("v1"), SHA.encode()),
            ]
        )
    )

    assert resolve_gh(parse(f"gh:acme/bp#{subdir}"), TOOL, fake, {}) == Resolved("v1", SHA)
    assert _contents("v1", f"{quoted}/.gitattributes") in [url for url, _ in fake.requests]


def test_at_a_given_ref_the_attributes_aren_t_read() -> None:
    fake = Fake({_commit("v1"): SHA.encode()})

    resolve_gh(parse("gh:acme/bp@v1"), TOOL, fake, {})

    assert [url for url, _ in fake.requests] == [_commit("v1")]


def test_when_every_tag_s_catalog_is_export_ignored_the_walk_says_no_release_accepts_this_fastro() -> None:
    answers = dict([_releases(_release("v0.22.0"))])
    answers[_contents("v0.22.0")] = _catalog()
    answers[_contents("v0.22.0", ".gitattributes")] = b"blueprint.toml export-ignore\n"

    with pytest.raises(FetchError, match=r"gh:acme/bp has no release whose blueprint\.toml accepts this fastro"):
        resolve_gh(parse("gh:acme/bp"), TOOL, Fake(answers), {})


def test_a_repo_github_doesn_t_show_says_so_and_names_github_token() -> None:
    with pytest.raises(FetchError, match=r"GitHub shows no repo gh:acme/bp\. If it's private, set GITHUB_TOKEN") as caught:
        resolve_gh(parse("gh:acme/bp"), TOOL, Fake({}), {})

    assert caught.value.status == 404


def test_a_ref_github_doesn_t_show_says_so_and_names_github_token() -> None:
    with pytest.raises(
        FetchError, match=r"GitHub shows no ref 'v9' in gh:acme/bp\. If the repo is private, set GITHUB_TOKEN"
    ) as caught:
        resolve_gh(parse("gh:acme/bp@v9"), TOOL, Fake({}), {})

    assert caught.value.status == 404


def test_a_release_without_a_catalog_or_with_a_newer_schema_is_skipped_and_later_pages_are_read() -> None:
    full = [_release(f"v0.20.{n}") for n in range(98)]
    fake = Fake(
        dict(
            [
                _releases(_release("v0.30.0"), _release("v0.20.0"), *full),
                _releases(_release("v0.19.0"), page=2),
                (_contents("v0.30.0"), _catalog(schema=2)),
                (_contents("v0.19.0"), _catalog()),
                (_commit("v0.19.0"), OLD.encode()),
            ]
        )
    )

    assert resolve_gh(parse("gh:acme/bp"), TOOL, fake, {}) == Resolved("v0.19.0", OLD)


def test_the_catalog_is_read_under_the_source_s_subdirectory() -> None:
    fake = Fake(
        dict(
            [
                _releases(_release("v1.0.0")),
                (_contents("v1.0.0", "templates/api/blueprint.toml"), _catalog()),
                (_commit("v1.0.0"), SHA.encode()),
            ]
        )
    )

    assert resolve_gh(parse("gh:acme/bp#templates/api"), TOOL, fake, {}).ref == "v1.0.0"


def test_no_compatible_release_is_a_fetch_failure_naming_the_source() -> None:
    fake = Fake(dict([_releases(_release("v0.20.0"))]))

    with pytest.raises(FetchError, match="gh:acme/bp has no release whose blueprint.toml accepts this fastro, 0.1.0.dev1"):
        resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})


@pytest.mark.parametrize("status", [403, 429, 500])
def test_an_error_other_than_404_on_a_catalog_ends_the_search_instead_of_skipping_the_release(status: int) -> None:
    error = FetchError(f"answered {status}", status)
    fake = Fake(dict([_releases(_release("v1.0.0")), (_contents("v1.0.0"), error)]))

    with pytest.raises(FetchError) as caught:
        resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})

    assert caught.value is error


def test_a_release_list_that_isn_t_a_list_is_a_fetch_failure() -> None:
    fake = Fake(dict([(f"{API}/releases?per_page=100&page=1", b'{"message": "Bad credentials"}')]))

    with pytest.raises(FetchError, match="didn't answer with a list of releases"):
        resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})


@pytest.mark.parametrize("answer", [b"not a sha", b"a" * 7, b"a" * 40 + b"zz", b"A" * 40])
def test_a_commit_that_isn_t_a_full_sha_is_a_fetch_failure(answer: bytes) -> None:
    fake = Fake({_commit("v1"): answer})

    with pytest.raises(FetchError, match="didn't answer with a full commit"):
        resolve_gh(parse("gh:acme/bp@v1"), TOOL, fake, {})


def test_github_token_is_sent_as_a_header_on_every_request_and_nowhere_else() -> None:
    fake = Fake(dict([_releases(_release("v1")), (_contents("v1"), _catalog()), (_commit("v1"), SHA.encode())]))

    resolve_gh(parse("gh:acme/bp"), TOOL, fake, {"GITHUB_TOKEN": "s3cret"})
    with_token, fake.requests = fake.requests, []
    resolve_gh(parse("gh:acme/bp"), TOOL, fake, {})

    assert len(with_token) == len(fake.requests) == 4
    for url, headers in with_token:
        assert (headers["Authorization"], headers["User-Agent"]) == ("Bearer s3cret", "fastro")
        assert "s3cret" not in url
    assert all("Authorization" not in headers for _, headers in fake.requests)


class Handler(BaseHTTPRequestHandler):
    """A local stand-in for a web server: each path answers as the test sets it, and the headers it got are kept."""

    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:
        Handler.seen.append(dict(self.headers))
        if self.path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
        elif self.path == "/stall":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"start")
            self.wfile.flush()
            time.sleep(3)
        elif self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"body")
        elif self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", self.path.removeprefix("/redirect?to="))
            self.end_headers()
        else:
            self.send_response(404 if self.path == "/missing" else 500)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    Handler.seen = []
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_get_returns_the_body_and_sends_the_headers(server: str) -> None:
    assert http_get(f"{server}/ok", {"X-Test": "1"}) == b"body"
    assert Handler.seen[0]["X-Test"] == "1"


@pytest.mark.parametrize(("limit", "body"), [(2, b"bod"), (3, b"body"), (4, b"body"), (10**15, b"body"), (None, b"body")])
def test_http_get_reads_at_most_one_byte_past_its_limit(server: str, limit: int | None, body: bytes) -> None:
    assert http_get(f"{server}/ok", {}, limit) == body


def test_a_404_is_not_found_and_another_error_a_fetch_failure(server: str) -> None:
    with pytest.raises(NotFound) as missing:
        http_get(f"{server}/missing", {})
    with pytest.raises(FetchError) as failed:
        http_get(f"{server}/broken", {})

    assert (missing.value.status, failed.value.status) == (404, 500)
    assert not isinstance(failed.value, NotFound)


@pytest.mark.parametrize("status", [403, 429])
def test_a_rate_limit_answer_says_to_set_github_token_or_wait(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def refused(request: object, timeout: float) -> None:
        timeouts.append(timeout)
        raise urllib.error.HTTPError("https://api.github.com/x", status, "Forbidden", Message(), None)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, request, timeout: refused(request, timeout))

    with pytest.raises(FetchError, match="If that's GitHub's rate limit, set GITHUB_TOKEN, or wait.") as caught:
        http_get("https://api.github.com/x", {"Authorization": "Bearer s3cret"})

    assert caught.value.status == status
    assert not isinstance(caught.value, NotFound)
    assert "s3cret" not in str(caught.value)
    assert timeouts == [30]


@pytest.mark.parametrize("limit", [None, 5, 99, 1000])
def test_a_body_cut_short_is_a_fetch_failure(server: str, limit: int | None) -> None:
    with pytest.raises(FetchError, match="can't reach .*IncompleteRead"):
        http_get(f"{server}/short", {}, limit)


def test_a_body_cut_short_past_the_limit_is_the_limit_s_worth(server: str) -> None:
    assert http_get(f"{server}/short", {}, 3) == b"shor"


def test_a_server_it_can_t_reach_is_a_fetch_failure(server: str) -> None:
    port = int(server.rsplit(":", 1)[1])

    with pytest.raises(FetchError, match="can't reach"):
        http_get(f"http://127.0.0.1:{port + 1 if port < 65535 else port - 1}/ok", {})


@pytest.mark.parametrize(
    ("to", "kept"),
    [
        ("https://api.github.com/y", True),
        ("http://api.github.com/y", False),
        ("https://api.github.com:8443/y", False),
        ("https://codeload.github.com/y", False),
    ],
)
def test_a_redirect_keeps_the_token_only_for_the_same_scheme_and_host(to: str, kept: bool) -> None:
    request = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer s3cret"})

    redirected = _SameHostAuthorization().redirect_request(request, None, 302, "Found", Message(), to)

    assert redirected is not None
    assert redirected.has_header("Authorization") is kept


def test_a_redirect_to_another_host_drops_the_token_and_one_to_the_same_host_keeps_it(server: str) -> None:
    other = server.replace("127.0.0.1", "localhost")

    assert http_get(f"{server}/redirect?to={other}/ok", {"Authorization": "Bearer s3cret"}) == b"body"
    assert http_get(f"{server}/redirect?to={server}/ok", {"Authorization": "Bearer s3cret"}) == b"body"

    assert "Authorization" not in Handler.seen[1]
    assert Handler.seen[3]["Authorization"] == "Bearer s3cret"


TLS = Path(__file__).parent / "fixtures" / "tls"


@pytest.fixture
def secure_server() -> Iterator[int]:
    """The same local server over TLS, with a certificate for ``localhost`` and ``127.0.0.1`` no system trusts; its
    port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS / "cert.pem", TLS / "key.pem")
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    Handler.seen = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("start", ["stall", "redirect?to=https://localhost:{port}/stall"])
def test_an_https_request_that_stops_sending_gives_up_once_nothing_arrives_for_the_stall(
    secure_server: int, start: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(TLS / "cert.pem"))
    url = f"https://127.0.0.1:{secure_server}/{start.format(port=secure_server)}"
    began = time.monotonic()

    with pytest.raises(FetchError, match="timed out"):
        http_get(url, {}, None, 1)

    assert time.monotonic() - began < 2.5
    assert http_get(f"https://localhost:{secure_server}/ok", {}, None, 1) == b"body"


def test_an_https_server_whose_certificate_the_system_doesn_t_trust_is_refused(
    secure_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    with pytest.raises(FetchError, match=r"fastro can't reach https://127\.0\.0\.1:\d+/ok: .*CERTIFICATE_VERIFY_FAILED"):
        http_get(f"https://127.0.0.1:{secure_server}/ok", {})


def test_a_request_that_stops_sending_gives_up_once_nothing_arrives_for_the_stall(server: str) -> None:
    started = time.monotonic()

    with pytest.raises(FetchError, match=rf"fastro can't reach {server}/stall: .*timed out"):
        http_get(f"{server}/stall", {}, None, 1)

    assert time.monotonic() - started < 2.5


def test_a_request_gives_up_connecting_after_30_seconds(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[object] = []
    real = socket.create_connection

    def recorded(address: Any, timeout: Any = None, *arguments: Any, **keywords: Any) -> socket.socket:
        timeouts.append(timeout)
        return real(address, timeout, *arguments, **keywords)

    monkeypatch.setattr(socket, "create_connection", recorded)

    assert http_get(f"{server}/ok", {}) == b"body"
    assert timeouts == [30]


def test_an_https_request_connects_and_shakes_hands_within_30_seconds_then_waits_the_stall_for_data(
    secure_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(TLS / "cert.pem"))
    handshakes: list[object] = []
    reads: list[object] = []
    wrap, connect = ssl.SSLContext.wrap_socket, sources._WaitingConnection.connect

    def recorded_wrap(context: ssl.SSLContext, sock: socket.socket, *arguments: Any, **keywords: Any) -> ssl.SSLSocket:
        if not keywords.get("server_side"):
            handshakes.append(sock.gettimeout())
        return wrap(context, sock, *arguments, **keywords)

    def recorded_connect(connection: Any) -> None:
        connect(connection)
        reads.append(connection.sock.gettimeout())

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", recorded_wrap)
    monkeypatch.setattr(sources._WaitingConnection, "connect", recorded_connect)

    assert http_get(f"https://localhost:{secure_server}/ok", {}, None, 77) == b"body"
    assert (handshakes, reads) == ([30], [77])
