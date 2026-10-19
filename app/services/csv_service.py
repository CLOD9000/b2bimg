"""Parsing e validazione di file CSV/XLSX prodotti.

- Nessuna riga di intestazione: il codice prodotto è sempre nella **prima colonna**.
- CSV: decodifica UTF-8 (con/senza BOM) o Windows-1252, rilevazione separatore.
- XLSX: lettura del primo foglio, tutte le celle convertite in testo.
- Header sintetizzati: ``codice, colonna_2, colonna_3, ...``
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from openpyxl import load_workbook
except Exception:  # pragma: no cover
    load_workbook = None  # type: ignore[assignment]

MAX_COLUMNS = 80

XLSX_EXTS = (".xlsx", ".xlsm", ".xltx", ".xltm")


class CSVValidationError(ValueError):
    """Errore di validazione del file caricato (mostrato all'utente)."""


@dataclass
class ParsedCSV:
    """Risultato del parsing: header sintetizzati + righe come dict."""

    headers: list[str]
    rows: list[dict[str, str]]
    encoding: str = "utf-8"
    delimiter: str = ","
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def preview(self, limit: int = 5) -> list[dict[str, str]]:
        return self.rows[:limit]


def decode_bytes(raw: bytes) -> tuple[str, str]:
    if not raw.strip():
        raise CSVValidationError("Il file è vuoto.")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError as exc:
        raise CSVValidationError(
            "Encoding non riconosciuto: usare UTF-8 o Windows-1252."
        ) from exc


def detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        pass
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {sep: first_line.count(sep) for sep in (",", ";", "\t", "|")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ","


def _synthesize_headers(n_cols: int) -> list[str]:
    headers: list[str] = []
    for i in range(n_cols):
        if i == 0:
            headers.append("codice")
        else:
            headers.append(f"colonna_{i + 1}")
    return headers


def _row_to_dict(headers: list[str], values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, h in enumerate(headers):
        out[h] = values[i] if i < len(values) else ""
    return out


def parse_csv(raw: bytes) -> ParsedCSV:
    text, encoding = decode_bytes(raw)
    delimiter = detect_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows_values: list[list[str]] = []
    for values in reader:
        if not any(str(v).strip() for v in values):
            continue
        rows_values.append([str(v).strip() for v in values])
    if not rows_values:
        raise CSVValidationError("Il file non contiene righe di dati.")
    n_cols = max(len(v) for v in rows_values) if rows_values else 0
    if n_cols == 0 or n_cols > MAX_COLUMNS:
        if n_cols > MAX_COLUMNS:
            raise CSVValidationError(f"Troppe colonne ({n_cols}): massimo {MAX_COLUMNS}.")
        raise CSVValidationError("Il file non contiene righe di dati.")
    headers = _synthesize_headers(n_cols)
    rows: list[dict[str, str]] = []
    for v in rows_values:
        padded = list(v) + [""] * (n_cols - len(v))
        rows.append(_row_to_dict(headers, padded))
    if len(rows) > 200_000:
        raise CSVValidationError("Troppe righe: massimo 200.000 righe per file.")
    return ParsedCSV(headers=headers, rows=rows, encoding=encoding, delimiter=delimiter)


def _iter_xlsx(raw: bytes) -> Iterator[list[str]]:
    if load_workbook is None:
        raise CSVValidationError(
            "openpyxl non disponibile. Installa le dipendenze (pip install -r requirements.txt)."
        )
    bio = io.BytesIO(raw)
    wb = load_workbook(bio, data_only=True, read_only=True)
    sheet = wb.active
    for row in sheet.iter_rows(values_only=True):
        values: list[str] = []
        for v in row:
            if v is None:
                values.append("")
            else:
                values.append(str(v).strip())
        if all(not x for x in values):
            continue
        if len(values) > MAX_COLUMNS:
            values = values[:MAX_COLUMNS]
        yield values
    wb.close()


def parse_xlsx(raw: bytes) -> ParsedCSV:
    rows_values: list[list[str]] = []
    for v in _iter_xlsx(raw):
        rows_values.append(v)
    if not rows_values:
        raise CSVValidationError("Il file XLSX non contiene righe di dati.")
    n_cols = max(len(v) for v in rows_values)
    if n_cols == 0 or n_cols > MAX_COLUMNS:
        if n_cols > MAX_COLUMNS:
            raise CSVValidationError(f"Troppe colonne ({n_cols}): massimo {MAX_COLUMNS}.")
        raise CSVValidationError("Il file XLSX non contiene righe di dati.")
    headers = _synthesize_headers(n_cols)
    rows: list[dict[str, str]] = []
    for v in rows_values:
        padded = list(v) + [""] * (n_cols - len(v))
        rows.append(_row_to_dict(headers, padded))
    if len(rows) > 200_000:
        raise CSVValidationError("Troppe righe: massimo 200.000 righe per file.")
    return ParsedCSV(headers=headers, rows=rows, encoding="utf-8", delimiter="xlsx")


def parse_file(filename: str | Path, raw: bytes) -> ParsedCSV:
    name = str(filename).lower()
    if name.endswith(XLSX_EXTS) or name.endswith(".xls"):
        return parse_xlsx(raw)
    return parse_csv(raw)
