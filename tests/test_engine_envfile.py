"""``.env.example`` processed by its ``# @fastro`` markers for a selection: sections kept or dropped, option lines
rewritten, markers removed, and every marker and marked line checked (spec 1.9, 6.1, A4 items 4 and 5a)."""

from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.envfile import EnvFileError, lines, marked, problems, process, refused_lines, unmarked
from fastro.engine.resolve import select

FIXTURES = Path(__file__).parent / "fixtures"
ENVFILE = FIXTURES / "envfile"
MINI = (FIXTURES / "blueprints" / "mini" / "blueprint.toml").read_text()
CACHE = 'choices = ["memory", "redis"]\ndefault = "memory"\n'
BILLING = 'choices = ["stripe", "paddle"]\ndefault = "stripe"\n'
HEADER = "# ===\n# Cache\n# ===\n"


def _catalog(cache: str = "", billing: str = "") -> Blueprint:
    text = MINI.replace(CACHE, CACHE + (f"env = {{ {cache} }}\n" if cache else ""))
    text = text.replace(BILLING, BILLING + (f"env = {{ {billing} }}\n" if billing else ""))
    return blueprint.load("blueprint.toml", text, Version("0.1.0"))


PLAIN = _catalog()
BACKEND = _catalog('CACHE_BACKEND = "{value}"')
FIXTURE = _catalog(
    'CACHE_BACKEND = "{value}", CACHE_URL = "{value}://localhost", CACHE_KEY = "{value}:{key}:{value}"',
    'BILLING_PROVIDER = "{value}"',
)
MARKED = "# @fastro option=cache.backend\nCACHE_BACKEND=memory\n"
REDIS = {("cache", "backend"): "redis"}


def _process(
    text: str,
    features: tuple[str, ...] = (),
    options: dict[tuple[str, str], str] | None = None,
    catalog: Blueprint = PLAIN,
) -> str:
    return process(text, catalog, select(catalog, features, options=options))


@pytest.mark.parametrize(
    ("expected", "features", "options"),
    [
        ("core", (), {}),
        ("cache-redis", ("cache",), REDIS),
        ("cache-memory-billing-paddle", ("cache", "billing"), {("billing", "provider"): "paddle"}),
        ("billing-admin", ("billing", "admin"), {}),
    ],
)
def test_the_fixture_gives_each_selection_its_file(
    expected: str, features: tuple[str, ...], options: dict[tuple[str, str], str]
) -> None:
    given = (ENVFILE / "input.env").read_text()

    assert _process(given, features, options, FIXTURE) == (ENVFILE / f"{expected}.env").read_text()


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

    assert _process(given, ("cache",), REDIS, BACKEND) == (f"{rule}\n# Cache\n{rule}\nCACHE_BACKEND=redis\n")


def test_a_rule_and_a_comment_without_a_closing_rule_dont_end_a_section() -> None:
    assert _process(f"{HEADER}# @fastro if=cache\nB=1\n# ===\n# note\nC=1\n") == ""


def test_a_kept_section_loses_only_its_marker() -> None:
    assert _process(f"{HEADER}# @fastro if=cache\nB=2\n", ("cache",)) == f"{HEADER}B=2\n"


def test_a_header_s_rules_can_be_any_length() -> None:
    given = "# =\n# Cache\n# ==========\n# @fastro if=cache\nB=2\n"

    assert _process(given) == ""


def test_an_option_line_is_rewritten_from_its_template_and_the_rest_of_the_line_is_replaced() -> None:
    given = "# @fastro option=cache.backend\nCACHE_URL=memory://elsewhere # note\n"

    assert _process(given, ("cache",), REDIS, _catalog('CACHE_URL = "{value}://localhost"')) == "CACHE_URL=redis://localhost\n"


def test_only_value_in_braces_is_substituted_and_other_braces_stay() -> None:
    given = "# @fastro option=cache.backend\nCACHE_KEY=x\n"

    assert (
        _process(given, ("cache",), REDIS, _catalog('CACHE_KEY = "{value}:{key}:{value}"')) == "CACHE_KEY=redis:{key}:redis\n"
    )


