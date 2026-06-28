"""Reconciliation engine and structural validators.

This module is the heart of the 'no double-counting / account-for-every-dollar'
guarantee. It is intentionally framework-free and fully unit-tested.
"""
from __future__ import annotations

from .models import Ledger, LedgerNode, NodeType, ReconResult

# tolerance is relative; figures are in JMD '000 so allow small rounding noise
OK_TOL = 0.005      # 0.5%
WARN_TOL = 0.02     # 2%


def _status(parent_amount: float, children_sum: float) -> str:
    if parent_amount == 0:
        return "OK" if children_sum == 0 else "ERROR"
    diff_ratio = abs(parent_amount - children_sum) / abs(parent_amount)
    if diff_ratio <= OK_TOL:
        return "OK"
    if diff_ratio <= WARN_TOL:
        return "WARNING"
    return "ERROR"


def reconcile(ledger: Ledger) -> list[ReconResult]:
    """For every PARENT (and ROOT), compare its amount to the sum of its children.

    BALANCE children ARE included in the sum (they are real remainders of that
    parent's money). CROSS_CUT nodes are NEVER included.
    """
    results: list[ReconResult] = []
    for node in ledger.nodes:
        if node.node_type not in (NodeType.ROOT, NodeType.PARENT):
            continue
        children = [c for c in ledger.children_of(node.node_id)
                    if c.node_type != NodeType.CROSS_CUT]
        if not children:
            continue
        csum = sum(c.amount for c in children)
        results.append(ReconResult(
            parent_id=node.node_id,
            parent_name=node.name,
            parent_amount=node.amount,
            children_sum=csum,
            difference=round(node.amount - csum, 4),
            status=_status(node.amount, csum),
            child_count=len(children),
        ))
    return results


def compute_balance(ledger: Ledger, parent_id: str, balance_node_id: str) -> float:
    """Compute a BALANCE node as parent - sum(other non-cross-cut children)."""
    parent = ledger.by_id(parent_id)
    if parent is None:
        raise ValueError(f"unknown parent {parent_id}")
    others = [c for c in ledger.children_of(parent_id)
              if c.node_id != balance_node_id and c.node_type != NodeType.CROSS_CUT]
    return round(parent.amount - sum(c.amount for c in others), 4)


# ---------- Structural validators (build requirements 6-9) ----------

def validate_single_root(ledger: Ledger) -> list[str]:
    roots = [n for n in ledger.nodes if n.node_type == NodeType.ROOT]
    if len(roots) == 1:
        return []
    return [f"Expected exactly 1 ROOT, found {len(roots)}: {[r.node_id for r in roots]}"]


def validate_single_parent(ledger: Ledger) -> list[str]:
    """Every non-root node references exactly one existing parent; no cycles."""
    errors: list[str] = []
    ids = {n.node_id for n in ledger.nodes}
    for n in ledger.nodes:
        if n.node_type == NodeType.ROOT:
            continue
        if not n.parent_id:
            errors.append(f"{n.node_id}: missing parent")
        elif n.parent_id not in ids:
            errors.append(f"{n.node_id}: parent '{n.parent_id}' does not exist")
        elif n.parent_id == n.node_id:
            errors.append(f"{n.node_id}: node is its own parent")
    for n in ledger.nodes:
        seen, cur = set(), n
        while cur and cur.parent_id:
            if cur.parent_id in seen:
                errors.append(f"{n.node_id}: cycle detected via {cur.parent_id}")
                break
            seen.add(cur.parent_id)
            cur = ledger.by_id(cur.parent_id)
    return errors


def validate_root_equals_children(ledger: Ledger) -> list[str]:
    """ROOT amount must equal the sum of its DIRECT children (excl. cross-cut)."""
    root = ledger.root()
    if root is None:
        return ["No ROOT node present"]
    kids = [c for c in ledger.children_of(root.node_id)
            if c.node_type != NodeType.CROSS_CUT]
    csum = sum(c.amount for c in kids)
    if _status(root.amount, csum) == "ERROR":
        return [f"ROOT {root.amount:,.0f} != sum(direct children) {csum:,.0f} "
                f"(diff {root.amount - csum:,.0f})"]
    return []


def validate_cross_cut_excluded(ledger: Ledger) -> list[str]:
    """CROSS_CUT nodes must not be the sole basis of a money parent's total."""
    errors: list[str] = []
    for n in ledger.nodes:
        if n.node_type != NodeType.CROSS_CUT:
            continue
        siblings = ledger.children_of(n.parent_id) if n.parent_id else []
        money_sibs = [s for s in siblings if s.node_type != NodeType.CROSS_CUT]
        if n.parent_id and not money_sibs:
            errors.append(f"CROSS_CUT {n.node_id} attached to parent {n.parent_id} "
                          f"with no money siblings - risk of being counted")
    return errors


def validate_balance_marked(ledger: Ledger) -> list[str]:
    errors: list[str] = []
    for n in ledger.nodes:
        if n.node_type == NodeType.BALANCE and not n.is_computed_balance:
            errors.append(f"BALANCE {n.node_id} not marked as computed remainder")
        if n.is_computed_balance and n.node_type != NodeType.BALANCE:
            errors.append(f"{n.node_id} marked computed but not typed BALANCE")
    return errors


def validate_no_double_counting(ledger: Ledger) -> list[str]:
    """Sum of all leaf money (CHILD + BALANCE) under ROOT must equal ROOT."""
    root = ledger.root()
    if root is None:
        return ["No ROOT node"]

    def money_children(nid: str) -> list[LedgerNode]:
        return [c for c in ledger.children_of(nid) if c.node_type != NodeType.CROSS_CUT]

    leaves: list[LedgerNode] = []

    def walk(nid: str):
        kids = money_children(nid)
        if not kids:
            node = ledger.by_id(nid)
            if node and node.node_type != NodeType.ROOT:
                leaves.append(node)
            return
        for k in kids:
            walk(k.node_id)

    walk(root.node_id)
    leaf_sum = sum(l.amount for l in leaves)
    if _status(root.amount, leaf_sum) == "ERROR":
        return [f"Double-counting / gap: ROOT {root.amount:,.0f} != leaf sum "
                f"{leaf_sum:,.0f} (diff {root.amount - leaf_sum:,.0f})"]
    return []


def validate_all(ledger: Ledger) -> dict[str, list[str]]:
    return {
        "single_root": validate_single_root(ledger),
        "single_parent": validate_single_parent(ledger),
        "root_equals_children": validate_root_equals_children(ledger),
        "cross_cut_excluded": validate_cross_cut_excluded(ledger),
        "balance_marked": validate_balance_marked(ledger),
        "no_double_counting": validate_no_double_counting(ledger),
    }


def is_healthy(ledger: Ledger) -> bool:
    return all(len(v) == 0 for v in validate_all(ledger).values())
