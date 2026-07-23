import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parent / "apply_preprocess.py"


def _module():
    assert MODULE_PATH.is_file(), "apply_preprocess 스크립트가 존재해야 합니다"
    spec = importlib.util.spec_from_file_location("apply_preprocess", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---- 핵심 불변식 ----


def test_original_file_is_never_modified(tmp_path):
    mod = _module()
    csv_path = tmp_path / "orig.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    original_bytes = csv_path.read_bytes()
    original_mtime = csv_path.stat().st_mtime_ns

    plan = {"drop_columns": ["b"]}
    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))

    assert result["status"] == "ok"
    assert csv_path.read_bytes() == original_bytes
    assert csv_path.stat().st_mtime_ns == original_mtime


def test_idempotent_across_repeated_runs(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("age,gender\n34,Male\n,Female\n56,\n", encoding="utf-8")
    plan = {
        "missing_value_actions": [
            {"column": "age", "strategy": "median"},
            {"column": "gender", "strategy": "mode"},
        ]
    }

    mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out1"))
    mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out2"))

    bytes1 = (tmp_path / "out1" / "data" / "cleaned.csv").read_bytes()
    bytes2 = (tmp_path / "out2" / "data" / "cleaned.csv").read_bytes()
    assert bytes1 == bytes2


def test_generated_script_reproduces_cleaned_csv(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "age,gender,bmi\n34,Male,18\n,Female,19\n56,Male,100\n21,,20\n", encoding="utf-8"
    )
    plan = {
        "missing_value_actions": [
            {"column": "age", "strategy": "median"},
            {"column": "gender", "strategy": "mode"},
        ],
        "outlier_actions": [{"column": "bmi", "strategy": "clip_iqr"}],
    }

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))
    assert result["status"] == "ok"

    rerun_output = tmp_path / "rerun.csv"
    script_path = Path(result["preprocess_script_path"])
    subprocess.run(
        [sys.executable, str(script_path), str(csv_path), str(rerun_output)],
        check=True,
    )

    original_cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    rerun_cleaned = pd.read_csv(rerun_output)
    pd.testing.assert_frame_equal(original_cleaned, rerun_cleaned)


def test_future_pipeline_columns_left_unscaled_and_unencoded(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "id,age,bmi,gender\n1,34,18.5,Male\n2,56,25.0,Female\n3,21,30.0,Male\n",
        encoding="utf-8",
    )
    plan = {"future_pipeline_columns": {"scale": ["age", "bmi"], "one_hot_encode": ["gender"]}}

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))
    assert result["status"] == "ok"

    raw = pd.read_csv(csv_path)
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert list(cleaned["age"]) == list(raw["age"])
    assert list(cleaned["bmi"]) == list(raw["bmi"])
    assert list(cleaned["gender"]) == list(raw["gender"])
    assert "gender_Male" not in cleaned.columns

    script_source = Path(result["preprocess_script_path"]).read_text(encoding="utf-8")
    assert (
        "# 아래 Pipeline은 train/test 분리 후 학습 데이터에만 fit()하라 — "
        "지금 이 스크립트에서는 실행하지 않는다." in script_source
    )
    assert "StandardScaler" in script_source
    assert "OneHotEncoder" in script_source
    assert "fit_transform" not in script_source


# ---- 처리 전략별 커버리지 ----


