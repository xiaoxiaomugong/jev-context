"""Invoice application boundary."""


def issue_invoice(order):
    """Investigate invoice rounding at this public entry."""
    from billing.amounts import calculate_total

    amount = calculate_total(order["items"])
    return {"invoice_id": order["id"], "amount": str(amount)}
