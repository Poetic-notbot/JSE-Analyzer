"""Unit tests for reconciliation logic and structural validators.

Run from the budget_opportunity_mapper/ folder:
    pytest -q
"""
from datetime import datetime

import pytest

from budget_mapper.models import (ExtractionMethod, Ledger, LedgerNode,
                                   NodeType, Source)
from budget_mapper.profiles import build_ledger
from budget_mapper.reconciliation import (compute_balance, is_healthy,
                                          reconcile, validate_all,
                                          validate_balance_marked,
                                          validate_cross_cut_excluded,
                                          validate_root_equals_children,
                                          validate_single_parent,
                                          validate_single_root)


def _src():
    return Source(source_id="t", title="test",
                  extraction_method=ExtractionMethod.MANUAL, confidence=1.0,
                  retrieved_at=datetime(2026, 1, 1))


def _node(nid, ntype, amt, parent=None):
    return LedgerNode(node_id=nid, name=nid, node_type=ntype, amount=amt,
                      parent_id=parent, source=_src())


def simple_ledger():
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 100),
        _node("a", NodeType.CHILD, 60, "root"),
        _node("b", NodeType.CHILD, 40, "root"),
    ]
    return led


def test_seed_profile_is_healthy():
    assert is_healthy(build_ledger())


def test_root_equals_children_ok():
    led = simple_ledger()
    assert validate_root_equals_children(led) == []


def test_root_not_equal_children_errors():
    led = simple_ledger()
    led.by_id("b").amount = 10  # 60 + 10 != 100
    assert validate_root_equals_children(led)


def test_reconcile_reports_difference():
    led = simple_ledger()
    led.by_id("b").amount = 30  # sum 90 vs 100
    res = {r.parent_id: r for r in reconcile(led)}
    assert res["root"].difference == pytest.approx(10)
    assert res["root"].status in ("WARNING", "ERROR")


def test_single_root_enforced():
    led = simple_ledger()
    assert validate_single_root(led) == []
    led.nodes.append(_node("root2", NodeType.ROOT, 5))
    assert validate_single_root(led)


def test_single_parent_detects_missing_parent():
    led = simple_ledger()
    led.by_id("a").parent_id = "ghost"
    assert validate_single_parent(led)


def test_single_parent_detects_cycle():
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 100),
        _node("a", NodeType.PARENT, 100, "root"),
        _node("b", NodeType.CHILD, 100, "a"),
    ]
    led.by_id("root").parent_id = "b"
    led.by_id("root").node_type = NodeType.PARENT  # force a cycle
    assert validate_single_parent(led)


def test_compute_balance():
    led = Ledger()
    led.nodes = [
        _node("p", NodeType.PARENT, 100),
        _node("c1", NodeType.CHILD, 70, "p"),
        _node("bal", NodeType.BALANCE, 0, "p"),
    ]
    led.by_id("p").parent_id = None
    led.by_id("p").node_type = NodeType.ROOT
    assert compute_balance(led, "p", "bal") == pytest.approx(30)


def test_balance_marked_computed():
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 50),
        _node("bal", NodeType.BALANCE, 50, "root"),
    ]
    assert led.by_id("bal").is_computed_balance is True
    assert validate_balance_marked(led) == []


def test_validate_all_keys():
    keys = set(validate_all(build_ledger()).keys())
    assert keys == {"single_root", "single_parent", "root_equals_children",
                    "cross_cut_excluded", "balance_marked", "no_double_counting"}
