from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Contextualization target types
# ---------------------------------------------------------------------------

SpineTargetType = Literal[
    "DOMAIN",
    "PRODUCT_LINE",
    "FUNCTION",
    "SUB_FUNCTION",
]

NodeTypeTargetType = Literal["NODE_TYPE"]


class SpineTargetRef(BaseModel):
    """Reference to an ontology element on the canonical business spine."""

    type: SpineTargetType = Field(
        ...,
        description=(
            "Spine element being contextualized: DOMAIN, PRODUCT_LINE, "
            "FUNCTION, or SUB_FUNCTION."
        ),
    )
    canonical_id: str = Field(
        ...,
        min_length=1,
        description="Canonical ontology identifier for the target.",
    )


# Backward-compatible name used by the current Product Line tests/code.
ProductLineTarget = Literal["PRODUCT_LINE"]


class ProductLineTargetRef(SpineTargetRef):
    """Backward-compatible Product Line target reference."""

    type: ProductLineTarget = Field(
        ...,
        description="Canonical Product Line identifier type.",
    )


class SetApplicabilityChange(BaseModel):
    """
    Set whether a canonical spine element applies in the selected domain.

    Applicability is the mandatory contextualization decision. Alias and
    customer additions are optional.
    """

    operation: Literal["SET_APPLICABILITY"]
    target: SpineTargetRef
    applicable: bool


class SetAliasChange(BaseModel):
    """Set a customer-specific alias for a canonical spine element."""

    operation: Literal["SET_ALIAS"]
    target: SpineTargetRef
    alias: str = Field(
        ...,
        min_length=1,
        description="Customer-specific alias.",
    )


class CustomerProductLine(BaseModel):
    """Customer-specific Product Line added under a canonical domain."""

    id: str | None = Field(
        None,
        min_length=1,
        description="Optional client-generated draft identifier.",
    )
    domain_id: str = Field(
        ...,
        min_length=1,
        description="Canonical domain under which the Product Line is added.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Customer-specific Product Line name.",
    )
    description: str | None = Field(
        None,
        description="Description of the customer-specific Product Line.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Optional customer-specific aliases.",
    )


class AddProductLineChange(BaseModel):
    """Add a customer-specific Product Line."""

    operation: Literal["ADD_PRODUCT_LINE"]
    product_line: CustomerProductLine


class CustomerFunction(BaseModel):
    """Customer-specific Business Capability / Function."""

    id: str | None = Field(
        None,
        min_length=1,
        description="Optional client-generated draft identifier.",
    )
    parent_product_line_id: str = Field(
        ...,
        min_length=1,
        description="Product Line under which this Function is added.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Customer-specific Business Capability / Function name.",
    )
    description: str | None = Field(
        None,
        description="Description of the customer-specific Function.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Optional customer-specific aliases.",
    )


class AddFunctionChange(BaseModel):
    """Add a customer-specific Business Capability / Function."""

    operation: Literal["ADD_FUNCTION"]
    function: CustomerFunction


class CustomerSubFunction(BaseModel):
    """Customer-specific Capability / SubFunction."""

    id: str | None = Field(
        None,
        min_length=1,
        description="Optional client-generated draft identifier.",
    )
    parent_function_id: str = Field(
        ...,
        min_length=1,
        description="Function under which this SubFunction is added.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Customer-specific Capability / SubFunction name.",
    )
    description: str | None = Field(
        None,
        description="Description of the customer-specific SubFunction.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Optional customer-specific aliases.",
    )


class AddSubFunctionChange(BaseModel):
    """Add a customer-specific Capability / SubFunction."""

    operation: Literal["ADD_SUB_FUNCTION"]
    sub_function: CustomerSubFunction


# ---------------------------------------------------------------------------
# Batch request
# ---------------------------------------------------------------------------


ContextualizationChange = Annotated[
    SetApplicabilityChange
    | SetAliasChange
    | AddProductLineChange
    | AddFunctionChange
    | AddSubFunctionChange,
    Field(discriminator="operation"),
]


class CustomerOntologyContextualizationRequest(BaseModel):
    """
    Complete business-intent batch for one domain contextualization save.

    The UI may make many draft changes, but one successful request produces
    exactly one new immutable Customer Ontology version.
    """

    customer_id: str = Field(
        ...,
        min_length=1,
        description="External customer identifier.",
    )
    domain_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Canonical domain being contextualized. Both spine and node-type "
            "context are scoped to this domain."
        ),
    )
    base_customer_ontology_version: int = Field(
        ...,
        ge=1,
        description="Customer Ontology version the UI last loaded.",
    )
    request_id: str = Field(
        ...,
        min_length=1,
        description="Idempotency key for this save request.",
    )
    changes: list[ContextualizationChange] = Field(
        ...,
        min_length=1,
        description=(
            "Complete batch of draft changes to apply atomically in one save."
        ),
    )
    change_reason: str | None = Field(
        None,
        description="Optional reason for the contextualization change.",
    )


class CustomerOntologyContextualizationResponse(BaseModel):
    """Result of a successful Customer Ontology contextualization save."""

    customer_id: str
    domain_id: str
    customer_ontology_version: int
    status: str
    change_type: str
    change_summary: dict
    created_by: str
    confirmed_by: str | None = None
