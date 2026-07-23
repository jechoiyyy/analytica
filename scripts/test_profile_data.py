import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parent / "profile_data.py"


def _module():
    assert MODULE_PATH.is_file(), "profile_data 스크립트가 존재해야 합니다"
    spec = importlib.util.spec_from_file_location("profile_data", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _find(items, name):
    return next(item for item in items if item["name"] == name)


def test_data_dictionary_fields_are_accurate(tmp_path):
    mod = _module()
    csv_path = tmp_path / "basic.csv"
    csv_path.write_text(
        "age,city\n34,seoul\n56,busan\n21,\n,seoul\n", encoding="utf-8"
    )

    result = mod.profile_data(str(csv_path))

    assert result["status"] == "ok"
    assert result["shape"] == {"rows": 4, "columns": 2}
    assert result["sampled"] is False
    assert result["rows_analyzed"] == 4

    age_entry = _find(result["data_dictionary"], "age")
    assert age_entry["dtype"] == "float64"
    assert age_entry["non_null_count"] == 3
    assert age_entry["null_count"] == 1
    assert age_entry["null_ratio"] == pytest.approx(0.25)
    assert age_entry["n_unique"] == 3
    assert len(age_entry["sample_values"]) <= 3

    city_entry = _find(result["data_dictionary"], "city")
    assert city_entry["non_null_count"] == 3
    assert city_entry["null_count"] == 1


def test_missing_ratio_calculation(tmp_path):
    mod = _module()
    csv_path = tmp_path / "missing.csv"
    csv_path.write_text("a,b\n1,2\n,4\n5,\n7,8\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path))

    assert result["missing"]["overall_null_ratio"] == pytest.approx(2 / 8)
    assert result["missing"]["rows_with_any_null"] == 2
    a_col = _find(result["missing"]["by_column"], "a")
    assert a_col["null_count"] == 1
    assert a_col["null_ratio"] == pytest.approx(0.25)


def test_detects_full_duplicate_rows(tmp_path):
    mod = _module()
    csv_path = tmp_path / "dupes.csv"
    csv_path.write_text("a,b\n1,2\n1,2\n3,4\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path))

    assert result["duplicates"]["duplicate_row_count"] == 1
    assert result["duplicates"]["duplicate_row_ratio"] == pytest.approx(1 / 3)
    assert result["duplicates"]["duplicate_key_count"] is None
    assert result["duplicates"]["key_columns"] is None


def test_detects_key_column_duplicates(tmp_path):
    mod = _module()
    csv_path = tmp_path / "dupes_key.csv"
    csv_path.write_text(
        "id,value\n1,a\n1,b\n2,c\n", encoding="utf-8"
    )

    result = mod.profile_data(str(csv_path), key_columns=["id"])

    assert result["duplicates"]["key_columns"] == ["id"]
    assert result["duplicates"]["duplicate_key_count"] == 1


def test_missing_key_column_returns_error(tmp_path):
    mod = _module()
    csv_path = tmp_path / "basic.csv"
    csv_path.write_text("id,value\n1,a\n2,b\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path), key_columns=["not_a_column"])

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


def test_iqr_and_zscore_outlier_detection(tmp_path):
    mod = _module()
    csv_path = tmp_path / "outliers.csv"
    normal_values = [10, 11, 12, 9, 10, 11, 12, 9, 10, 11, 12, 9, 10, 11, 12, 9]
    values = normal_values + [500]
    rows = "\n".join(str(v) for v in values)
    csv_path.write_text(f"score\n{rows}\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path))

    score_entry = _find(result["outliers"], "score")
    assert score_entry["iqr_outlier_count"] >= 1
    assert score_entry["zscore_outlier_count"] >= 1
    assert score_entry["iqr_outlier_ratio"] > 0
    assert score_entry["zscore_outlier_ratio"] > 0


def test_categorical_whitespace_case_and_rare_category_detection(tmp_path):
    mod = _module()
    csv_path = tmp_path / "categorical.csv"
    values = ["Male"] * 400 + ["male "] * 400 + ["MALE"] * 150 + ["Other"] * 2
    rows = "\n".join(values)
    csv_path.write_text(f"gender\n{rows}\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path))

    gender_entry = _find(result["categorical_issues"], "gender")
    assert gender_entry["has_leading_trailing_whitespace"] is True
    assert len(gender_entry["case_variant_groups"]) >= 1
    rare_values = [r["value"] for r in gender_entry["rare_categories"]]
    assert "Other" in rare_values


def test_categorical_issue_present_with_empty_case_variant_groups(tmp_path):
    mod = _module()
    csv_path = tmp_path / "clean_categorical.csv"
    values = ["a"] * 30 + ["b"] * 30 + ["c"] * 30
    rows = "\n".join(values)
    csv_path.write_text(f"letter\n{rows}\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path))

    letter_entry = _find(result["categorical_issues"], "letter")
    assert letter_entry["case_variant_groups"] == []


def test_sampling_kicks_in_below_threshold(tmp_path):
    mod = _module()
    csv_path = tmp_path / "many_rows.csv"
    rows = "\n".join(f"{i},{i * 2}" for i in range(200))
    csv_path.write_text(f"a,b\n{rows}\n", encoding="utf-8")

    result = mod.profile_data(str(csv_path), sample_threshold=50)

    assert result["sampled"] is True
    assert result["rows_analyzed"] == 50
    assert result["shape"] == {"rows": 200, "columns": 2}


def test_missing_file_returns_error(tmp_path):
    mod = _module()
    missing_path = tmp_path / "missing.csv"

    result = mod.profile_data(str(missing_path))

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


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


def test_cli_accepts_key_columns_argument(tmp_path, capsys):
    mod = _module()
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text("id,value\n1,a\n1,b\n", encoding="utf-8")

    exit_code = mod.main([str(csv_path), "--key-columns", "id"])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured["duplicates"]["key_columns"] == ["id"]
    assert captured["duplicates"]["duplicate_key_count"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
