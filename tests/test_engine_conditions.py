"""The condition language behind every ``if = "…"`` (spec 1.3)."""

import re

import pytest

from fastro.engine.conditions import And, Condition, Feature, Not, Option, Or, atoms, parse
from fastro.engine.spec import SpecError


@pytest.mark.parametrize(
    ("text", "condition"),
    [
        ("admin", Feature("admin")),
        ("  admin\t", Feature("admin")),
        ("taskiq.broker=rabbitmq", Option("taskiq", "broker", "rabbitmq")),
        ("deploy.mode=prod-eu.2", Option("deploy", "mode", "prod-eu.2")),
        ("not taskiq", Not(Feature("taskiq"))),
        ("not not taskiq", Not(Not(Feature("taskiq")))),
        ("accounts and not taskiq", And((Feature("accounts"), Not(Feature("taskiq"))))),
        ("a and b and c", And((Feature("a"), Feature("b"), Feature("c")))),
        ("a or b or c", Or((Feature("a"), Feature("b"), Feature("c")))),
        ("(admin)", Feature("admin")),
        ("((admin))", Feature("admin")),
        ("not(admin)", Not(Feature("admin"))),
        ("(a or b)and c", And((Or((Feature("a"), Feature("b"))), Feature("c")))),
    ],
)
def test_a_condition_parses_into_its_tree(text: str, condition: Condition) -> None:
    assert parse(text) == condition


@pytest.mark.parametrize(
    ("text", "condition"),
    [
        ("a or b and c", Or((Feature("a"), And((Feature("b"), Feature("c")))))),
        ("a and b or c", Or((And((Feature("a"), Feature("b"))), Feature("c")))),
        ("not a and b", And((Not(Feature("a")), Feature("b")))),
        ("not a or b", Or((Not(Feature("a")), Feature("b")))),
        ("not (a or b)", Not(Or((Feature("a"), Feature("b"))))),
        ("a and (b or c)", And((Feature("a"), Or((Feature("b"), Feature("c")))))),
    ],
)
def test_not_binds_tighter_than_and_which_binds_tighter_than_or(text: str, condition: Condition) -> None:
    assert parse(text) == condition


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "expected a feature name, 'not' or '(' at the end"),
        ("   ", "expected a feature name, 'not' or '(' at the end"),
        ("and", "expected a feature name, 'not' or '(' at column 1, found 'and'"),
        ("admin and", "expected a feature name, 'not' or '(' at the end"),
        ("admin or or b", "expected a feature name, 'not' or '(' at column 10, found 'or'"),
        ("not", "expected a feature name, 'not' or '(' at the end"),
        ("admin taskiq", "expected 'and', 'or' or the end at column 7, found 'taskiq'"),
        ("admin)", "expected 'and', 'or' or the end at column 6, found ')'"),
        ("(admin", "expected ')' at the end"),
        ("(admin taskiq)", "expected ')' at column 8, found 'taskiq'"),
        ("()", "expected a feature name, 'not' or '(' at column 2, found ')'"),
        (")", "expected a feature name, 'not' or '(' at column 1, found ')'"),
        ("Admin", "'Admin' at column 1 isn't a feature name or 'feature.option=value'"),
        ("2fa", "'2fa' at column 1 isn't a feature name or 'feature.option=value'"),
        ("admin && b", "expected 'and', 'or' or the end at column 7, found '&&'"),
        ("admin and && b", "'&&' at column 11 isn't a feature name or 'feature.option=value'"),
        ("taskiq.broker", "'taskiq.broker' at column 1 isn't a feature name or 'feature.option=value'"),
        ("taskiq.broker=", "'taskiq.broker=' at column 1 isn't a feature name or 'feature.option=value'"),
        ("taskiq.Broker=redis", "'taskiq.Broker=redis' at column 1 isn't a feature name or 'feature.option=value'"),
        ("taskiq.broker==redis", "'taskiq.broker==redis' at column 1 isn't a feature name or 'feature.option=value'"),
        ("taskiq=redis", "'taskiq=redis' at column 1 isn't a feature name or 'feature.option=value'"),
        ("a.b.c=redis", "'a.b.c=redis' at column 1 isn't a feature name or 'feature.option=value'"),
        ("or.x=y", "'or.x=y' at column 1 isn't a feature name or 'feature.option=value'"),
        ("a and not.x=y", "'not.x=y' at column 7 isn't a feature name or 'feature.option=value'"),
    ],
)
def test_text_outside_the_grammar_is_refused_with_where_and_what(text: str, why: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a condition: {why}.")):
        parse(text)


@pytest.mark.parametrize(
    "text",
    ["not " * 100 + "a", "(" * 100 + "a" + ")" * 100, "not (" * 50 + "a" + ")" * 50, " and ".join(["not (a)"] * 101)],
)
def test_a_condition_may_nest_100_levels(text: str) -> None:
    assert set(atoms(parse(text))) == {Feature("a")}


@pytest.mark.parametrize(
    ("text", "column"),
    [
        ("not " * 101 + "a", 401),
        ("(" * 101 + "a" + ")" * 101, 101),
        ("not (" * 50 + "not a" + ")" * 50, 251),
        ("not " * 5000 + "a", 401),
        ("(" * 5000 + "a" + ")" * 5000, 101),
    ],
)
def test_a_condition_nested_deeper_than_100_levels_is_refused(text: str, column: int) -> None:
    with pytest.raises(SpecError, match=re.escape(f"nests deeper than 100 levels at column {column}.")):
        parse(text)


def test_a_long_condition_is_quoted_shortened_in_its_error() -> None:
    text = "a and " * 20 + "2fa"

    with pytest.raises(SpecError, match=re.escape(f"{text[:60]!r}... isn't a condition: '2fa' at column 121")):
        parse(text)


def test_atoms_lists_every_name_a_condition_mentions_in_order() -> None:
    condition = parse("accounts and not (taskiq.broker=rabbitmq or admin) or accounts")

    assert atoms(condition) == [
        Feature("accounts"),
        Option("taskiq", "broker", "rabbitmq"),
        Feature("admin"),
        Feature("accounts"),
    ]
