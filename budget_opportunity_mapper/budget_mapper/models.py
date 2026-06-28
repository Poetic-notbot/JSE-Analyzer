"""Pydantic data models for the reconciled opportunity ledger.

Core invariants enforced here (structurally) and in reconciliation.py (numerically):
  - exactly one ROOT
  - every non-root node has exactly one parent
  - CROSS_CUT nodes never contribute to budget totals
  - BALANCE nodes are computed remainders, flagged as such
  - every figure carries full provenance
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class NodeType(str, Enum):
    ROOT = "ROOT"            # master total, no parent
    PARENT = "PARENT"        # reconciles to its children
    CHILD = "CHILD"          # belongs to exactly one parent, leaf money
    BALANCE = "BALANCE"      # computed remainder, NOT new money
    CROSS_CUT = "CROSS_CUT"  # analytical view only, never added to totals


class FundingStream(str, Enum):
    CENTRAL_RECURRENT = "central_recurrent"
    CENTRAL_CAPITAL = "central_capital"
    PUBLIC_BODY_CAPEX = "public_body_capex"
    PUBLIC_BODY_OPEX = "public_body_opex"
    NONE = "none"  # for ROOT / balances / cross-cuts


class AccessPath(str, Enum):
    """How an external party can access the money (the 'opportunity' angle)."""
    PROCUREMENT_CONTRACTOR = "contractor"      # works/construction
    PROCUREMENT_SUPPLIER = "supplier"          # goods
    PROCUREMENT_CONSULTANT = "consultant"      # services/consulting
    SME = "sme"                                # SME-set-aside / small value
    CITIZEN_WELFARE = "citizen_welfare"        # transfers to people
    FINANCING = "financing"                    # loans/equity/debt
    PUBLIC_BODY_INVESTMENT = "public_body"     # SOE capex projects
    NON_OPPORTUNITY = "non_opportunity"        # comp of employees, debt service


class ExtractionMethod(str, Enum):
    WEB_PDF = "web_pdf"
    UPLOADED_PDF = "uploaded_pdf"
    MANUAL = "manual"
    COMPUTED = "computed"


class Source(BaseModel):
    """Provenance attached to every figure."""
    source_id: str
    title: str
    url: Optional[str] = None
    page: Optional[int] = None            # page number if PDF
    quote: Optional[str] = None           # snippet supporting the figure
    extraction_method: ExtractionMethod
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    retrieved_at: Optional[datetime] = None


class LedgerNode(BaseModel):
    """A single line in the reconciled budget/opportunity tree."""
    node_id: str
    name: str
    node_type: NodeType
    amount: float = Field(ge=0.0)         # JMD '000 (consistent unit, see profile)

    parent_id: Optional[str] = None

    # Classification
    funding_stream: FundingStream = FundingStream.NONE
    access_path: AccessPath = AccessPath.NON_OPPORTUNITY
    ministry: Optional[str] = None
    agency: Optional[str] = None
    programme: Optional[str] = None
    project: Optional[str] = None
    object_class: Optional[str] = None    # object/economic classification
    public_body: Optional[str] = None

    # Workflow
    verified: bool = False                # verified ledger value vs review-queue
    is_computed_balance: bool = False

    source: Source

    @model_validator(mode="after")
    def _structural_rules(self) -> "LedgerNode":
        if self.node_type == NodeType.ROOT and self.parent_id is not None:
            raise ValueError("ROOT node must not have a parent_id")
        if self.node_type != NodeType.ROOT and not self.parent_id:
            raise ValueError(f"Non-root node '{self.node_id}' must have exactly one parent_id")
        if self.node_type == NodeType.BALANCE:
            object.__setattr__(self, "is_computed_balance", True)
        return self

    @field_validator("amount")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("amount must be finite")
        return v


class ReviewItem(BaseModel):
    """An extracted-but-unverified figure awaiting human acceptance."""
    review_id: str
    proposed_node: LedgerNode
    reason: str = "newly extracted figure"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"  # pending | accepted | rejected


class ReconResult(BaseModel):
    parent_id: str
    parent_name: str
    parent_amount: float
    children_sum: float
    difference: float
    status: str          # OK | WARNING | ERROR
    child_count: int


class Ledger(BaseModel):
    """The full set of nodes plus the review queue."""
    nodes: list[LedgerNode] = Field(default_factory=list)
    review_queue: list[ReviewItem] = Field(default_factory=list)

    def by_id(self, node_id: str) -> Optional[LedgerNode]:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def children_of(self, node_id: str) -> list[LedgerNode]:
        return [n for n in self.nodes if n.parent_id == node_id]

    def root(self) -> Optional[LedgerNode]:
        roots = [n for n in self.nodes if n.node_type == NodeType.ROOT]
        return roots[0] if roots else None

    def money_nodes(self) -> list[LedgerNode]:
        """Nodes that count toward totals (excludes CROSS_CUT)."""
        return [n for n in self.nodes if n.node_type != NodeType.CROSS_CUT]
