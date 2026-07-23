import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parent / "load_data.py"


def _module():
    assert MODULE_PATH.is_file(), "load_data 스크립트가 존재해야 합니다"
    spec = importlib.util.spec_from_file_location("load_data", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_loads_utf8_csv_with_shape_columns_and_sample(tmp_path):
    mod = _module()
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("이름,나이,매출\n홍길동,30,100\n김철수,25,200\n", encoding="utf-8")

    result = mod.load_data(str(csv_path))

    assert result["status"] == "ok"
    assert result["format"] == "csv"
    assert result["encoding"] == "utf-8"
    assert result["shape"] == {"rows": 2, "columns": 3}
    assert {"name": "이름", "dtype": "object"} in result["columns"]
    assert {"name": "나이", "dtype": "int64"} in result["columns"]
    assert result["sample"][0]["이름"] == "홍길동"


def test_falls_back_to_cp949_when_utf8_decoding_fails(tmp_path):
    mod = _module()
    csv_path = tmp_path / "cp949_sales.csv"
    text = "이름,나이\n박영희,40\n"
    csv_path.write_bytes(text.encode("cp949"))

    result = mod.load_data(str(csv_path))

    assert result["status"] == "ok"
    assert result["encoding"] == "cp949"
    assert result["sample"][0]["이름"] == "박영희"


def test_loads_excel_file_with_null_encoding(tmp_path):
    mod = _module()
    excel_path = tmp_path / "sales.xlsx"
    pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}).to_excel(excel_path, index=False)

    result = mod.load_data(str(excel_path))

    assert result["status"] == "ok"
    assert result["format"] == "excel"
    assert result["encoding"] is None
    assert result["shape"] == {"rows": 2, "columns": 2}


def test_loads_parquet_file_with_null_encoding(tmp_path):
    mod = _module()
    parquet_path = tmp_path / "sales.parquet"
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_parquet(parquet_path, index=False)

    result = mod.load_data(str(parquet_path))

    assert result["status"] == "ok"
    assert result["format"] == "parquet"
    assert result["encoding"] is None
    assert result["shape"] == {"rows": 3, "columns": 2}


def test_missing_file_returns_error_with_hint(tmp_path):
    mod = _module()
    missing_path = tmp_path / "missing.csv"

    result = mod.load_data(str(missing_path))

    assert result["status"] == "error"
    assert "찾을 수 없" in result["error"]["reason"]
    assert result["error"]["hint"]


def test_unsupported_extension_returns_error_listing_supported_formats(tmp_path):
    mod = _module()
    txt_path = tmp_path / "notes.txt"
    txt_path.write_text("hello", encoding="utf-8")

    result = mod.load_data(str(txt_path))

    assert result["status"] == "error"
    assert "지원하지 않는" in result["error"]["reason"]
    assert "csv" in result["error"]["hint"]


def test_corrupted_csv_that_fails_all_encodings_returns_error(tmp_path):
    mod = _module()
    csv_path = tmp_path / "broken.csv"
    csv_path.write_bytes(bytes([0xFF, 0xFE, 0xFF, 0xFE]) * 5)

    result = mod.load_data(str(csv_path))

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


def test_empty_csv_with_header_only_returns_zero_rows(tmp_path):
    mod = _module()
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("col_a,col_b\n", encoding="utf-8")

    result = mod.load_data(str(csv_path))

    assert result["status"] == "ok"
    assert result["shape"] == {"rows": 0, "columns": 2}
    assert result["sample"] == []


def test_sample_size_is_limited_to_default(tmp_path):
    mod = _module()
    csv_path = tmp_path / "many_rows.csv"
    rows = "\n".join(f"{i},{i * 2}" for i in range(10))
    csv_path.write_text(f"a,b\n{rows}\n", encoding="utf-8")

    result = mod.load_data(str(csv_path))

    assert result["shape"]["rows"] == 10
    assert len(result["sample"]) == 5


def test_sample_size_argument_overrides_default(tmp_path):
    mod = _module()
    csv_path = tmp_path / "many_rows.csv"
    rows = "\n".join(f"{i},{i * 2}" for i in range(10))
    csv_path.write_text(f"a,b\n{rows}\n", encoding="utf-8")

    result = mod.load_data(str(csv_path), sample_size=3)

    assert len(result["sample"]) == 3


def test_cli_main_prints_json_and_returns_zero_on_success(tmp_path, capsys):
    mod = _module()
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")

    exit_code = mod.main([str(csv_path)])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured["status"] == "ok"


def test_cli_main_returns_nonzero_on_error(tmp_path, capsys):
    mod = _module()
    missing_path = tmp_path / "missing.csv"

    exit_code = mod.main([str(missing_path)])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["status"] == "error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
