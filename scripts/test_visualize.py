import importlib.util
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parent / "visualize.py"


def _module():
    assert MODULE_PATH.is_file(), "visualize 스크립트가 존재해야 합니다"
    spec = importlib.util.spec_from_file_location("visualize", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _find(items, kind):
    return next(item for item in items if item["kind"] == kind)


def _write_basic_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "basic.csv"
    rows = []
    for i in range(30):
        age = 20 + (i % 10)
        bmi = 18.0 + (i % 5) * 0.5
        target = i % 2
        date = f"2024-{(i % 12) + 1:02d}-01"
        rows.append(f"{age},{bmi},{target},{date}")
    csv_path.write_text(
        "age,bmi,target,admit_date\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )
    return csv_path


def test_all_columns_none_produces_no_charts(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir))

    assert result["status"] == "ok"
    assert result["charts"] == []


def test_histogram_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), histogram_columns=["age"])

    chart = _find(result["charts"], "histogram")
    assert chart["columns"] == ["age"]
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0


def test_boxplot_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), boxplot_columns=["bmi"])

    chart = _find(result["charts"], "boxplot")
    assert chart["columns"] == ["bmi"]
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0


def test_correlation_heatmap_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(
        str(csv_path), str(out_dir), correlation_columns=["age", "bmi"]
    )

    chart = _find(result["charts"], "correlation_heatmap")
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0
    assert "truncated" not in chart


def test_target_distribution_numeric_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = tmp_path / "numeric_target.csv"
    rows = "\n".join(str(v) for v in range(20))
    csv_path.write_text(f"score\n{rows}\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), target="score")

    chart = _find(result["charts"], "target_distribution")
    assert chart["columns"] == ["score"]
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0


def test_target_distribution_categorical_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), target="target")

    chart = _find(result["charts"], "target_distribution")
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0


def test_time_pattern_generates_real_png_file(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), time_column="admit_date")

    chart = _find(result["charts"], "time_pattern")
    assert chart["columns"] == ["admit_date"]
    file_path = out_dir / chart["file"]
    assert file_path.is_file()
    assert file_path.stat().st_size > 0
    assert "warnings" not in result


def test_time_pattern_unparseable_column_adds_warning_without_full_failure(tmp_path):
    mod = _module()
    csv_path = tmp_path / "badtime.csv"
    csv_path.write_text(
        "value,date\n1,not-a-date\n2,also-bad\n3,still-bad\n", encoding="utf-8"
    )
    out_dir = tmp_path / "out"

    result = mod.generate_charts(
        str(csv_path), str(out_dir), histogram_columns=["value"], time_column="date"
    )

    assert result["status"] == "ok"
    assert "warnings" in result
    assert any("time_column" in w for w in result["warnings"])
    kinds = [c["kind"] for c in result["charts"]]
    assert "time_pattern" not in kinds
    assert "histogram" in kinds


def test_nonexistent_column_is_skipped_with_warning_others_still_generated(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(
        str(csv_path),
        str(out_dir),
        histogram_columns=["age", "not_a_column"],
    )

    assert result["status"] == "ok"
    assert "warnings" in result
    assert any("not_a_column" in w for w in result["warnings"])
    kinds_columns = [(c["kind"], c["columns"]) for c in result["charts"]]
    assert ("histogram", ["age"]) in kinds_columns


def test_correlation_columns_over_twenty_are_truncated(tmp_path):
    mod = _module()
    csv_path = tmp_path / "wide.csv"
    n_cols = 25
    header = ",".join(f"c{i}" for i in range(n_cols))
    rows = []
    for r in range(10):
        rows.append(",".join(str(r + i) for i in range(n_cols)))
    csv_path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    columns = [f"c{i}" for i in range(n_cols)]
    result = mod.generate_charts(str(csv_path), str(out_dir), correlation_columns=columns)

    chart = _find(result["charts"], "correlation_heatmap")
    assert chart["truncated"] is True
    assert len(chart["columns"]) == 20


def test_korean_font_used_field_present(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), histogram_columns=["age"])

    assert "korean_font_used" in result


def test_out_dir_created_if_missing(tmp_path):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "does_not_exist_yet" / "out"

    result = mod.generate_charts(str(csv_path), str(out_dir), histogram_columns=["age"])

    assert result["status"] == "ok"
    assert (out_dir / "figures").is_dir()


def test_missing_file_returns_error(tmp_path):
    mod = _module()
    missing_path = tmp_path / "missing.csv"
    out_dir = tmp_path / "out"

    result = mod.generate_charts(str(missing_path), str(out_dir))

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


def test_cli_main_prints_json_and_returns_zero_on_success(tmp_path, capsys):
    mod = _module()
    csv_path = _write_basic_csv(tmp_path)
    out_dir = tmp_path / "out"

    exit_code = mod.main(
        [str(csv_path), "--out", str(out_dir), "--histogram-columns", "age,bmi"]
    )
    import json

    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured["status"] == "ok"
    kinds = [c["kind"] for c in captured["charts"]]
    assert kinds.count("histogram") == 2


def test_cli_main_returns_nonzero_on_error(tmp_path, capsys):
    mod = _module()
    missing_path = tmp_path / "missing.csv"
    out_dir = tmp_path / "out"

    exit_code = mod.main([str(missing_path), "--out", str(out_dir)])
    import json

    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["status"] == "error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
