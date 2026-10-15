from decimal import Decimal

import pytest

from storefront.cart import Cart


def test_subtotal_tax_and_total():
    cart = Cart()
    cart.add("MUG-1", "Mug", "12.50", 2)
    assert cart.subtotal == Decimal("25.00")
    assert cart.tax == Decimal("5.00")
    assert cart.total == Decimal("30.00")


def test_adding_the_same_sku_merges_lines():
    cart = Cart()
    cart.add("MUG-1", "Mug", "12.50")
    cart.add("MUG-1", "Mug", "12.50", 2)
    assert len(cart.lines) == 1 and cart.lines[0].qty == 3


def test_discount_applies_before_tax():
    cart = Cart()
    cart.add("TEE-2", "T-shirt", "20.00")
    cart.apply_discount("WELCOME10")
    assert cart.discount == Decimal("2.00")
    assert cart.tax == Decimal("3.60")
    assert cart.total == Decimal("21.60")


def test_unknown_discount_code_is_rejected():
    with pytest.raises(ValueError):
        Cart().apply_discount("FREE100")


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        Cart().add("MUG-1", "Mug", "12.50", 0)
