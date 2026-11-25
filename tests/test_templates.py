"""Rendering a blueprint template is bounded by size, never by a clock (spec 6.2): each rendered file stops at
10 MB, counted as it's produced, and a sandboxed ``*``, ``+`` or ``**`` whose result would pass its limit fails
before it computes, and Python's integer digit limit is 4,300 while it renders, whatever the interpreter's own
setting."""

import sys
from collections.abc import Iterator, Mapping

import pytest

from fastro.templates import TemplateError, render

MB10 = 10**7
STRING = "a.txt.j2 doesn't render: '{op}' would give a string over 10 MB, the most fastro builds in a template (6.2)."
ITEMS = (
    "a.txt.j2 doesn't render: '{op}' would give a list or tuple of more than 10,000,000 items, the most fastro builds "
    "in a template (6.2)."
)
DIGITS = "a.txt.j2 doesn't render: '{op}' would give an integer of more than 4,300 digits, the most fastro writes out (6.2)."


@pytest.fixture
def digits() -> Iterator[None]:
    before = sys.get_int_max_str_digits()
    yield
    sys.set_int_max_str_digits(before)


def _render(text: str, values: Mapping[str, object] | None = None) -> str:
    return render("a.txt.j2", text.encode(), values or {})


def _refused(text: str, values: Mapping[str, object] | None = None) -> str:
    with pytest.raises(TemplateError) as caught:
        _render(text, values)
    return str(caught.value)


def test_a_file_of_exactly_10_mb_renders_and_one_byte_more_doesn_t() -> None:
    assert len(_render("{{ x }}", {"x": "a" * MB10}).encode()) == MB10

    assert _refused("{{ x }}{{ y }}", {"x": "a" * MB10, "y": "b"}) == (
        "a.txt.j2 doesn't render: its output is over 10 MB, the most fastro writes from one template (6.2)."
    )


def test_the_file_limit_counts_utf_8_bytes_not_characters() -> None:
    assert "is over 10 MB" in _refused("{% for _ in range(n) %}{{ x }}{% endfor %}", {"n": 6, "x": "é" * 10**6})


def test_output_holding_a_character_utf_8_can_t_encode_fails_to_render() -> None:
    assert _refused('{{ "\\ud800" }}') == (
        "a.txt.j2 doesn't render: its output holds a character UTF-8 can't encode, so it can't be written."
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{{ ('a' * 10000001) | length }}", STRING),
        ("{{ (10000001 * 'a') | length }}", STRING),
        ("{{ ('é' * 5000001) | length }}", STRING),
        ("{{ (true * x) | length }}", STRING),
        ("{{ ('a' * 10**8) | length }}", STRING),
        ("{{ ([1] * 10000001) | length }}", ITEMS),
        ("{{ ((1,) * 10000001) | length }}", ITEMS),
    ],
)
def test_a_repeated_string_list_or_tuple_past_its_limit_fails_before_it_s_built(text: str, message: str) -> None:
    assert _refused(text, {"x": "a" * MB10 + "b"}) == message.format(op="*")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{ ('a' * 10000000) | length }}", MB10),
        ("{{ ([1] * 10000000) | length }}", MB10),
        ("{{ ('a' * -5) | length }}", 0),
        ("{{ (x * 0) | length }}", 0),
    ],
)
def test_a_repeated_value_at_its_limit_renders(text: str, expected: int) -> None:
    assert int(_render(text, {"x": "a" * 20})) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{{ (x + y) | length }}", STRING),
        ("{{ ((x | safe) + y) | length }}", STRING),
        ("{{ (y + (x | safe)) | length }}", STRING),
        ("{{ (xs + ys) | length }}", ITEMS),
        ("{{ (ts + us) | length }}", ITEMS),
    ],
)
def test_joined_strings_lists_or_tuples_past_their_limit_fail_before_they_re_built(text: str, message: str) -> None:
    values = {"x": "a" * MB10, "y": "b", "xs": [1] * MB10, "ys": [2], "ts": (1,) * MB10, "us": (2,)}

    assert _refused(text, values) == message.format(op="+")


def test_joined_values_at_their_limit_render() -> None:
    assert _render("{{ (x + y) | length }}", {"x": "a" * (MB10 - 1), "y": "b"}) == str(MB10)


