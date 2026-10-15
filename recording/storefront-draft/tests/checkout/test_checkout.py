import pytest

from storefront.cart import Cart
from storefront.checkout import place_order

CUSTOMER = {"name": "Ada Byrne", "email": "ada@example.com"}


def test_place_order_builds_the_invoice():
    cart = Cart()
    cart.add("MUG-1", "Mug", "12.50", 2)
    cart.apply_discount("WELCOME10")
    order = place_order(cart, CUSTOMER)
    assert order["number"].startswith("INV-")
    assert "Discount: -2.50" in order["invoice_html"]
    assert "Total: 27.00" in order["invoice_html"]


def test_empty_cart_is_rejected():
    with pytest.raises(ValueError):
        place_order(Cart(), CUSTOMER)


def test_customer_without_email_is_rejected():
    cart = Cart()
    cart.add("MUG-1", "Mug", "12.50")
    with pytest.raises(ValueError):
        place_order(cart, {"name": "Ada Byrne"})
