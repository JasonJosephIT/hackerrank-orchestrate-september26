"""Output formatting shared by the pipeline and the verifier."""
from __future__ import annotations

TWO_DECIMAL_CURRENCIES = {"EUR", "USD"}


def fmt_amount(x: float, currency: str) -> str:
    x = round(float(x) + 0.0, 2)
    if currency in TWO_DECIMAL_CURRENCIES:
        return f"{x:.2f}"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def fmt_plain(x: float) -> str:
    s = f"{round(float(x), 2):.2f}".rstrip("0").rstrip(".")
    return s if s else "0"
