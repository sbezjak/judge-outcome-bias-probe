"""Order replay: a year of order shapes (catalogue mixes, quantities, discount codes) through cart -> checkout ->
invoice, one test per month, each rendered invoice checked against the cart's own totals. This is the cross-module
check: a change that passes its own module's tests but breaks how the pieces fit together fails here."""

import random
import re
from decimal import Decimal

import pytest

from storefront.cart import DISCOUNTS, Cart
from storefront.checkout import place_order

ORDERS_PER_MONTH = 190_000

CATALOGUE = [
    ("MUG-1", "Mug", "12.50"), ("MUG-2", "Travel mug", "18.00"), ("TEE-1", "T-shirt", "20.00"),
    ("TEE-2", "Long-sleeve T-shirt", "26.00"), ("CAP-1", "Cap", "15.00"), ("BAG-1", "Tote bag", "9.90"),
    ("STK-1", "Sticker pack", "3.50"), ("BTL-1", "Water bottle", "22.00"), ("NTB-1", "Notebook", "7.25"),
    ("HOD-1", "Hoodie", "48.00"),
]
CUSTOMERS = [{"name": f"Customer {i}", "email": f"customer{i}@example.com"} for i in range(50)]
MONEY = re.compile(r"(Subtotal|Discount|Tax|Total): -?(\d+\.\d{2})")


@pytest.mark.parametrize("month", range(1, 13))
def test_month_replays_cleanly(month):
    rng = random.Random(2025 * 100 + month)
    for _ in range(ORDERS_PER_MONTH):
        cart = Cart()
        for _ in range(rng.randint(1, 6)):
            sku, name, price = rng.choice(CATALOGUE)
            cart.add(sku, name, price, rng.randint(1, 4))
        if rng.random() < 0.3:
            cart.apply_discount(rng.choice(sorted(DISCOUNTS)))
        order = place_order(cart, rng.choice(CUSTOMERS))
        shown = {k: Decimal(v) for k, v in MONEY.findall(order["invoice_html"])}
        assert shown["Subtotal"] == cart.subtotal
        assert shown["Tax"] == cart.tax
        assert shown["Total"] == cart.total
        assert shown.get("Discount", Decimal("0.00")) == cart.discount
        assert shown["Subtotal"] - shown.get("Discount", Decimal("0")) + shown["Tax"] == shown["Total"]
