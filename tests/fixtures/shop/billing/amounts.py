from decimal import Decimal, ROUND_HALF_UP


def calculate_total(items):
    """Round once after adding decimal line amounts."""
    subtotal = sum((Decimal(str(item["price"])) * item["quantity"] for item in items), Decimal("0"))
    return subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
