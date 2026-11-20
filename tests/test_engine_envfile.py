"""``.env.example`` processed by its ``# @fastro`` markers for a selection: sections kept or dropped, option lines
rewritten, markers removed (spec 1.9, 6.1, A4 item 4)."""

from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.envfile import EnvFileError, lines, process
from fastro.engine.resolve import select

FIXTURES = Path(__file__).parent / "fixtures"
ENVFILE = FIXTURES / "envfile"
CACHE = 'choices = ["memory", "redis"]\ndefault = "memory"\n'
BILLING = 'choices = ["stripe", "paddle"]\ndefault = "stripe"\n'
TEXT = (
    (FIXTURES / "blueprints" / "mini" / "blueprint.toml")
    .read_text()
    .replace(
        CACHE,
        CACHE + 'env = { CACHE_BACKEND = "{value}", CACHE_URL = "{value}://localhost", CACHE_KEY = "{value}:{key}:{value}" }\n',
    )
    .replace(BILLING, BILLING + 'env = { BILLING_PROVIDER = "{value}" }\n')
)
CATALOG = blueprint.load("blueprint.toml", TEXT, Version("0.1.0"))
HEADER = "# ===\n# Cache\n# ===\n"


def _process(text: str, features: tuple[str, ...] = (), options: dict[tuple[str, str], str] | None = None) -> str:
    return process(text, CATALOG, select(CATALOG, features, options=options))


@pytest.mark.parametrize(
    ("expected", "features", "options"),
    [
        ("core", (), {}),
        ("cache-redis", ("cache",), {("cache", "backend"): "redis"}),
        ("cache-memory-billing-paddle", ("cache", "billing"), {("billing", "provider"): "paddle"}),
        ("billing-admin", ("billing", "admin"), {}),
    ],
)
def test_the_fixture_gives_each_selection_its_file(
    expected: str, features: tuple[str, ...], options: dict[tuple[str, str], str]
) -> None:
    given = (ENVFILE / "input.env").read_text()

    assert _process(given, features, options) == (ENVFILE / f"{expected}.env").read_text()


def test_a_section_whose_condition_is_false_goes_up_to_the_next_header() -> None:
    given = f"A=1\n\n{HEADER}# @fastro if=cache\nB=2\n\n# ===\n# Next\n# ===\nC=3\n"

    assert _process(given) == "A=1\n\n# ===\n# Next\n# ===\nC=3\n"


def test_the_last_section_goes_to_the_end_of_the_file() -> None:
    assert _process(f"A=1\n{HEADER}# @fastro if=cache\nB=2\n") == "A=1\n"


def test_a_dropped_last_section_without_a_final_newline_goes_to_the_end() -> None:
    assert _process(f"A=1\n{HEADER}# @fastro if=cache\nB=2") == "A=1"


def test_a_section_whose_body_starts_with_a_rule_is_dropped_whole() -> None:
    given = "# ===\n# A\n# ===\n# @fastro if=cache\n# ===\nB=1\n# ===\n# N\n# ===\nC=1\n"

    assert _process(given) == "# ===\n# N\n# ===\nC=1\n"


@pytest.mark.parametrize("trailing", [" ", "\t", "\r"])
def test_trailing_whitespace_doesnt_hide_a_header_or_a_marker(trailing: str) -> None:
    rule = f"# ==={trailing}"
    given = (
        f"{rule}\n# Cache\n{rule}\n# @fastro if=cache{trailing}\n# @fastro option=cache.backend{trailing}\n"
        f"CACHE_BACKEND=memory\n{rule}\n# Redis\n{rule}\n# @fastro if=cache.backend=memory{trailing}\nR=1\n"
    )

    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == (f"{rule}\n# Cache\n{rule}\nCACHE_BACKEND=redis\n")


def test_a_rule_and_a_comment_without_a_closing_rule_dont_end_a_section() -> None:
    assert _process(f"{HEADER}# @fastro if=cache\nB=1\n# ===\n# note\nC=1\n") == ""


def test_a_kept_section_loses_only_its_marker() -> None:
    assert _process(f"{HEADER}# @fastro if=cache\nB=2\n", ("cache",)) == f"{HEADER}B=2\n"


def test_a_header_s_rules_can_be_any_length() -> None:
    given = "# =\n# Cache\n# ==========\n# @fastro if=cache\nB=2\n"

    assert _process(given) == ""


def test_an_option_line_is_rewritten_from_its_template_and_the_rest_of_the_line_is_replaced() -> None:
    given = "# @fastro option=cache.backend\nCACHE_URL=memory://elsewhere # note\n"

    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == "CACHE_URL=redis://localhost\n"


