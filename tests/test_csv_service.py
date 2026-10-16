"""Test del parsing CSV: encoding, separatore, validazione header, anteprima."""

from __future__ import annotations

import pytest

from app.services.csv_service import (
    CSVValidationError,
    decode_bytes,
    detect_delimiter,
    parse_csv,
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
    parsed = parse_csv(b"codice,descrizione\nA1,Mouse\nA2,Tastiera\n")
    assert parsed.headers == ["codice", "descrizione"]
    assert parsed.row_count == 2
    assert parsed.rows[0] == {"codice": "A1", "descrizione": "Mouse"}
    assert len(parsed.preview(1)) == 1


def test_parse_semicolon_cp1252() -> None:
    raw = "codice;descrizione\nA1;Caffè espresso".encode("cp1252")
    parsed = parse_csv(raw)
    assert parsed.delimiter == ";"
    assert parsed.rows[0]["descrizione"] == "Caffè espresso"


def test_parse_duplicate_headers_renamed() -> None:
    parsed = parse_csv(b"codice,codice\nA1,B1\n")
    assert parsed.headers == ["codice", "codice_2"]
    assert parsed.warnings


def test_parse_short_row_padded() -> None:
    parsed = parse_csv(b"a,b,c\n1,2\n")
    assert parsed.rows[0] == {"a": "1", "b": "2", "c": ""}


def test_parse_no_data_rows_rejected() -> None:
    with pytest.raises(CSVValidationError):
        parse_csv(b"codice,descrizione\n")


def test_parse_skips_blank_lines() -> None:
    parsed = parse_csv(b"codice\nA1\n\n   \nA2\n")
    assert parsed.row_count == 2
