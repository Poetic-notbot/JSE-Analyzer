"""Seed profile: a reconciled, ILLUSTRATIVE Jamaica FY2026/27 opportunity tree.

IMPORTANT: the amounts below are PLACEHOLDER / illustrative values (JMD '000)
used to demonstrate the reconciliation engine end-to-end. They are flagged as
low-confidence, manual-method figures. Replace them with verified figures via
the review queue fed from the official PDFs in config/sources.json.

Unit convention: every 'amount' is JMD thousands ('000).

The tree is intentionally built so that:
  * ROOT = Central Government + Public Body Capex (kept SEPARATE then consolidated)
  * each PARENT reconciles to its CHILD/BALANCE children
  * a BALANCE node absorbs the unallocated remainder (computed, not new money)
  * CROSS_CUT nodes (e.g. 'Health opportunities') are attached for analysis only
"""
from __future__ import annotations

from datetime import datetime

from ..models import (AccessPath, ExtractionMethod, FundingStream, Ledger,
                      LedgerNode, NodeType, Source)
from ..reconciliation import compute_balance


def _src(title: str, conf: float = 0.3, page=None, quote=None) -> Source:
    return Source(
        source_id=title[:10].lower().replace(" ", "_"),
        title=title,
        url="https://www.mof.gov.jm/budget",
        page=page, quote=quote,
        extraction_method=ExtractionMethod.MANUAL, confidence=conf,
        retrieved_at=datetime(2026, 3, 1),
    )


# ---- illustrative top lines (JMD '000) -- replace with verified figures ----
CENTRAL_TOTAL = 1_150_000_000
PUBLIC_BODY_CAPEX = 200_000_000
ROOT_TOTAL = CENTRAL_TOTAL + PUBLIC_BODY_CAPEX  # 1,350,000,000

# central government split
RECURRENT = 980_000_000
CAPITAL = CENTRAL_TOTAL - RECURRENT  # 170,000,000

# recurrent split
COMP_EMPLOYEES = 330_000_000
DEBT_SERVICE = 360_000_000
PROGRAMMES = 200_000_000
# recurrent balance absorbs the rest (computed)


def build_ledger() -> Ledger:
    led = Ledger()

    def add(node_id, name, ntype, amount, parent_id=None, **kw):
        led.nodes.append(LedgerNode(
            node_id=node_id, name=name, node_type=ntype, amount=amount,
            parent_id=parent_id, source=kw.pop("source", _src(name)), **kw))

    # ROOT
    add("root", "Master Opportunity Total (GOJ FY2026/27)", NodeType.ROOT,
        ROOT_TOTAL, verified=True)

    # Level 1 -- central vs public bodies kept separate, then consolidated to root
    add("central", "Central Government Budget", NodeType.PARENT, CENTRAL_TOTAL,
        "root", verified=True)
    add("pb_capex", "Public Bodies Capital Expenditure", NodeType.PARENT,
        PUBLIC_BODY_CAPEX, "root", funding_stream=FundingStream.PUBLIC_BODY_CAPEX,
        access_path=AccessPath.PUBLIC_BODY_INVESTMENT, verified=True)

    # Level 2 -- central government
    add("recurrent", "Recurrent Expenditure", NodeType.PARENT, RECURRENT,
        "central", funding_stream=FundingStream.CENTRAL_RECURRENT, verified=True)
    add("capital", "Capital Expenditure (Central)", NodeType.PARENT, CAPITAL,
        "central", funding_stream=FundingStream.CENTRAL_CAPITAL, verified=True)

    # Level 3 -- recurrent components
    add("comp_emp", "Compensation of Employees", NodeType.CHILD, COMP_EMPLOYEES,
        "recurrent", funding_stream=FundingStream.CENTRAL_RECURRENT,
        access_path=AccessPath.NON_OPPORTUNITY, object_class="Compensation of Employees",
        verified=True)
    add("debt", "Public Debt Service (Interest)", NodeType.CHILD, DEBT_SERVICE,
        "recurrent", funding_stream=FundingStream.CENTRAL_RECURRENT,
        access_path=AccessPath.NON_OPPORTUNITY, object_class="Interest Payment",
        verified=True)
    add("programmes", "Recurrent Programmes (goods & services)", NodeType.CHILD,
        PROGRAMMES, "recurrent", funding_stream=FundingStream.CENTRAL_RECURRENT,
        access_path=AccessPath.PROCUREMENT_SUPPLIER, verified=True)
    # BALANCE absorbs remainder of recurrent (computed, not new money)
    add("recurrent_bal", "Recurrent -- Other / Unallocated (Balance)", NodeType.BALANCE,
        0.0, "recurrent", funding_stream=FundingStream.CENTRAL_RECURRENT,
        access_path=AccessPath.CITIZEN_WELFARE,
        source=Source(source_id="computed", title="Computed remainder",
                      extraction_method=ExtractionMethod.COMPUTED, confidence=1.0))

    # Level 3 -- capital (central) example projects
    add("cap_roads", "Major Roads & Bridges Programme", NodeType.CHILD, 90_000_000,
        "capital", funding_stream=FundingStream.CENTRAL_CAPITAL,
        access_path=AccessPath.PROCUREMENT_CONTRACTOR, ministry="Economic Growth & Job Creation",
        project="Road Rehabilitation", verified=True)
    add("cap_water", "Water Supply Infrastructure", NodeType.CHILD, 40_000_000,
        "capital", funding_stream=FundingStream.CENTRAL_CAPITAL,
        access_path=AccessPath.PROCUREMENT_CONTRACTOR, ministry="Economic Growth & Job Creation",
        verified=True)
    add("cap_path", "PATH Social Welfare Transfers", NodeType.CHILD, 25_000_000,
        "capital", funding_stream=FundingStream.CENTRAL_CAPITAL,
        access_path=AccessPath.CITIZEN_WELFARE, ministry="Labour & Social Security",
        verified=True)
    add("capital_bal", "Capital -- Other Projects (Balance)", NodeType.BALANCE, 0.0,
        "capital", funding_stream=FundingStream.CENTRAL_CAPITAL,
        access_path=AccessPath.PROCUREMENT_CONSULTANT,
        source=Source(source_id="computed", title="Computed remainder",
                      extraction_method=ExtractionMethod.COMPUTED, confidence=1.0))

    # Level 2 -- public body capex example
    add("pb_nwc", "National Water Commission -- Capex", NodeType.CHILD, 120_000_000,
        "pb_capex", funding_stream=FundingStream.PUBLIC_BODY_CAPEX,
        access_path=AccessPath.PUBLIC_BODY_INVESTMENT, public_body="National Water Commission",
        agency="NWC", verified=True)
    add("pb_jps", "Airports/Ports Authority -- Capex", NodeType.CHILD, 80_000_000,
        "pb_capex", funding_stream=FundingStream.PUBLIC_BODY_CAPEX,
        access_path=AccessPath.PUBLIC_BODY_INVESTMENT, public_body="Port/Airport Authority",
        verified=True)

    # ---- compute BALANCE nodes (remainders, NOT new money) ----
    led.by_id("recurrent_bal").amount = compute_balance(led, "recurrent", "recurrent_bal")
    led.by_id("capital_bal").amount = compute_balance(led, "capital", "capital_bal")

    # ---- CROSS_CUT analytical view: 'Health opportunities' (excluded from totals) ----
    add("xc_health", "CROSS-CUT: Health-sector opportunities (view only)",
        NodeType.CROSS_CUT, 55_000_000, "root",
        access_path=AccessPath.PROCUREMENT_SUPPLIER,
        source=_src("Analytical cross-cut", conf=0.5))

    return led