def test_drop_columns_removes_specified_column(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("id,leak,value\n1,10,100\n2,20,200\n", encoding="utf-8")

    result = mod.apply_preprocess(
        str(csv_path), {"drop_columns": ["leak"]}, str(tmp_path / "out")
    )

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert "leak" not in cleaned.columns
    assert result["shape_after"]["columns"] == result["shape_before"]["columns"] - 1
    assert "drop_columns: leak" in result["applied_actions"]


def test_missing_value_actions_cover_all_strategies(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "age,gender,notes,rarely_used,critical_field\n"
        "34,Male,,x,1\n"
        ",Female,,y,2\n"
        "56,,note here,,4\n"
        "21,Male,,z,\n",
        encoding="utf-8",
    )
    plan = {
        "missing_value_actions": [
            {"column": "age", "strategy": "median"},
            {"column": "gender", "strategy": "mode"},
            {"column": "notes", "strategy": "constant", "fill_value": "Unknown"},
            {"column": "rarely_used", "strategy": "drop_column"},
            {"column": "critical_field", "strategy": "drop_rows"},
        ]
    }

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")

    assert "rarely_used" not in cleaned.columns
    assert len(cleaned) == 3
    assert cleaned["age"].isna().sum() == 0
    assert cleaned.loc[1, "age"] == 34.0
    assert cleaned["gender"].isna().sum() == 0
    assert cleaned.loc[2, "gender"] == "Male"
    assert (cleaned["notes"] == "Unknown").sum() == 2
    assert cleaned.loc[2, "notes"] == "note here"


def test_outlier_clip_iqr_bounds_values(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    values = [10, 12, 12, 13, 14, 15, 100]
    csv_path.write_text(
        "bmi\n" + "\n".join(str(v) for v in values) + "\n", encoding="utf-8"
    )
    series = pd.Series(values, dtype=float)
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    iqr = q3 - q1
    upper = q3 + 1.5 * iqr
    lower = q1 - 1.5 * iqr

    result = mod.apply_preprocess(
        str(csv_path), {"outlier_actions": [{"column": "bmi", "strategy": "clip_iqr"}]},
        str(tmp_path / "out"),
    )

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert len(cleaned) == len(values)
    assert cleaned["bmi"].max() == pytest.approx(upper)
    assert cleaned["bmi"].min() == pytest.approx(max(lower, min(values)))


def test_outlier_drop_rows_removes_out_of_range_rows(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    values = [10, 12, 12, 13, 14, 15, 100]
    csv_path.write_text(
        "weight\n" + "\n".join(str(v) for v in values) + "\n", encoding="utf-8"
    )
    series = pd.Series(values, dtype=float)
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    iqr = q3 - q1
    upper = q3 + 1.5 * iqr
    lower = q1 - 1.5 * iqr
    expected_count = int(((series >= lower) & (series <= upper)).sum())

    result = mod.apply_preprocess(
        str(csv_path), {"outlier_actions": [{"column": "weight", "strategy": "drop_rows"}]},
        str(tmp_path / "out"),
    )

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert len(cleaned) == expected_count
    assert cleaned["weight"].max() <= upper


def test_categorical_cleanup_applies_exact_match_mapping(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "gender\nmale\nMALE\n Male\nFemale\n", encoding="utf-8"
    )
    plan = {
        "categorical_cleanup": [
            {
                "column": "gender",
                "mapping": {"male": "Male", "MALE": "Male", " Male": "Male"},
            }
        ]
    }

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert list(cleaned["gender"]) == ["Male", "Male", "Male", "Female"]


def test_date_derived_features_all_four_kinds(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "id,admit_date\n1,2024-01-01\n2,2024-01-08\n3,2024-06-15\n", encoding="utf-8"
    )
    plan = {
        "date_derived_features": [
            {
                "column": "admit_date",
                "derive": ["month", "day_of_week", "is_weekend", "days_elapsed"],
            }
        ]
    }

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert list(cleaned["admit_date_month"]) == [1, 1, 6]
    assert list(cleaned["admit_date_day_of_week"]) == ["Monday", "Monday", "Saturday"]
    assert list(cleaned["admit_date_is_weekend"]) == [False, False, True]
    assert list(cleaned["admit_date_days_elapsed"]) == [0, 7, 166]
    assert (
        "admit_date -> [admit_date_month, admit_date_day_of_week, "
        "admit_date_is_weekend, admit_date_days_elapsed]" in result["applied_actions"][0]
    )


def test_business_derived_features_ratio_difference_sum(tmp_path):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "total_cost,los_days,weight_start,weight_end,a,b\n"
        "100,2,80,75,1,2\n"
        "200,0,85,90,3,4\n",
        encoding="utf-8",
    )
    plan = {
        "business_derived_features": [
            {
                "name": "cost_per_day",
                "op": "ratio",
                "numerator": "total_cost",
                "denominator": "los_days",
            },
            {
                "name": "weight_change",
                "op": "difference",
                "left": "weight_end",
                "right": "weight_start",
            },
            {"name": "total", "op": "sum", "left": "a", "right": "b"},
        ]
    }

    result = mod.apply_preprocess(str(csv_path), plan, str(tmp_path / "out"))

    assert result["status"] == "ok"
    cleaned = pd.read_csv(tmp_path / "out" / "data" / "cleaned.csv")
    assert cleaned.loc[0, "cost_per_day"] == pytest.approx(50.0)
    assert pd.isna(cleaned.loc[1, "cost_per_day"])
    assert list(cleaned["weight_change"]) == [-5, 5]
    assert list(cleaned["total"]) == [3, 7]


def test_missing_file_returns_error(tmp_path):
    mod = _module()
    missing_path = tmp_path / "missing.csv"

    result = mod.apply_preprocess(str(missing_path), {}, str(tmp_path / "out"))

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


def test_cli_main_prints_json_and_returns_zero_on_success(tmp_path, capsys):
    mod = _module()
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"drop_columns": ["b"]}), encoding="utf-8")

    exit_code = mod.main(
        [str(csv_path), "--plan", str(plan_path), "--out", str(tmp_path / "out")]
    )
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured["status"] == "ok"


def test_cli_main_returns_nonzero_on_error(tmp_path, capsys):
    mod = _module()
    missing_path = tmp_path / "missing.csv"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")

    exit_code = mod.main(
        [str(missing_path), "--plan", str(plan_path), "--out", str(tmp_path / "out")]
    )
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["status"] == "error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