def test_a_rewritten_line_keeps_its_carriage_return() -> None:
    given = "# @fastro option=cache.backend\r\nCACHE_BACKEND=memory\r\nA=1\r\n"

    assert _process(given, ("cache",), REDIS, BACKEND) == "CACHE_BACKEND=redis\r\nA=1\r\n"


def test_a_block_of_rules_without_a_title_isnt_a_header() -> None:
    with pytest.raises(EnvFileError, match="^line 4: '# @fastro if=' sits"):
        _process("# ===\n# ===\n# ===\n# @fastro if=cache\nB=2\n")


def test_an_option_of_a_feature_the_selection_doesnt_have_keeps_its_line() -> None:
    assert _process(MARKED, catalog=BACKEND) == "CACHE_BACKEND=memory\n"


@pytest.mark.parametrize("separator", ["\r", "\f", "\u2028", "\x0b", "\x85"])
def test_lines_split_at_newline_only(separator: str) -> None:
    given = f"# @fastro option=cache.backend\nCACHE_BACKEND=memory{separator}tail\n"

    assert lines(given) == ["# @fastro option=cache.backend", f"CACHE_BACKEND=memory{separator}tail", ""]
    assert _process(given, ("cache",), REDIS, BACKEND) == "CACHE_BACKEND=redis\n"


def test_a_byte_order_mark_is_kept_and_doesnt_hide_a_marker_on_the_first_line() -> None:
    given = "\ufeff" + MARKED

    assert _process(given, ("cache",), REDIS, BACKEND) == "\ufeffCACHE_BACKEND=redis\n"


def test_a_file_with_no_markers_comes_back_as_it_was() -> None:
    given = "# ===\n# App\n# ===\nA=1\r\n\nB=2"

    assert _process(given, ("cache",)) == given


def test_a_marker_fastro_doesnt_know_is_refused_in_a_section_that_goes_too() -> None:
    with pytest.raises(EnvFileError, match="^line 5: '# @fastro iff=x' isn't a marker"):
        _process(f"{HEADER}# @fastro if=cache\n# @fastro iff=x\n")


def test_an_option_marker_is_checked_in_a_section_that_goes_too() -> None:
    with pytest.raises(EnvFileError, match="^line 5: 'cache.nope' isn't an option of this catalog."):
        _process(f"{HEADER}# @fastro if=cache\n# @fastro option=cache.nope\nCACHE_BACKEND=x\n")


SITS = "'# @fastro option=' sits on its own line directly above a line that starts with VAR=."


@pytest.mark.parametrize(
    ("given", "said"),
    [
        ("A=1\n# @fastro if=cache\nB=2\n", f"line 2: {'# @fastro if='!r} sits on its own line directly below a section header"),
        (f"{HEADER}\n# @fastro if=cache\nB=2\n", "line 5: '# @fastro if=' sits on its own line directly below"),
        (f"{HEADER}# @fastro if=cache and\nB=2\n", "line 4: "),
        (f"{HEADER}# @fastro if=cachee\nB=2\n", "line 4: 'cachee' isn't a feature of this catalog."),
        (f"{HEADER}# @fastro if=cache.size=big\nB=2\n", "line 4: 'cache.size' isn't an option of cache."),
        (f"{HEADER}# @fastro if=cache.backend=disk\nB=2\n", "line 4: 'disk' isn't a choice of cache.backend."),
        ("# ===\n# Cache\n# not a rule\n# @fastro if=cache\n", "line 4: '# @fastro if=' sits on its own line"),
        ("A=1\n# @fastro iff=cache\n", "line 2: '# @fastro iff=cache' isn't a marker"),
        (MARKED + "# @fastro option=cache.nope\nCACHE_BACKEND=x\n", "line 3: 'cache.nope' isn't an option of this catalog."),
        (
            MARKED + "# @fastro option=nope.backend\nCACHE_BACKEND=x\n",
            "line 3: 'nope.backend' isn't an option of this catalog.",
        ),
        (MARKED + "# @fastro option=cache\nCACHE_BACKEND=x\n", "line 3: 'cache' isn't an option of this catalog."),
        (MARKED + "# @fastro option=cache.backend\n# a comment\n", f"line 3: {SITS}"),
        (MARKED + "# @fastro option=cache.backend\n", f"line 3: {SITS}"),
        (MARKED + "# @fastro option=cache.backend", f"line 3: {SITS}"),
        (MARKED + "# @fastro option=cache.backend\n1VAR=x\n", f"line 3: {SITS}"),
        (MARKED + "# @fastro option=cache.backend\nOTHER=1\n", "line 3: OTHER isn't in the env table of cache.backend."),
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
        "option-unknown-option",
        "option-unknown-feature",
        "option-no-dot",
        "option-above-a-comment",
        "option-above-nothing",
        "option-last-line",
        "option-above-a-bad-name",
        "option-above-another-var",
    ],
)
def test_a_marker_that_breaks_6_1_is_refused_naming_its_line(given: str, said: str) -> None:
    with pytest.raises(EnvFileError) as raised:
        _process(given, ("cache",), catalog=BACKEND)

    assert str(raised.value).startswith(said)


