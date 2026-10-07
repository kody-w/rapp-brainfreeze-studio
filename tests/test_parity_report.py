"""The parity line says what the proof covers and warns about what it does not."""
from brainfreeze_studio.__main__ import _parity_lines


def test_proven_agent_with_no_gaps_prints_one_line():
    prov = {"parity": {"A": {"passed": 9, "cases": 9, "parity": True}},
            "agents": [{"name": "A", "materialized": {"approximated_inputs": [], "blocked_operations": {}}}]}
    assert _parity_lines(prov, "      ") == ["parity:      A 9/9 PROVEN"]


def test_approximated_inputs_and_blocked_operations_are_named():
    prov = {"parity": {"A": {"passed": 9, "cases": 9, "parity": True}},
            "agents": [{"name": "A", "materialized": {"approximated_inputs": ["deals"],
                                                      "blocked_operations": {"price": ["discount"]}}}]}
    lines = _parity_lines(prov, "      ")
    assert lines[0].endswith("PROVEN")
    assert "deals" in lines[1] and "unknown" in lines[1]
    assert "A.price is blocked" in lines[2] and "discount" in lines[2]
