from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    customer_id: str
    customer_name: str
    industry_id: str
