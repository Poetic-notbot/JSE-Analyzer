"""Heuristic classification of budget lines into opportunity access paths.

Business rules (from spec):
  - Compensation of employees   -> NON_OPPORTUNITY
  - Debt service / interest     -> NON_OPPORTUNITY (financing, not procurement)
  - Welfare transfers (PATH etc)-> CITIZEN_WELFARE (not procurement)
  - Construction/works          -> contractor
  - Goods/supplies              -> supplier
  - Services/consulting         -> consultant
  - Small-value / SME set-aside -> sme
  - Public body capex           -> public_body (separately labelled)
"""
from __future__ import annotations

import re

from .models import AccessPath, FundingStream, LedgerNode

_RULES: list[tuple[AccessPath, re.Pattern]] = [
    (AccessPath.NON_OPPORTUNITY, re.compile(
        r"compensation of employ|wages|salar|personal emolument|"
        r"interest payment|debt servic|amortization|amortisation|"
        r"public debt|sinking fund|pension(?!s? administration)", re.I)),
    (AccessPath.CITIZEN_WELFARE, re.compile(
        r"\bPATH\b|welfare|social assist|grant to individual|"
        r"poor relief|cash transfer|subsid(y|ies) to household|"
        r"student loan|scholarship|benefit", re.I)),
    (AccessPath.FINANCING, re.compile(
        r"\bloan\b|equity injection|on-lending|guarantee|"
        r"capital subscription|contingenc", re.I)),
    (AccessPath.PROCUREMENT_CONTRACTOR, re.compile(
        r"construct|road|bridge|civil works|rehabilitat|infrastructure|"
        r"building works|drainage|water supply works|housing development", re.I)),
    (AccessPath.PROCUREMENT_CONSULTANT, re.compile(
        r"consult|advisory|technical assist|feasibility|design service|"
        r"audit service|legal service|training service", re.I)),
    (AccessPath.PROCUREMENT_SUPPLIER, re.compile(
        r"supply of|procurement of|purchase of|equipment|furniture|"
        r"vehicle|pharmaceutic|medical supplies|ICT hardware|software licen|"
        r"goods|materials|uniform", re.I)),
]


def classify_access_path(node: LedgerNode) -> AccessPath:
    """Return the access path for a node based on its text + funding stream."""
    # Public body capex is always separately labelled as a public-body opportunity
    if node.funding_stream == FundingStream.PUBLIC_BODY_CAPEX:
        return AccessPath.PUBLIC_BODY_INVESTMENT

    text = " ".join(filter(None, [
        node.name, node.programme, node.project,
        node.object_class, node.public_body,
    ]))
    for path, pattern in _RULES:
        if pattern.search(text):
            return path

    # default: capital lines lean procurement, recurrent goods lean supplier
    if node.funding_stream == FundingStream.CENTRAL_CAPITAL:
        return AccessPath.PROCUREMENT_CONTRACTOR
    return AccessPath.NON_OPPORTUNITY


def is_sme_accessible(node: LedgerNode, sme_threshold: float) -> bool:
    """SME-accessible = a procurement opportunity below an SME value threshold.

    Threshold is in the same unit as amounts (JMD '000). Non-procurement paths
    are never SME opportunities.
    """
    procurement = {
        AccessPath.PROCUREMENT_CONTRACTOR,
        AccessPath.PROCUREMENT_SUPPLIER,
        AccessPath.PROCUREMENT_CONSULTANT,
    }
    return node.access_path in procurement and node.amount <= sme_threshold


def apply_classification(nodes: list[LedgerNode]) -> list[LedgerNode]:
    for n in nodes:
        if n.access_path == AccessPath.NON_OPPORTUNITY:
            n.access_path = classify_access_path(n)
    return nodes
