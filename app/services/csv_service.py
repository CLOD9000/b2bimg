"""Parsing e validazione dei CSV prodotti (UTF-8 / Windows-1252)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

MAX_COLUMNS = 80
MAX_HEADER_LEN = 120


class CSVValidationError(ValueError):
    """Errore di validazione del file caricato (mostrato all'utente)."""


@dataclass
class ParsedCSV:
    """Risultato del parsing: header normalizzati + righe come dict."""

    headers: list[str]
    rows: list[dict[str, str]]
    encoding: str
    delimiter: str
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def preview(self, limit: int = 5) -> list[dict[str, str]]:
        """Prime `limit` righe per l'anteprima prima dell'import."""
        return self.rows[:limit]


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decodifica il file provando UTF-8 (con BOM) e Windows-1252.

    Restituisce `(testo, encoding_usato)`.
    """
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
    """Individua il separatore (`,`, `;`, tab, `|`) con fallback sul più frequente."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        pass
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {sep: first_line.count(sep) for sep in (",", ";", "\t", "|")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ","


def _normalize_headers(raw_headers: list[str]) -> tuple[list[str], list[str]]:
    headers: list[str] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_headers):
        name = (raw or "").strip()[:MAX_HEADER_LEN] or f"colonna_{index + 1}"
        if name in seen:
            seen[name] += 1
            new_name = f"{name}_{seen[name]}"
            warnings.append(f"Header duplicato '{name}' rinominato in '{new_name}'.")
            name = new_name
        else:
            seen[name] = 1
        headers.append(name)
    if not any(h.strip() for h in headers):
        raise CSVValidationError("Header vuoti: il file deve avere una riga di intestazione.")
    return headers, warnings


def parse_csv(raw: bytes) -> ParsedCSV:
    """Valida e converte un file CSV in righe dizionarie."""
    text, encoding = decode_bytes(raw)
    delimiter = detect_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        raw_headers = next(reader)
    except StopIteration as exc:
        raise CSVValidationError("Il file non contiene righe.") from exc
    except csv.Error as exc:
        raise CSVValidationError(f"CSV non valido: {exc}") from exc

    headers, warnings = _normalize_headers(raw_headers)
    if len(headers) > MAX_COLUMNS:
        raise CSVValidationError(f"Troppe colonne ({len(headers)}): massimo {MAX_COLUMNS}.")

    rows: list[dict[str, str]] = []
    for values in reader:
        if not any(str(v).strip() for v in values):
            continue  # salta righe completamente vuote
        row = {
            headers[i]: (values[i].strip() if i < len(values) else "")
            for i in range(len(headers))
        }
        if not any(row.values()):
            continue
        rows.append(row)
        if len(rows) > 200_000:
            raise CSVValidationError("Troppe righe: massimo 200.000 righe per file.")

    if not rows:
        raise CSVValidationError("Il file non contiene righe di dati.")

    # verifica coerenza: almeno una colonna con valori non vuoti
    return ParsedCSV(
        headers=headers, rows=rows, encoding=encoding, delimiter=delimiter, warnings=warnings
    )
