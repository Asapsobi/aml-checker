"""Values an operator types, read the same way by every command and file (check, batch, watch)."""

from decimal import Decimal, InvalidOperation

MAX_CLIENT = 200  # characters in a client name


def amount_hint(text: str) -> str:
    """A planned amount as it is stored: a positive decimal, with thousands separators allowed."""
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a number") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("must be a positive number")
    return format(value, "f")


def client_name(text: str | None) -> str | None:
    """A client name as typed, without surrounding spaces, or None when empty (Q13)."""
    name = (text or "").strip()
    if len(name) > MAX_CLIENT:
        raise ValueError(f"is longer than {MAX_CLIENT} characters")
    return name or None


def safe_cell(value: str) -> str:
    """A CSV cell a spreadsheet will not run as a formula (OWASP "CSV injection")."""
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