def test_a_list_joined_to_a_tuple_isn_t_measured_and_fails_as_python_says() -> None:
    message = _refused("{{ xs + ts }}", {"xs": [1] * MB10, "ts": (1,)})

    assert "10 MB" not in message and "can only concatenate list" in message


@pytest.mark.parametrize(
    "text",
    [
        "{{ 10 ** 4300 }}",
        "{{ 2 ** 14285 }}",
        "{{ 3 ** 14000 }}",
        "{{ (-3) ** 9013 }}",
        "{{ 10 ** 100000000 }}",
        "{{ 99 ** 99999999 }}",
    ],
)
def test_a_power_past_4300_digits_fails_before_it_computes(text: str) -> None:
    assert _refused(text) == DIGITS.format(op="**")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{ (9 ** 4506) | string | length }}", 4300),
        ("{{ (2 ** 14284) | string | length }}", 4300),
        ("{{ (3 ** 9012) | string | length }}", 4300),
        ("{{ ((-10) ** 4299) | string | length }}", 4301),
        ("{{ (1 ** 100000000) | string | length }}", 1),
        ("{{ (0 ** 100000000) | string | length }}", 1),
        ("{{ ((-1) ** 100000001) | string | length }}", 2),
        ("{{ (2 ** -5) | string | length }}", 7),
    ],
)
def test_a_power_of_4300_digits_or_fewer_renders(text: str, expected: int) -> None:
    assert int(_render(text)) == expected


def test_a_product_past_4300_digits_fails_before_it_computes() -> None:
    values = {"a": 10**2150, "b": 10**2150, "big": 10**4299}

    for text in ("{{ a * b }}", "{{ -a * b }}", "{{ a * -b }}", "{{ big * 10 }}"):
        assert _refused(text, values) == DIGITS.format(op="*")


def test_a_product_of_4300_digits_or_fewer_renders() -> None:
    values = {"a": 10**2150 - 1, "b": 10**2150 + 1, "big": 10**4299, "zero": 0}

    assert _render("{{ (a * b) | string | length }}", values) == "4300"
    assert _render("{{ (a * -b) | string | length }}", values) == "4301"
    assert _render("{{ (big * 9) | string | length }}", values) == "4300"
    assert _render("{{ zero * big }}", values) == "0"
    assert _render("{{ zero * huge }}{{ huge * zero }}", {**values, "huge": 10**5000}) == "00"


def test_the_largest_integer_of_4300_digits_to_the_power_1_renders() -> None:
    assert _render("{{ (n ** 1) | string | length }}", {"n": 10**4300 - 1}) == "4300"


def test_a_power_whose_bits_land_on_the_bound_is_still_refused() -> None:
    assert _refused("{{ b ** 5 }}", {"b": 2**2857 - 1}) == DIGITS.format(op="**")


def test_a_lone_surrogate_counts_as_the_three_bytes_python_would_pass_it_as() -> None:
    assert _refused("{{ (x * n) | length }}", {"x": "\ud800", "n": 3333334}) == STRING.format(op="*")


@pytest.mark.usefixtures("digits")
def test_an_interpreter_set_below_4300_digits_still_renders_4300_and_keeps_its_setting() -> None:
    sys.set_int_max_str_digits(1000)

    assert _render("{{ (9 ** 4506) | string | length }}") == "4300"
    assert sys.get_int_max_str_digits() == 1000


@pytest.mark.usefixtures("digits")
def test_an_interpreter_with_no_digit_limit_still_stops_at_4300_and_keeps_its_setting() -> None:
    sys.set_int_max_str_digits(0)

    assert "Exceeds the limit (4300 digits)" in _refused("{{ n }}", {"n": 10**4300})
    assert sys.get_int_max_str_digits() == 0


@pytest.mark.usefixtures("digits")
def test_an_integer_literal_past_4300_digits_is_refused_whatever_the_interpreter_s_setting() -> None:
    sys.set_int_max_str_digits(0)

    assert "Exceeds the limit (4300 digits)" in _refused("{{ " + "9" * 4301 + " }}")
    assert sys.get_int_max_str_digits() == 0
