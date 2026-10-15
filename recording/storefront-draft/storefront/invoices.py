from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"

env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def render_invoice(order: dict) -> str:
    """The invoice HTML sent to the customer and attached to the order record."""
    return env.get_template("invoice.html").render(order=order)
