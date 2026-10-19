"""Test del parsing CSV: encoding, separatore, validazione header, anteprima."""
from __future__ import annotations

import pytest

from app.services.csv_service import (
    CSVValidationError,
    decode_bytes,
    detect_delimiter,
    parse_csv,
    parse_file,
    parse_xlsx,
)


def test_decode_utf8() -> None:
    text, encoding = decode_bytes("codice,descrizione\nA1,Caffè".encode())
    assert encoding == "utf-8"
    assert "Caffè" in text


def test_decode_utf8_bom() -> None:
    text, encoding = decode_bytes(b"\xef\xbb\xbfcodice,descrizione\nA1,x")
    assert encoding == "utf-8-sig"
    assert text.startswith("codice")


def test_decode_windows_1252() -> None:
    raw = 'codice,descrizione\nA1,Sci "per fondi”'.encode("cp1252")
    text, encoding = decode_bytes(raw)
    assert encoding == "cp1252"
    assert "Sci" in text


def test_decode_empty_file_rejected() -> None:
    with pytest.raises(CSVValidationError):
        decode_bytes(b"   \n  ")


def test_detect_delimiter_semicolon() -> None:
    assert detect_delimiter("codice;nome;prezzo\nA;B;1") == ";"


def test_detect_delimiter_comma() -> None:
    assert detect_delimiter("codice,nome,prezzo\nA,B,1") == ","


def test_parse_basic() -> None:
    parsed = parse_csv(b"A1,Mouse\nA2,Tastiera\n")
    assert parsed.headers == ["codice", "colonna_2"]
    assert parsed.row_count == 2
    assert parsed.rows[0] == {"codice": "A1", "colonna_2": "Mouse"}
    assert len(parsed.preview(1)) == 1


def test_parse_semicolon_cp1252() -> None:
    raw = "A1;Caffè espresso\nA2;X".encode("cp1252")
    parsed = parse_csv(raw)
    assert parsed.delimiter == ";"
    assert parsed.rows[0]["colonna_2"] == "Caffè espresso"


def test_parse_skips_blank_lines() -> None:
    parsed = parse_csv(b"A1\n\n   \nA2\n")
    assert parsed.row_count == 2


def test_parse_no_data_rejected() -> None:
    with pytest.raises(CSVValidationError):
        parse_csv(b"\n\n   \n")


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def test_parse_xlsx_headerless() -> None:
    raw = _xlsx_bytes([["A1", "Mouse"], ["A2", "Tastiera"]])
    parsed = parse_xlsx(raw)
    assert parsed.headers == ["codice", "colonna_2"]
    assert parsed.row_count == 2
    assert parsed.rows[0] == {"codice": "A1", "colonna_2": "Mouse"}


def test_parse_xlsx_numbers_as_text() -> None:
    raw = _xlsx_bytes([["12345", "Mouse", 9.99]])
    parsed = parse_xlsx(raw)
    assert parsed.rows[0]["codice"] == "12345"
    assert parsed.rows[0]["colonna_2"] == "Mouse"


def test_parse_file_dispatches_by_extension() -> None:
    raw = _xlsx_bytes([["A1", "X"]])
    assert parse_file("listino.xlsx", raw).row_count == 1
    assert parse_file("listino.csv", b"A1,X\n").row_count == 1
