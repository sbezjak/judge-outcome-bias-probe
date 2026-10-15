from decimal import Decimal

from storefront.invoices import render_invoice

ORDER = {
    "number": "INV-1",
    "customer": {"name": "Ada Byrne", "email": "ada@example.com"},
    "lines": [{"name": "Mug", "qty": 2, "total": Decimal("25.00")}],
    "subtotal": Decimal("25.00"),
    "discount": Decimal("0.00"),
    "tax": Decimal("5.00"),
    "total": Decimal("30.00"),
}


def test_invoice_shows_number_customer_and_total():
    html = render_invoice(ORDER)
    assert "Invoice INV-1" in html
    assert "Ada Byrne" in html and "ada@example.com" in html
    assert "Total: 30.00" in html


def test_invoice_hides_a_zero_discount():
    assert "Discount" not in render_invoice(ORDER)


def test_invoice_escapes_customer_text():
    order = dict(ORDER, customer={"name": "<script>x</script>", "email": "a@example.com"})
    html = render_invoice(order)
    assert "<script>" not in html and "&lt;script&gt;" in html
