"""
Modus -- Billing actuals import
===============================
Validation and reconciliation maths for importing provider invoice totals
(``billing_actuals``) so the Finance "Reconciliation Status" view can show the
variance between what Modus tracked and what the provider billed.

Modus does NOT pull invoices from providers automatically; an operator imports
them (JSON or CSV) through ``POST /api/v1/finance/reconciliation/import[/csv]``.

Rules
-----
* Money is ``Decimal`` end to end: amounts must be JSON strings (or CSV text),
  non-negative, at most 8 decimal places and 10 integer digits (NUMERIC(18,8)).
  JSON floats are rejected because they cannot represent decimal money exactly.
* Periods are half-open ``[period_start, period_end)`` in UTC. A date-only
  value (``2026-09-01``) means midnight UTC; a naive datetime is taken as UTC.
* Only USD is supported (the column is ``actual_cost_usd``); any other
  ``currency`` is rejected rather than silently mislabelled.
* The natural key of a row is (provider, service, period_start, period_end);
  importing the same key again updates the row instead of duplicating it.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

MAX_ROWS = 5000
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_PERIOD_DAYS = 400

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_MONEY_QUANT = Decimal("0.00000001")

# Invoice provider -> the ``usage_records.provider`` values Modus tracks it under.
PROVIDER_USAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "aws": ("aws", "bedrock"),
    "bedrock": ("bedrock", "aws"),
    "google": ("google", "gcp", "vertex"),
    "gcp": ("gcp", "google", "vertex"),
    "vertex": ("vertex", "gcp", "google"),
}


def usage_providers_for(provider: str) -> tuple[str, ...]:
    """Usage-record provider names that correspond to an invoice provider."""
    return PROVIDER_USAGE_ALIASES.get(provider, (provider,))


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BillingActualRow(BaseModel):
    """One provider invoice total for a period."""

    provider: str
    service: Optional[str] = Field(None, max_length=128)
    period_start: datetime
    period_end: datetime
    actual_cost_usd: Decimal
    currency: str = "USD"

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _PROVIDER_RE.match(v):
            raise ValueError(
                "provider must be 1-64 characters: lowercase letters, digits, '-', '_' or '.' "
                "(for example openai, anthropic, aws, azure, gcp)"
            )
        return v

    @field_validator("service", mode="before")
    @classmethod
    def _service(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any) -> str:
        v = ("USD" if v is None or str(v).strip() == "" else str(v)).strip().upper()
        if v != "USD":
            raise ValueError(
                f"currency {v!r} is not supported: amounts are stored in USD, convert before importing"
            )
        return v

    @field_validator("actual_cost_usd", mode="before")
    @classmethod
    def _amount(cls, v: Any) -> Decimal:
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError('send the amount as a string such as "123.45" (floats are not exact)')
        if isinstance(v, str):
            v = v.strip()
        try:
            amount = Decimal(str(v))
        except (InvalidOperation, ValueError):
            raise ValueError("actual_cost_usd must be a decimal amount such as 123.45")
        if not amount.is_finite():
            raise ValueError("actual_cost_usd must be a finite number")
        if amount < 0:
            raise ValueError("actual_cost_usd must not be negative")
        if amount != amount.quantize(_MONEY_QUANT):
            raise ValueError("actual_cost_usd has more than 8 decimal places")
        if amount >= Decimal("10000000000"):
            raise ValueError("actual_cost_usd exceeds NUMERIC(18,8) (10 integer digits)")
        return amount

    @field_validator("period_start", "period_end", mode="after")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _to_utc(v)

    def validate_period(self) -> None:
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        if self.period_end - self.period_start > timedelta(days=MAX_PERIOD_DAYS):
            raise ValueError(f"period is longer than {MAX_PERIOD_DAYS} days")

    @property
    def natural_key(self) -> tuple[str, str, datetime, datetime]:
        return (self.provider, self.service or "", self.period_start, self.period_end)


class RowError(BaseModel):
    row: int            # 1-based position in the JSON list, or the CSV line number
    field: Optional[str] = None
    message: str


def _errors_from(exc: ValidationError, row: int) -> list[RowError]:
    out: list[RowError] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p != "__root__"]
        msg = err.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        out.append(RowError(row=row, field=".".join(loc) or None, message=msg))
    return out


def validate_rows(raw_rows: Iterable[tuple[int, dict]]) -> tuple[list[BillingActualRow], list[RowError]]:
    """Validate ``(row_number, mapping)`` pairs. All errors are collected."""
    rows: list[BillingActualRow] = []
    errors: list[RowError] = []
    seen: dict[tuple, int] = {}
    for number, data in raw_rows:
        try:
            row = BillingActualRow(**data)
            row.validate_period()
        except ValidationError as exc:
            errors.extend(_errors_from(exc, number))
            continue
        except ValueError as exc:
            errors.append(RowError(row=number, field="period_end", message=str(exc)))
            continue
        key = row.natural_key
        if key in seen:
            errors.append(RowError(
                row=number,
                message=f"duplicate of row {seen[key]} (same provider, service and period)",
            ))
            continue
        seen[key] = number
        rows.append(row)
    return rows, errors


_CSV_REQUIRED = ("provider", "period_start", "period_end", "actual_cost_usd")
_CSV_KNOWN = _CSV_REQUIRED + ("service", "currency")


def parse_csv(content: bytes) -> tuple[list[tuple[int, dict]], list[RowError]]:
    """Parse an uploaded CSV into ``(line_number, mapping)`` pairs.

    The first line must be a header containing provider, period_start,
    period_end and actual_cost_usd (service and currency are optional; extra
    columns are ignored).
    """
    if len(content) > MAX_CSV_BYTES:
        return [], [RowError(row=0, message=f"file is larger than {MAX_CSV_BYTES // 1024} KiB")]
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [RowError(row=0, message="file is not valid UTF-8 text")]

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], [RowError(row=0, message="file is empty")]
    names = [h.strip().lower() for h in header]
    missing = [c for c in _CSV_REQUIRED if c not in names]
    if missing:
        return [], [RowError(row=1, message=f"header is missing column(s): {', '.join(missing)}")]

    rows: list[tuple[int, dict]] = []
    errors: list[RowError] = []
    for record in reader:
        line = reader.line_num
        if not record or all(not c.strip() for c in record):
            continue
        if len(record) > len(names):
            errors.append(RowError(row=line, message="more values than header columns"))
            continue
        data = {n: v for n, v in zip(names, record) if n in _CSV_KNOWN}
        rows.append((line, data))
        if len(rows) > MAX_ROWS:
            return [], [RowError(row=line, message=f"more than {MAX_ROWS} rows; split the file")]
    if not rows and not errors:
        errors.append(RowError(row=0, message="file has a header but no data rows"))
    return rows, errors


def money_str(amount: Decimal) -> str:
    """Fixed-point, 8 decimal places, never scientific notation ("0.00000000", not "0E-8")."""
    return format(Decimal(amount).quantize(_MONEY_QUANT), "f")


def variance(actual: Decimal, inferred: Decimal) -> tuple[Decimal, Optional[float]]:
    """(delta_usd, delta_pct). delta = actual - inferred; pct is of actual, None if actual is 0.

    Positive delta means Modus tracked less than the provider billed.
    """
    delta = (actual - inferred).quantize(_MONEY_QUANT)
    if actual == 0:
        return delta, None
    pct = (delta / actual * 100).quantize(Decimal("0.0001"))
    return delta, float(pct)
