"""Evidence layer: typed facts from messages (regex catalogue, EN + ID) and the image cache.

Message and image content is untrusted data. Only the narrow facts below are extracted,
each validated (amount numeric, currency known, date ISO). Embedded instructions are ignored.
An optional LLM fallback (Groq, structured output) handles messages no rule matches.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CURRENCIES = ("USD", "EUR", "INR", "ZAR", "IDR")
AMOUNT_RE = re.compile(r"\b(USD|EUR|INR|ZAR|IDR)\s?([0-9][0-9,]*(?:\.[0-9]+)?)")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
IMAGE_FACTS = Path(__file__).resolve().parent.parent / "evidence" / "image_facts.json"


@dataclass
class Fact:
    kind: str
    message_id: str
    amount: float | None = None
    currency: str | None = None
    on: date | None = None
    scope: str | None = None       # next | all
    pct: float | None = None
    source: str = "rule"
    note: str = ""
    extra: dict = field(default_factory=dict)


# (kind, [regex patterns, any match], needs_amount, needs_date, scope)
RULES: list[tuple[str, list[str], bool, bool, str | None]] = [
    ("salary_increase", [r"monthly salary has increased to", r"naik menjadi"], True, True, "all"),
    ("salary_next_plus_arrears", [r"regular salary for the next payroll is", r"gaji rutin anda untuk penggajian berikutnya adalah"], True, False, "next"),
    ("bonus_pending", [r"quarterly bonus is still subject", r"bonus kuartalan anda masih menunggu"], False, False, None),
    ("salary_temporary", [r"temporary monthly pay is", r"gaji bulanan sementara anda adalah"], True, False, "next"),
    ("salary_date_moved", [r"confirmed salary is now expected on", r"kini diperkirakan masuk pada"], False, True, "next"),
    ("salary_reduced_next", [r"next salary is reduced to"], True, False, "next"),
    ("first_salary", [r"first salary", r"gaji pertama"], True, True, "all"),
    ("employment_ended", [r"employment has ended", r"hubungan kerja anda telah berakhir",
                          r"seasonal contract has ended", r"kontrak musiman saat ini telah berakhir"], False, False, None),
    ("household_income_ended", [r"remaining confirmed monthly salary is", r"sisa gaji bulanan yang dikonfirmasi adalah"], True, False, "all"),
    ("salary_resumes", [r"regular salary of .* resumes on"], True, True, "all"),
    ("base_salary_commission_pending", [r"confirmed base salary is", r"gaji pokok yang dikonfirmasi adalah"], True, False, "all"),
    ("foreign_salary_confirmed", [r"your salary of [A-Z]{3} [\d.,]+ is confirmed for", r"gaji sebesar [A-Z]{3} [\d.,]+ dikonfirmasi untuk"], True, True, "all"),
    ("reimbursement_not_salary", [r"reimbursement for your earlier work expense", r"penggantian atas biaya kerja"], False, False, None),
    ("regular_salary_confirmed", [r"gaji rutin untuk penggajian berikutnya sudah dikonfirmasi"], False, False, None),
    ("payout_pending", [r"payout is still pending", r"pembayaran berikutnya dari .* masih tertunda"], False, False, None),
    ("invoice_approved", [r"approved an invoice payment of", r"klien menyetujui pembayaran faktur sebesar"], True, True, None),
    ("rent_increase", [r"increases monthly rent by", r"menaikkan biaya sewa bulanan sebesar"], False, False, None),
    ("internal_transfer", [r"transfer between your two accounts", r"transfer antara dua rekening"], False, False, None),
    ("failed_debit_retry", [r"previous debit attempt failed"], False, False, None),
    ("charge_disputed", [r"extra card charge is still being investigated", r"tagihan kartu tambahan masih dalam penyelidikan"], False, False, None),
    ("two_card_minimums", [r"minimum payments due on two separate card accounts"], False, False, None),
    ("refund_pending", [r"refund has been initiated", r"refund is still processing", r"pengembalian dana sudah diproses"], False, False, None),
    ("foreign_charge_pending", [r"bill was charged in a foreign currency", r"tagihan dikenakan dalam mata uang asing"], False, False, None),
    ("investment_valuation", [r"displayed market value", r"displayed value of the investment", r"nilai investasi yang ditampilkan"], False, False, None),
    ("investment_sale_settled", [r"proceeds from your investment sale have settled", r"hasil penjualan investasi anda sudah masuk"], False, False, None),
    ("prize_pending", [r"prize claim has been verified and is still", r"klaim hadiah anda sudah diverifikasi"], False, False, None),
    ("prize_received", [r"prize proceeds have reached your account"], False, False, None),
    ("scam_prize", [r"pay the release charge", r"bayar biaya pencairan"], False, False, None),
    ("receipt_confirms_amount", [r"receipt has the final", r"receipt contains the final"], False, False, None),
]


def _parse_amount(text: str) -> tuple[float, str] | None:
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    return float(m.group(2).replace(",", "")), m.group(1)


def _parse_date(text: str) -> date | None:
    m = DATE_RE.search(text)
    return date.fromisoformat(m.group(1)) if m else None


def extract_message_facts(message_id: str, text: str, llm_fallback=None) -> list[Fact]:
    low = text.lower()
    facts: list[Fact] = []
    for kind, pats, need_amt, need_date, scope in RULES:
        if not any(re.search(p, low) for p in pats):
            continue
        f = Fact(kind=kind, message_id=message_id, scope=scope)
        amt = _parse_amount(text)
        if amt:
            f.amount, f.currency = amt
        f.on = _parse_date(text)
        if kind == "rent_increase":
            m = PCT_RE.search(text)
            f.pct = float(m.group(1)) if m else None
            if f.pct is None:
                continue
        if kind == "salary_next_plus_arrears":
            all_amts = AMOUNT_RE.findall(text)
            if len(all_amts) >= 2:
                f.extra["arrears"] = float(all_amts[1][1].replace(",", ""))
        if kind == "foreign_salary_confirmed" and "salary credit for" in low:
            pass
        if need_amt and f.amount is None:
            continue
        if need_date and f.on is None:
            continue
        facts.append(f)
        break
    # MoneyHub style: "employer has confirmed a USD 1296 salary credit for 15 September 2026"
    m = re.search(r"confirmed a ([A-Z]{3}) ([\d.,]+) salary credit for (\d{1,2} \w+ \d{4})", text)
    if m:
        try:
            import datetime as _dt
            on = _dt.datetime.strptime(m.group(3), "%d %B %Y").date()
            facts.append(Fact(kind="foreign_salary_confirmed", message_id=message_id, amount=float(m.group(2).replace(",", "")),
                              currency=m.group(1), on=on, scope="all"))
        except ValueError:
            pass
    if not facts and llm_fallback is not None:
        facts.extend(llm_fallback(message_id, text))
    return facts


def load_image_facts(path: Path = IMAGE_FACTS) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def image_amounts(path: Path = IMAGE_FACTS) -> dict[str, float]:
    """event_id -> amount from the cache."""
    return {v["event_id"]: float(v["amount"]) for v in load_image_facts(path).values()}
