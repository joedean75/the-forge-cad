"""The part search finds what people and planners actually type.

The copilot logs show eight searches for a car motor that came back empty,
because the whole query was one substring: "540 motor" missed "540 can motor
(brushed)". These are those queries, and the catalog rows added for them.
"""

import pytest

from app.services import assembly_service, cad_operations


def _search(text):
    asm = assembly_service.create_assembly("search")
    env = cad_operations.execute("search_assembly_parts", asm.id, {"search": text},
                                 include_state=False)
    assert env["ok"], env.get("error")
    return env["result"]


@pytest.mark.parametrize("query, expected", [
    ("540 motor", "inrunner-540"),
    ("rc car motor", "inrunner-540"),
    ("a motor for my crawler", "inrunner-3650"),
    ("3650 brushless", "inrunner-3650"),
    ("2212 motor", "bldc-2212"),
    ("A2212", "bldc-2212"),
    ("m4 screw", "iso4762-m4-4"),
    ("608 bearing", "bearing-608"),
])
def test_what_was_asked_for_is_found(query, expected):
    result = _search(query)
    assert result["match"] == "all"
    assert expected in [p["key"] for p in result["parts"]]


def test_the_row_named_in_the_query_ranks_above_one_that_only_mentions_it():
    """The 540's notes say "3650 is 36 mm across"; asking for a 3650 should not
    put the 540 first."""
    keys = [p["key"] for p in _search("3650 brushless")["parts"]]
    assert keys[0] == "inrunner-3650"


def test_a_query_nothing_fully_matches_says_so():
    result = _search("m4 zeppelin")
    assert result["match"] == "partial" and result["parts"]
    assert _search("zeppelin quux")["match"] == "none"
