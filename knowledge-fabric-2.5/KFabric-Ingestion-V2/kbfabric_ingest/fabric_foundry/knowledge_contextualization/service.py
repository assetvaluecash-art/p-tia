from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from pathlib import Path
from typing import Any

import yaml

from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.identity import canonical_spine_id
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRecord,
    CustomerOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.ontology.registry import (
    get_ontology_definition,
    get_ontology_path,
)
from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


class KnowledgeContextualizationError(Exception):
    """Base error for customer ontology contextualization."""


class CanonicalOntologyNotFoundError(KnowledgeContextualizationError):
    """Raised when the customer's allocated canonical ontology cannot be resolved."""


class CanonicalOntologyArtifactError(KnowledgeContextualizationError):
    """Raised when the canonical ontology artifact cannot be loaded or validated."""


class KnowledgeContextualizationService:
    """
    Build immutable customer ontology versions from canonical ontology artifacts.

    Version 1 is an exact customer-owned snapshot of the active canonical
    ontology. Customer-specific context is added by subsequent contextualization
    operations, producing new immutable versions.
    """

    def __init__(
        self,
        *,
        customer_repository: CustomerRepository | None = None,
        industry_ontology_repository: IndustryOntologyRepository | None = None,
        customer_ontology_repository: CustomerOntologyRepository | None = None,
    ) -> None:
        self.customer_repository = customer_repository or CustomerRepository()
        self.industry_ontology_repository = (
            industry_ontology_repository or IndustryOntologyRepository()
        )
        self.customer_ontology_repository = (
            customer_ontology_repository or CustomerOntologyRepository()
        )

    def initialize_customer_ontology(
        self,
        *,
        customer_id: str,
        created_by: str = "SYSTEM",
        change_reason: str | None = None,
    ) -> CustomerOntologyRecord:
        """
        Create customer ontology version 1 from the customer's active
        industry-to-ontology allocation.
        """
        normalized_customer_id = customer_id.strip()
        normalized_created_by = created_by.strip()

        if not normalized_customer_id:
            raise ValueError("customer_id must not be blank")

        if not normalized_created_by:
            raise ValueError("created_by must not be blank")

        customer = self.customer_repository.get_by_customer_id(
            customer_id=normalized_customer_id
        )

        if customer is None:
            raise ValueError(
                f"Customer not found: {normalized_customer_id}"
            )

        existing = self.customer_ontology_repository.get_active(
            normalized_customer_id
        )
        if existing is not None:
            return existing

        allocation = self._resolve_canonical_allocation(
            customer.industry_id
        )

        ontology_definition = get_ontology_definition(
            ontology_id=allocation.ontology_id,
            version=allocation.ontology_version,
        )

        if ontology_definition is None:
            raise CanonicalOntologyNotFoundError(
                "No registered canonical ontology matches the customer's "
                f"industry allocation: {customer.industry_id}"
            )

        ontology_path = get_ontology_path(
            ontology_id=ontology_definition.ontology_id,
            version=ontology_definition.version,
        )

        if ontology_path is None:
            raise CanonicalOntologyNotFoundError(
                "Canonical ontology path could not be resolved for "
                f"{ontology_definition.ontology_id} "
                f"version {ontology_definition.version}"
            )

        definition = self._load_canonical_ontology(ontology_path)

        change_summary = self._build_initial_change_summary(
            definition=definition,
            ontology_definition=ontology_definition,
        )

        record = self.customer_ontology_repository.create_customer_ontology(
            customer_id=customer.customer_id,
            ontology_id=ontology_definition.ontology_id,
            ontology_version=ontology_definition.version,
            customer_ontology_version=1,
            status="ACTIVE",
            definition=definition,
            change_type="INITIAL",
            change_reason=change_reason,
            change_summary=change_summary,
            parent_customer_ontology_id=None,
            created_by=normalized_created_by,
            confirmed_by=normalized_created_by,
        )

        logger.info(
            "knowledge_contextualization.customer_ontology.initialized",
            customer_id=customer.customer_id,
            tenant_id=customer.tenant_id,
            ontology_id=record.ontology_id,
            ontology_version=record.ontology_version,
            customer_ontology_version=record.customer_ontology_version,
            customer_ontology_id=str(record.customer_ontology_id),
        )

        return record

    def contextualize_customer_ontology(
        self,
        request: CustomerOntologyContextualizationRequest,
        *,
        actor_id: str,
    ) -> CustomerOntologyRecord:
        """Apply one atomic domain-scoped contextualization batch.

        Canonical ontology values are never mutated. Customer context is stored
        under the selected domain, and the complete resulting definition is
        persisted as exactly one immutable Customer Ontology version.
        """
        customer_id = request.customer_id.strip()
        domain_id = request.domain_id.strip()
        actor_id = actor_id.strip()

        if not actor_id:
            raise KnowledgeContextualizationError(
                "Authenticated actor is required"
            )

        created_by = actor_id
        confirmed_by = actor_id

        customer = self.customer_repository.get_by_customer_id(customer_id=customer_id)
        if customer is None:
            raise KnowledgeContextualizationError(
                f"Customer not found: {customer_id}"
            )

        replay = self.customer_ontology_repository.get_by_request_id(
            customer_id=customer_id,
            request_id=request.request_id,
        )
        if replay is not None:
            logger.info(
                "knowledge_contextualization.customer_ontology.contextualize.replay",
                customer_id=customer_id,
                request_id=request.request_id,
                customer_ontology_version=replay.customer_ontology_version,
            )
            return replay

        active = self.customer_ontology_repository.get_active(customer_id)
        if active is None:
            raise KnowledgeContextualizationError(
                f"Customer Ontology not initialized: {customer_id}"
            )

        if active.customer_ontology_version != request.base_customer_ontology_version:
            raise KnowledgeContextualizationError(
                "Customer Ontology version conflict: "
                f"expected {request.base_customer_ontology_version}, "
                f"current {active.customer_ontology_version}"
            )

        definition = deepcopy(active.definition)
        domains = definition.get("domains")
        if not isinstance(domains, list):
            raise CanonicalOntologyArtifactError(
                "Customer Ontology definition must contain a domains list"
            )

        domain = next(
            (d for d in domains if d.get("name") == domain_id),
            None,
        )
        if domain is None:
            raise KnowledgeContextualizationError(
                f"Domain not found in Customer Ontology: {domain_id}"
            )

        context = domain.setdefault("customer_context", {})
        spine_context = context.setdefault("spine", {})
        pl_context = spine_context.setdefault("product_lines", {})
        fn_context = spine_context.setdefault("functions", {})
        sf_context = spine_context.setdefault("sub_functions", {})
        additions = context.setdefault(
            "additions",
            {"product_lines": [], "functions": [], "sub_functions": []},
        )

        def pl_ctx(name):
            return pl_context.setdefault(name, {"applicable": None, "aliases": []})

        def fn_ctx(pl_name, name):
            return fn_context.setdefault(
                pl_name, {}
            ).setdefault(name, {"applicable": None, "aliases": []})

        def sf_ctx(pl_name, fn_name, name):
            return sf_context.setdefault(pl_name, {}).setdefault(fn_name, {}).setdefault(
                name, {"applicable": None, "aliases": []}
            )

        def find_pl(canonical_id):
            for pl in domain.get("spine", []):
                if canonical_spine_id(
                    domain_id,
                    "PRODUCT_LINE",
                    pl["ProductLine"],
                ) == canonical_id:
                    return pl
            return None

        def find_function(canonical_id):
            for pl in domain.get("spine", []):
                for fn in pl.get("functions", []):
                    if canonical_spine_id(
                        domain_id,
                        "FUNCTION",
                        pl["ProductLine"],
                        fn["Function"],
                    ) == canonical_id:
                        return pl, fn
            return None

        def find_sub_function(canonical_id):
            for pl in domain.get("spine", []):
                for fn in pl.get("functions", []):
                    for sf in fn.get("subfunctions", []):
                        if canonical_spine_id(
                            domain_id,
                            "SUB_FUNCTION",
                            pl["ProductLine"],
                            fn["Function"],
                            sf,
                        ) == canonical_id:
                            return pl, fn, sf
            return None


        for change in request.changes:
            operation = change.operation

            if operation == "SET_APPLICABILITY":
                target = change.target
                if target.type == "PRODUCT_LINE":
                    found = find_pl(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(
                            f"Product Line not found in domain {domain_id}: {target.canonical_id}"
                        )
                    pl_ctx(found["ProductLine"])["applicable"] = change.applicable
                elif target.type == "FUNCTION":
                    found = find_function(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(
                            f"Function not found in domain {domain_id}: {target.canonical_id}"
                        )
                    fn_ctx(found[0]["ProductLine"], found[1]["Function"])["applicable"] = change.applicable
                elif target.type == "SUB_FUNCTION":
                    found = find_sub_function(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(
                            f"SubFunction not found in domain {domain_id}: {target.canonical_id}"
                        )
                    sf_ctx(found[0]["ProductLine"], found[1]["Function"], found[2])["applicable"] = change.applicable
                elif target.type == "DOMAIN":
                    context["applicable"] = change.applicable

            elif operation == "SET_ALIAS":
                target = change.target
                if target.type == "PRODUCT_LINE":
                    found = find_pl(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(
                            f"Product Line not found in domain {domain_id}: {target.canonical_id}"
                        )
                    pl_ctx(found["ProductLine"])["aliases"] = [change.alias]
                elif target.type == "FUNCTION":
                    found = find_function(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(f"Function not found: {target.canonical_id}")
                    fn_ctx(found[0]["ProductLine"], found[1]["Function"])["aliases"] = [change.alias]
                elif target.type == "SUB_FUNCTION":
                    found = find_sub_function(target.canonical_id)
                    if found is None:
                        raise KnowledgeContextualizationError(f"SubFunction not found: {target.canonical_id}")
                    sf_ctx(found[0]["ProductLine"], found[1]["Function"], found[2])["aliases"] = [change.alias]
                elif target.type == "DOMAIN":
                    context["aliases"] = [change.alias]

            elif operation == "ADD_PRODUCT_LINE":
                item = change.product_line
                if item.domain_id != domain_id:
                    raise KnowledgeContextualizationError(
                        f"Added Product Line domain mismatch: {item.domain_id} != {domain_id}"
                    )
                item_id = item.id or f"customer-pl-{uuid4().hex}"
                additions["product_lines"].append({
                    "id": item_id, "name": item.name,
                    "description": item.description, "aliases": list(item.aliases),
                    "applicable": True,
                })

            elif operation == "ADD_FUNCTION":
                item = change.function
                item_id = item.id or f"customer-fn-{uuid4().hex}"
                additions["functions"].append({
                    "id": item_id, "parent_product_line_id": item.parent_product_line_id,
                    "name": item.name, "description": item.description,
                    "aliases": list(item.aliases), "applicable": True,
                })

            elif operation == "ADD_SUB_FUNCTION":
                item = change.sub_function
                item_id = item.id or f"customer-sf-{uuid4().hex}"
                additions["sub_functions"].append({
                    "id": item_id, "parent_function_id": item.parent_function_id,
                    "name": item.name, "description": item.description,
                    "aliases": list(item.aliases), "applicable": True,
                })

        # Every canonical spine element in the selected domain must have an
        # explicit applicability decision before the contextualization can be confirmed.
        missing = []
        for pl in domain.get("spine", []):
            pc = pl_ctx(pl["ProductLine"])
            if pc.get("applicable") is None:
                missing.append(f"PRODUCT_LINE:{pl['ProductLine']}")
            for fn in pl.get("functions", []):
                fc = fn_ctx(pl["ProductLine"], fn["Function"])
                if fc.get("applicable") is None:
                    missing.append(f"FUNCTION:{fn['Function']}")
                for sf in fn.get("subfunctions", []):
                    sc = sf_ctx(pl["ProductLine"], fn["Function"], sf)
                    if sc.get("applicable") is None:
                        missing.append(f"SUB_FUNCTION:{sf}")

        if missing:
            raise KnowledgeContextualizationError(
                "Applicability is required for every canonical spine element "
                f"in domain {domain_id}; missing: {', '.join(missing[:20])}"
            )

        change_summary = {
            "operation": "CONTEXTUALIZATION",
            "domain_id": domain_id,
            "change_count": len(request.changes),
            "operations": [c.operation for c in request.changes],
            "changes": [
                change.model_dump(mode="json")
                for change in request.changes
            ],
        }

        record = self.customer_ontology_repository.create_next_version(
            customer_id=customer_id,
            definition=definition,
            base_customer_ontology_version=request.base_customer_ontology_version,
            request_id=request.request_id,
            change_type="CONTEXTUALIZATION",
            change_reason=request.change_reason,
            change_summary=change_summary,
            created_by=created_by,
            confirmed_by=confirmed_by,
        )

        logger.info(
            "Customer Ontology contextualization completed: customer_id=%s domain_id=%s version=%s changes=%s",
            customer_id, domain_id, record.customer_ontology_version, len(request.changes),
        )
        return record

    def get_contextualization_page(
        self,
        *,
        customer_id: str,
    ) -> dict[str, Any]:
        customer_id = customer_id.strip()
        if not customer_id:
            raise KnowledgeContextualizationError("customer_id must not be blank")

        customer = self.customer_repository.get_by_customer_id(
            customer_id=customer_id
        )
        if customer is None:
            raise KnowledgeContextualizationError(
                f"Customer not found: {customer_id}"
            )

        allocation = self._resolve_canonical_allocation(customer.industry_id)
        ontology_definition = get_ontology_definition(
            ontology_id=allocation.ontology_id,
            version=allocation.ontology_version,
        )
        if ontology_definition is None:
            raise CanonicalOntologyNotFoundError(
                "No registered canonical ontology matches the customer industry: "
                f"{customer.industry_id}"
            )

        ontology_path = get_ontology_path(
            ontology_id=ontology_definition.ontology_id,
            version=ontology_definition.version,
        )
        if ontology_path is None:
            raise CanonicalOntologyNotFoundError(
                "Canonical ontology path could not be resolved for "
                f"{ontology_definition.ontology_id} version "
                f"{ontology_definition.version}"
            )

        definition = self._load_canonical_ontology(ontology_path)
        domains = definition.get("domains", [])
        if not isinstance(domains, list):
            raise CanonicalOntologyArtifactError(
                "Canonical ontology definition must contain a domains list"
            )

        active = self.customer_ontology_repository.get_active(customer_id)
        configured_domain_ids = set()

        if active is not None:
            active_domains = active.definition.get("domains", [])
            if isinstance(active_domains, list):
                for domain in active_domains:
                    if not isinstance(domain, dict):
                        continue
                    context = domain.get("customer_context")
                    if isinstance(context, dict) and context:
                        configured_domain_ids.add(domain.get("name"))

        industry = self.customer_repository.industry_repository.get_industry(
            industry_id=customer.industry_id
        )
        if industry is None:
            raise KnowledgeContextualizationError(
                f"Industry not found: {customer.industry_id}"
            )

        result = {
            "customer_id": customer.customer_id,
            "customer_name": customer.customer_name,
            "industry_id": customer.industry_id,
            "industry_name": industry.industry_name,
            "ontology_id": ontology_definition.ontology_id,
            "ontology_version": ontology_definition.version,
            "customer_ontology_version": (
                active.customer_ontology_version if active is not None else None
            ),
            "domains": [
                {
                    "id": domain["name"],
                    "name": domain.get("display_name", domain["name"]),
                    "description": domain.get("description"),
                    "configured": domain["name"] in configured_domain_ids,
                }
                for domain in domains
                if isinstance(domain, dict) and domain.get("name")
            ],
        }

        logger.info(
            "knowledge_contextualization.contextualization_page.loaded",
            customer_id=customer.customer_id,
            industry_id=customer.industry_id,
            domain_count=len(result["domains"]),
            configured_domain_count=len(configured_domain_ids),
        )

        return result

    def get_contextualization_domain(
        self,
        *,
        customer_id: str,
        domain_id: str,
    ) -> dict[str, Any]:
        """Return persisted customer state and remaining canonical domain delta."""
        customer_id = customer_id.strip()
        domain_id = domain_id.strip()

        if not customer_id:
            raise KnowledgeContextualizationError("customer_id must not be blank")
        if not domain_id:
            raise KnowledgeContextualizationError("domain_id must not be blank")

        customer = self.customer_repository.get_by_customer_id(
            customer_id=customer_id
        )
        if customer is None:
            raise KnowledgeContextualizationError(
                f"Customer not found: {customer_id}"
            )

        active = self.customer_ontology_repository.get_active(customer_id)

        if active is None:
            allocation = self._resolve_canonical_allocation(customer.industry_id)
            ontology_definition = get_ontology_definition(
                ontology_id=allocation.ontology_id,
                version=allocation.ontology_version,
            )
            if ontology_definition is None:
                raise CanonicalOntologyNotFoundError(
                    "No registered canonical ontology matches the customer industry: "
                    f"{customer.industry_id}"
                )

            ontology_path = get_ontology_path(
                ontology_id=ontology_definition.ontology_id,
                version=ontology_definition.version,
            )
            if ontology_path is None:
                raise CanonicalOntologyNotFoundError(
                    "Canonical ontology path could not be resolved for "
                    f"{ontology_definition.ontology_id} version "
                    f"{ontology_definition.version}"
                )

            source_definition = self._load_canonical_ontology(ontology_path)
        else:
            source_definition = active.definition

        domains = source_definition.get("domains")
        if not isinstance(domains, list):
            raise CanonicalOntologyArtifactError(
                "Ontology definition must contain a domains list"
            )

        domain = next(
            (
                item
                for item in domains
                if isinstance(item, dict)
                and item.get("name") == domain_id
            ),
            None,
        )
        if domain is None:
            raise KnowledgeContextualizationError(
                f"Domain not found in ontology: {domain_id}"
            )

        context = domain.get("customer_context")
        if not isinstance(context, dict):
            context = {}

        spine = context.get("spine")
        if not isinstance(spine, dict):
            spine = {}

        product_line_state = spine.get("product_lines")
        if not isinstance(product_line_state, dict):
            product_line_state = {}

        function_state = spine.get("functions")
        if not isinstance(function_state, dict):
            function_state = {}

        sub_function_state = spine.get("sub_functions")
        if not isinstance(sub_function_state, dict):
            sub_function_state = {}

        additions = context.get(
            "additions",
            {"product_lines": [], "functions": [], "sub_functions": []},
        )
        if not isinstance(additions, dict):
            additions = {
                "product_lines": [],
                "functions": [],
                "sub_functions": [],
            }

        def alias_from(state: Any) -> str | None:
            if not isinstance(state, dict):
                return None
            aliases = state.get("aliases")
            if isinstance(aliases, list) and aliases:
                return aliases[0]
            return None

        persisted_product_lines = []
        canonical_delta_product_lines = []

        for pl in domain.get("spine", []):
            if not isinstance(pl, dict):
                continue

            pl_name = pl.get("ProductLine")
            if not pl_name:
                continue

            pl_state = product_line_state.get(pl_name)
            pl_configured = isinstance(pl_state, dict)

            persisted_functions = []
            delta_functions = []

            fn_states = function_state.get(pl_name, {})
            if not isinstance(fn_states, dict):
                fn_states = {}

            for fn in pl.get("functions", []):
                if not isinstance(fn, dict):
                    continue

                fn_name = fn.get("Function")
                if not fn_name:
                    continue

                fn_state = fn_states.get(fn_name)
                fn_configured = isinstance(fn_state, dict)

                persisted_subfunctions = []
                delta_subfunctions = []

                sf_states = sub_function_state.get(pl_name, {})
                if not isinstance(sf_states, dict):
                    sf_states = {}
                sf_states = sf_states.get(fn_name, {})
                if not isinstance(sf_states, dict):
                    sf_states = {}

                for sf in fn.get("subfunctions", []):
                    sf_state = sf_states.get(sf)
                    sf_configured = isinstance(sf_state, dict)

                    item = {
                        "id": canonical_spine_id(
                            domain_id,
                            "SUB_FUNCTION",
                            pl_name,
                            fn_name,
                            sf,
                        ),
                        "name": sf,
                        "applicable": (
                            sf_state.get("applicable")
                            if sf_configured
                            else False
                        ),
                        "alias": (
                            alias_from(sf_state)
                            if sf_configured
                            else None
                        ),
                        "configured": sf_configured,
                        "locked": sf_configured,
                    }

                    if sf_configured:
                        persisted_subfunctions.append(item)
                    else:
                        delta_subfunctions.append(item)

                function_base = {
                    "id": canonical_spine_id(
                        domain_id,
                        "FUNCTION",
                        pl_name,
                        fn_name,
                    ),
                    "name": fn_name,
                    "description": fn.get("description"),
                    "applicable": (
                        fn_state.get("applicable")
                        if fn_configured
                        else False
                    ),
                    "alias": (
                        alias_from(fn_state)
                        if fn_configured
                        else None
                    ),
                    "configured": fn_configured,
                    "locked": fn_configured,
                }

                if fn_configured:
                    persisted_functions.append({
                        **function_base,
                        "sub_functions": persisted_subfunctions,
                    })

                    if delta_subfunctions:
                        delta_functions.append({
                            **function_base,
                            "configured": True,
                            "locked": True,
                            "sub_functions": delta_subfunctions,
                        })
                else:
                    delta_functions.append({
                        **function_base,
                        "sub_functions": delta_subfunctions,
                    })

            pl_base = {
                "id": canonical_spine_id(
                    domain_id,
                    "PRODUCT_LINE",
                    pl_name,
                ),
                "name": pl_name,
                "description": pl.get("description"),
                "applicable": (
                    pl_state.get("applicable")
                    if pl_configured
                    else False
                ),
                "alias": (
                    alias_from(pl_state)
                    if pl_configured
                    else None
                ),
                "configured": pl_configured,
                "locked": pl_configured,
            }

            if pl_configured:
                persisted_product_lines.append({
                    **pl_base,
                    "functions": persisted_functions,
                })

                if delta_functions:
                    canonical_delta_product_lines.append({
                        **pl_base,
                        "functions": delta_functions,
                    })
            else:
                canonical_delta_product_lines.append({
                    **pl_base,
                    "functions": delta_functions,
                })

        industry = self.customer_repository.industry_repository.get_industry(
            industry_id=customer.industry_id
        )
        if industry is None:
            raise KnowledgeContextualizationError(
                f"Industry not found: {customer.industry_id}"
            )

        result = {
            "customer_id": customer.customer_id,
            "customer_name": customer.customer_name,
            "industry_id": customer.industry_id,
            "industry_name": industry.industry_name,
            "ontology_id": (
                active.ontology_id if active is not None else ontology_definition.ontology_id
            ),
            "ontology_version": (
                active.ontology_version
                if active is not None
                else ontology_definition.version
            ),
            "customer_ontology_version": (
                active.customer_ontology_version
                if active is not None
                else None
            ),
            "domain": {
                "id": domain_id,
                "name": domain.get("display_name", domain_id),
                "description": domain.get("description"),
                "persisted": {
                    "product_lines": persisted_product_lines,
                    "additions": additions,
                },
                "canonical_delta": {
                    "product_lines": canonical_delta_product_lines,
                },
                "operations": {
                    "can_add_product_line": True,
                    "can_add_function": True,
                    "can_add_sub_function": True,
                },
            },
        }

        logger.info(
            "knowledge_contextualization.contextualization_domain.loaded",
            customer_id=customer_id,
            domain_id=domain_id,
            customer_ontology_version=active.customer_ontology_version if active is not None else None,
            persisted_product_lines=len(persisted_product_lines),
            delta_product_lines=len(canonical_delta_product_lines),
        )

        return result

    def _resolve_canonical_allocation(self, industry_id: str):
        """
        Resolve the single active canonical ontology allocated to the industry.

        Industry-to-ontology allocation is persisted in Fabric Foundry and is
        the source of truth for customer ontology initialization.
        """
        associations = self.industry_ontology_repository.list_for_industry(
            industry_id=industry_id,
            active_only=True,
        )

        if len(associations) != 1:
            raise CanonicalOntologyNotFoundError(
                f"Expected exactly one active ontology allocation for "
                f"industry '{industry_id}', found {len(associations)}"
            )

        return associations[0]

    @staticmethod
    def _load_canonical_ontology(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise CanonicalOntologyArtifactError(
                f"Canonical ontology artifact not found: {path}"
            )

        try:
            with path.open("r", encoding="utf-8") as handle:
                definition = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise CanonicalOntologyArtifactError(
                f"Failed to parse canonical ontology: {path}"
            ) from exc
        except OSError as exc:
            raise CanonicalOntologyArtifactError(
                f"Failed to read canonical ontology: {path}"
            ) from exc

        if not isinstance(definition, dict):
            raise CanonicalOntologyArtifactError(
                f"Canonical ontology must be a YAML mapping: {path}"
            )

        return deepcopy(definition)

    @staticmethod
    def _build_initial_change_summary(
        *,
        definition: dict[str, Any],
        ontology_definition: Any,
    ) -> dict[str, Any]:
        domains = definition.get("domains", [])
        domain_count = len(domains) if isinstance(domains, list) else 0

        product_line_count = 0
        function_count = 0
        subfunction_count = 0

        if isinstance(domains, list):
            for domain in domains:
                if not isinstance(domain, dict):
                    continue

                spine = domain.get("spine", [])
                if not isinstance(spine, list):
                    continue

                for product_line in spine:
                    if not isinstance(product_line, dict):
                        continue

                    if isinstance(product_line.get("ProductLine"), str):
                        product_line_count += 1

                    functions = product_line.get("functions", [])
                    if not isinstance(functions, list):
                        continue

                    function_count += sum(
                        1
                        for function in functions
                        if isinstance(function, dict)
                        and isinstance(function.get("Function"), str)
                    )

                    for function in functions:
                        if not isinstance(function, dict):
                            continue

                        subfunctions = function.get("subfunctions", [])
                        if isinstance(subfunctions, list):
                            subfunction_count += sum(
                                1
                                for value in subfunctions
                                if isinstance(value, str)
                            )

        return {
            "operation": "INITIAL",
            "source": {
                "ontology_id": ontology_definition.ontology_id,
                "ontology_name": ontology_definition.ontology_name,
                "ontology_version": ontology_definition.version,
            },
            "snapshot": {
                "domains": domain_count,
                "product_lines": product_line_count,
                "functions": function_count,
                "subfunctions": subfunction_count,
            },
        }