def test_only_value_in_braces_is_substituted_and_other_braces_stay() -> None:
    given = "# @fastro option=cache.backend\nCACHE_KEY=x\n"

    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == "CACHE_KEY=redis:{key}:redis\n"


def test_a_block_of_rules_without_a_title_isnt_a_header() -> None:
    with pytest.raises(EnvFileError, match="^line 4: '# @fastro if=' sits"):
        _process("# ===\n# ===\n# ===\n# @fastro if=cache\nB=2\n")


def test_an_option_of_a_feature_the_selection_doesnt_have_keeps_its_line() -> None:
    assert _process("# @fastro option=cache.backend\nCACHE_BACKEND=redis\n") == "CACHE_BACKEND=redis\n"


@pytest.mark.parametrize(
    "given",
    [
        "# @fastro option=cache.backend\nOTHER=1\n",
        "# @fastro option=cache.backend\n# a comment\n",
        "# @fastro option=cache.backend\n",
        "# @fastro option=cache.nope\nCACHE_BACKEND=x\n",
        "# @fastro option=nope.backend\nCACHE_BACKEND=x\n",
    ],
    ids=["another-var", "not-an-assignment", "last-line", "unknown-option", "unknown-feature"],
)
def test_a_marker_that_marks_no_line_of_its_option_is_removed_and_changes_nothing_else(given: str) -> None:
    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == given.split("\n", 1)[1]


@pytest.mark.parametrize("separator", ["\r", "\f", "\u2028", "\x0b", "\x85"])
def test_lines_split_at_newline_only(separator: str) -> None:
    given = f"# @fastro option=cache.backend\nCACHE_BACKEND=memory{separator}tail\n"

    assert lines(given) == ["# @fastro option=cache.backend", f"CACHE_BACKEND=memory{separator}tail", ""]
    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == "CACHE_BACKEND=redis\n"


def test_a_byte_order_mark_is_kept_and_doesnt_hide_a_marker_on_the_first_line() -> None:
    given = "\ufeff# @fastro option=cache.backend\nCACHE_BACKEND=memory\n"

    assert _process(given, ("cache",), {("cache", "backend"): "redis"}) == "\ufeffCACHE_BACKEND=redis\n"


def test_a_file_with_no_markers_comes_back_as_it_was() -> None:
    given = "# ===\n# App\n# ===\nA=1\r\n\nB=2"

    assert _process(given, ("cache",)) == given


def test_a_marker_fastro_doesnt_know_is_refused_in_a_section_that_goes_too() -> None:
    with pytest.raises(EnvFileError, match="^line 5: '# @fastro iff=x' isn't a marker"):
        _process(f"{HEADER}# @fastro if=cache\n# @fastro iff=x\n")


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (
            "A=1\n# @fastro if=cache\nB=2\n",
            "line 2: '# @fastro if=' sits on its own line directly below a section header, a block of '# ===' lines "
            "and a title.",
        ),
        (
            f"{HEADER}\n# @fastro if=cache\nB=2\n",
            "line 5: '# @fastro if=' sits on its own line directly below a section header, a block of '# ===' lines "
            "and a title.",
        ),
        (f"{HEADER}# @fastro if=cache and\nB=2\n", "line 4: "),
        (f"{HEADER}# @fastro if=cachee\nB=2\n", "line 4: 'cachee' isn't a feature of this catalog."),
        (f"{HEADER}# @fastro if=cache.size=big\nB=2\n", "line 4: 'cache.size' isn't an option of cache."),
        (f"{HEADER}# @fastro if=cache.backend=disk\nB=2\n", "line 4: 'disk' isn't a choice of cache.backend."),
        (
            "# ===\n# Cache\n# not a rule\n# @fastro if=cache\n",
            "line 4: '# @fastro if=' sits on its own line directly below a section header, a block of '# ===' lines "
            "and a title.",
        ),
        ("A=1\n# @fastro iff=cache\n", "line 2: '# @fastro iff=cache' isn't a marker"),
    ],
    ids=[
        "not-below-a-header",
        "blank-line-between",
        "bad-grammar",
        "unknown-feature",
        "unknown-option",
        "bad-choice",
        "no-closing-rule",
        "unknown-marker",
    ],
)
def test_a_marker_that_breaks_6_1_is_refused_naming_its_line(given: str, said: str) -> None:
    with pytest.raises(EnvFileError) as raised:
        _process(given, ("cache",))

    assert str(raised.value).startswith(said)
