from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.20")
DISCOUNTS = {"WELCOME10": Decimal("0.10"), "STAFF25": Decimal("0.25")}


def money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Line:
    sku: str
    name: str
    unit_price: Decimal
    qty: int

    @property
    def total(self) -> Decimal:
        return money(self.unit_price * self.qty)


@dataclass
class Cart:
    lines: list[Line] = field(default_factory=list)
    discount_code: str | None = None

    def add(self, sku: str, name: str, unit_price: str, qty: int = 1) -> None:
        if qty < 1:
            raise ValueError("qty must be at least 1")
        for line in self.lines:
            if line.sku == sku:
                line.qty += qty
                return
        self.lines.append(Line(sku, name, Decimal(unit_price), qty))

    def apply_discount(self, code: str) -> None:
        if code not in DISCOUNTS:
            raise ValueError(f"unknown discount code: {code}")
        self.discount_code = code

    @property
    def subtotal(self) -> Decimal:
        return money(sum((line.total for line in self.lines), Decimal("0")))

    @property
    def discount(self) -> Decimal:
        rate = DISCOUNTS.get(self.discount_code or "", Decimal("0"))
        return money(self.subtotal * rate)

    @property
    def tax(self) -> Decimal:
        return money((self.subtotal - self.discount) * TAX_RATE)

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.discount + self.tax
