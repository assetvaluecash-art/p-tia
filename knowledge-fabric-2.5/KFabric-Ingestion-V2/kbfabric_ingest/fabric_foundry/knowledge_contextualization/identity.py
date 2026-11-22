from __future__ import annotations


def canonical_domain_id(domain_id: str) -> str:
    return f"DOMAIN:{domain_id}"


def canonical_product_line_id(domain_id: str, product_line: str) -> str:
    return f"DOMAIN:{domain_id}|PRODUCT_LINE:{product_line}"


def canonical_function_id(domain_id: str, product_line: str, function: str) -> str:
    return f"DOMAIN:{domain_id}|PRODUCT_LINE:{product_line}|FUNCTION:{function}"


def canonical_sub_function_id(
    domain_id: str,
    product_line: str,
    function: str,
    sub_function: str,
) -> str:
    return (
        f"DOMAIN:{domain_id}|PRODUCT_LINE:{product_line}"
        f"|FUNCTION:{function}|SUB_FUNCTION:{sub_function}"
    )


def canonical_spine_id(
    domain_id: str,
    element_type: str,
    product_line: str,
    function: str | None = None,
    sub_function: str | None = None,
) -> str:
    if element_type == "PRODUCT_LINE":
        return canonical_product_line_id(domain_id, product_line)
    if element_type == "FUNCTION":
        if function is None:
            raise ValueError("function is required for FUNCTION canonical ID")
        return canonical_function_id(domain_id, product_line, function)
    if element_type == "SUB_FUNCTION":
        if function is None or sub_function is None:
            raise ValueError("function and sub_function are required for SUB_FUNCTION canonical ID")
        return canonical_sub_function_id(domain_id, product_line, function, sub_function)
    if element_type == "DOMAIN":
        return canonical_domain_id(domain_id)
    raise ValueError(f"Unsupported spine element type: {element_type}")
