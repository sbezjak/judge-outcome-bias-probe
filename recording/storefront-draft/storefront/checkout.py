import itertools

from storefront.cart import Cart
from storefront.invoices import render_invoice

_numbers = itertools.count(1001)


def place_order(cart: Cart, customer: dict) -> dict:
    """Turn a cart into an order with its invoice. Raises on an empty cart or a customer without an email."""
    if not cart.lines:
        raise ValueError("cannot place an empty order")
    if not customer.get("email"):
        raise ValueError("customer email is required for the invoice")
    order = {
        "number": f"INV-{next(_numbers)}",
        "customer": customer,
        "lines": [{"name": line.name, "qty": line.qty, "total": line.total} for line in cart.lines],
        "subtotal": cart.subtotal,
        "discount": cart.discount,
        "tax": cart.tax,
        "total": cart.total,
    }
    order["invoice_html"] = render_invoice(order)
    return order
