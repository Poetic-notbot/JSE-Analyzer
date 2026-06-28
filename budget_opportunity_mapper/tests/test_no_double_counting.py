"""Tests proving no double-counting and that cross-cut views never inflate totals."""
from datetime import datetime

from budget_mapper.models import (AccessPath, ExtractionMethod, Ledger,
                                   LedgerNode, NodeType, Source)
from budget_mapper.profiles import build_ledger
from budget_mapper.reconciliation import (validate_cross_cut_excluded,
                                          validate_no_double_counting)


def _src():
    return Source(source_id="t", title="test",
                  extraction_method=ExtractionMethod.MANUAL, confidence=1.0,
                  retrieved_at=datetime(2026, 1, 1))


def _node(nid, ntype, amt, parent=None, ap=AccessPath.NON_OPPORTUNITY):
    return LedgerNode(node_id=nid, name=nid, node_type=ntype, amount=amt,
                      parent_id=parent, access_path=ap, source=_src())


def test_seed_no_double_counting():
    assert validate_no_double_counting(build_ledger()) == []


def test_leaf_sum_equals_root():
    """ROOT == sum of CHILD/BALANCE leaves (parents not double-counted)."""
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 100),
        _node("p1", NodeType.PARENT, 70, "root"),
        _node("p2", NodeType.PARENT, 30, "root"),
        _node("c1", NodeType.CHILD, 40, "p1"),
        _node("c2", NodeType.CHILD, 30, "p1"),
        _node("c3", NodeType.CHILD, 30, "p2"),
    ]
    assert validate_no_double_counting(led) == []


def test_detects_double_counting():
    """If a parent's amount is added on top of its children, totals break."""
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 170),  # wrongly counts p1 AND its children
        _node("p1", NodeType.PARENT, 70, "root"),
        _node("extra", NodeType.CHILD, 100, "root"),
        _node("c1", NodeType.CHILD, 40, "p1"),
        _node("c2", NodeType.CHILD, 30, "p1"),
    ]
    # leaves = c1+c2+extra = 170, root = 170 -> OK here; now inflate root
    led.by_id("root").amount = 240  # double counts p1 (70) on top
    assert validate_no_double_counting(led)


def test_cross_cut_excluded_from_totals():
    """Adding a CROSS_CUT node must NOT change the reconciled leaf total."""
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 100),
        _node("c1", NodeType.CHILD, 60, "root"),
        _node("c2", NodeType.CHILD, 40, "root"),
    ]
    assert validate_no_double_counting(led) == []
    # attach a big cross-cut analytical view
    led.nodes.append(_node("xc", NodeType.CROSS_CUT, 999, "root"))
    # still balances because cross-cut is excluded
    assert validate_no_double_counting(led) == []
    # and the cross-cut has money siblings, so it is not flagged
    assert validate_cross_cut_excluded(led) == []


def test_cross_cut_with_no_money_siblings_flagged():
    led = Ledger()
    led.nodes = [
        _node("root", NodeType.ROOT, 100),
        _node("p", NodeType.PARENT, 100, "root"),
        _node("c", NodeType.CHILD, 100, "p"),
        _node("xc", NodeType.CROSS_CUT, 50, "p"),
    ]
    # 'xc' parent 'p' DOES have money sibling 'c' -> not flagged
    assert validate_cross_cut_excluded(led) == []
