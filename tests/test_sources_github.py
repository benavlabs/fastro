"""Resolving a ``gh:`` source (spec 1.7): a given ref to its commit, or the newest release whose catalog accepts this
fastro, through GitHub's API, with ``GITHUB_TOKEN`` sent as a header and never anywhere else."""

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from packaging.version import Version

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

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.requests.append((url, dict(headers)))
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


def test_with_no_ref_the_newest_release_whose_catalog_accepts_this_fastro_is_taken() -> None:
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
                    _release("v0.22.0rc1", prerelease=True),
                    _release("v0.22.0"),
                    _release("v1/x+y&z"),
                    _release("v0.21.0"),
                    _release("v0.20.0"),
                ),
                (_contents("v0.22.0"), _catalog(">=9")),
                (_contents("v1%2Fx%2By%26z"), _catalog().replace(b'"bp"', b'"b\xff"')),
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
        _contents("v1%2Fx%2By%26z"),
        _contents("v0.21.0"),
        _commit("v0.21.0"),
    ]
    listing, contents = fake.requests[0][1], fake.requests[1][1]
    assert (listing["Accept"], listing["X-GitHub-Api-Version"]) == ("application/vnd.github+json", "2022-11-28")
    assert contents["Accept"] == "application/vnd.github.raw+json"


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

    assert len(with_token) == len(fake.requests) == 3
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


def test_a_body_cut_short_is_a_fetch_failure(server: str) -> None:
    with pytest.raises(FetchError, match="can't reach"):
        http_get(f"{server}/short", {})


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
