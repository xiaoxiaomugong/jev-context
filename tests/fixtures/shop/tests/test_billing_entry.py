from billing.entry import issue_invoice


def test_issue_invoice_preserves_half_cent_rounding():
    order = {"id": "sample", "items": [{"price": "1.005", "quantity": 1}]}
    assert issue_invoice(order)["amount"] == "1.01"