@pytest.mark.parametrize("features", [(), ("cache",)], ids=["unselected", "selected"])
def test_a_var_with_no_marked_line_is_refused_whether_or_not_its_option_is_selected(features: tuple[str, ...]) -> None:
    with pytest.raises(EnvFileError) as raised:
        _process("CACHE_BACKEND=memory\n", features, catalog=BACKEND)

    assert (
        str(raised.value)
        == "CACHE_BACKEND has no '# @fastro option=cache.backend' marker directly above a CACHE_BACKEND= line."
    )


def test_a_var_marked_twice_is_refused_at_the_second_naming_both() -> None:
    with pytest.raises(EnvFileError) as raised:
        _process(MARKED + "A=1\n" + MARKED, catalog=BACKEND)

    assert str(raised.value) == (
        "line 4: CACHE_BACKEND has 2 '# @fastro option=cache.backend' markers, on lines 1 and 4, and needs one."
    )


def test_a_marked_line_in_a_dropped_section_counts() -> None:
    assert _process(f"{HEADER}# @fastro if=cache\n{MARKED}", catalog=BACKEND) == ""


def test_marker_lines_come_before_vars_and_each_list_is_in_file_order() -> None:
    given = "# @fastro iff=a\nCACHE_BACKEND=1\n# @fastro option=cache.nope\nX=1\n"

    assert [str(error) for error in problems(given, BACKEND)] == [
        "line 1: '# @fastro iff=a' isn't a marker fastro knows: use '# @fastro if=' or '# @fastro option='.",
        "line 3: 'cache.nope' isn't an option of this catalog.",
        "CACHE_BACKEND has no '# @fastro option=cache.backend' marker directly above a CACHE_BACKEND= line.",
    ]
    assert [error.number for error in refused_lines(given, BACKEND)] == [1, 3]
    assert [error.number for error in unmarked(given, BACKEND)] == [None]


def test_marked_maps_each_option_and_var_to_its_marker_lines() -> None:
    given = MARKED + "# @fastro option=cache.backend\nCACHE_URL=x\n# @fastro option=x.y\n# note\n" + MARKED

    assert marked(given) == {("cache.backend", "CACHE_BACKEND"): [1, 7], ("cache.backend", "CACHE_URL"): [3]}


def test_a_byte_order_mark_doesnt_hide_a_refused_marker_on_the_first_line() -> None:
    assert [error.number for error in problems("\ufeff# @fastro iff=x\n", PLAIN)] == [1]


def test_a_marked_var_on_the_last_line_without_a_final_newline_is_rewritten() -> None:
    given = "# @fastro option=cache.backend\nCACHE_BACKEND=memory"

    assert problems(given, BACKEND) == []
    assert _process(given, ("cache",), REDIS, BACKEND) == "CACHE_BACKEND=redis"
