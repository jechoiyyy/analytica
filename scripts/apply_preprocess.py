#!/usr/bin/env python3
"""
Analytica [3] 누출 점검 & 전처리 적용

read_dataframe()으로 데이터를 로드해 plan(dict)에 따라 결측 대체·이상치 처리·
범주 정리·날짜/비즈니스 파생변수를 실제로 적용한 cleaned.csv와, 동일한 처리를
재현하는 preprocess.py를 생성한다. 원본 파일은 읽기만 하고 절대 수정하지 않는다
(ADR-005). 분리 후 fit이 필요한 처리(스케일링·인코딩)는 future_pipeline_columns로
지정된 컬럼에 대해 값은 그대로 두고, sklearn Pipeline 골격 코드만 생성한다.

plan 스키마는 docs/PLUGIN_DESIGN.md [3] 및 이 스크립트의 _apply_plan()을 참고.

출력 JSON 스키마 (성공):
{
  "status": "ok",
  "cleaned_path": "<out_dir>/data/cleaned.csv",
  "preprocess_script_path": "<out_dir>/scripts/preprocess.py",
  "shape_before": {"rows": int, "columns": int},
  "shape_after": {"rows": int, "columns": int},
  "applied_actions": [str, ...],
  "warnings": [str, ...]
}

출력 JSON 스키마 (실패):
{
  "status": "error",
  "path": "<입력 경로>",
  "error": {"reason": str, "hint": str}
}
"""

import argparse
import inspect
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from load_data import DataLoadError, read_dataframe


def _error(path: str, reason: str, hint: str) -> dict:
    return {"status": "error", "path": path, "error": {"reason": reason, "hint": hint}}


def _apply_plan(df: pd.DataFrame, plan: dict) -> tuple[pd.DataFrame, list[str], list[str]]:
    """plan에 따라 df를 순서대로 변형해 (df, applied_actions, warnings)를 반환한다.
    순서: drop_columns -> missing_value_actions -> outlier_actions ->
    categorical_cleanup -> date_derived_features -> business_derived_features."""
    df = df.copy()
    applied_actions: list[str] = []
    warnings: list[str] = []

    drop_columns = [c for c in (plan.get("drop_columns") or []) if c in df.columns]
    for c in plan.get("drop_columns") or []:
        if c not in df.columns:
            warnings.append(f"drop_columns: 존재하지 않는 컬럼을 건너뜁니다: {c}")
    if drop_columns:
        df = df.drop(columns=drop_columns)
        applied_actions.append("drop_columns: " + ", ".join(drop_columns))

    missing_labels = []
    for action in plan.get("missing_value_actions") or []:
        column = action["column"]
        strategy = action["strategy"]
        if column not in df.columns:
            warnings.append(f"missing_value_actions: 존재하지 않는 컬럼을 건너뜁니다: {column}")
            continue
        if strategy == "median":
            if not pd.api.types.is_numeric_dtype(df[column]):
                raise ValueError(f"median 전략은 숫자형 컬럼에만 적용할 수 있습니다: {column}")
            df[column] = df[column].fillna(df[column].median())
        elif strategy == "mode":
            mode_values = df[column].mode()
            if not mode_values.empty:
                df[column] = df[column].fillna(mode_values.iloc[0])
        elif strategy == "constant":
            if "fill_value" not in action:
                raise ValueError(f"constant 전략은 fill_value가 필요합니다: {column}")
            df[column] = df[column].fillna(action["fill_value"])
        elif strategy == "drop_column":
            df = df.drop(columns=[column])
        elif strategy == "drop_rows":
            df = df[df[column].notna()]
        else:
            raise ValueError(f"알 수 없는 missing_value strategy: {strategy}")
        missing_labels.append(f"{column}({strategy})")
    if missing_labels:
        applied_actions.append("missing_value_actions: " + ", ".join(missing_labels))

    outlier_labels = []
    for action in plan.get("outlier_actions") or []:
        column = action["column"]
        strategy = action["strategy"]
        if column not in df.columns:
            warnings.append(f"outlier_actions: 존재하지 않는 컬럼을 건너뜁니다: {column}")
            continue
        if not pd.api.types.is_numeric_dtype(df[column]):
            raise ValueError(f"outlier 처리는 숫자형 컬럼에만 적용할 수 있습니다: {column}")
        q1 = df[column].quantile(0.25)
        q3 = df[column].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        if strategy == "clip_iqr":
            df[column] = df[column].clip(lower=lower, upper=upper)
        elif strategy == "drop_rows":
            in_range = (df[column] >= lower) & (df[column] <= upper)
            df = df[in_range | df[column].isna()]
        else:
            raise ValueError(f"알 수 없는 outlier strategy: {strategy}")
        outlier_labels.append(f"{column}({strategy})")
    if outlier_labels:
        applied_actions.append("outlier_actions: " + ", ".join(outlier_labels))

    categorical_labels = []
    for action in plan.get("categorical_cleanup") or []:
        column = action["column"]
        mapping = action.get("mapping") or {}
        if column not in df.columns:
            warnings.append(f"categorical_cleanup: 존재하지 않는 컬럼을 건너뜁니다: {column}")
            continue
        df[column] = df[column].replace(mapping)
        categorical_labels.append(column)
    if categorical_labels:
        applied_actions.append("categorical_cleanup: " + ", ".join(categorical_labels))

    date_labels = []
    for action in plan.get("date_derived_features") or []:
        column = action["column"]
        derive = action.get("derive") or []
        if column not in df.columns:
            warnings.append(f"date_derived_features: 존재하지 않는 컬럼을 건너뜁니다: {column}")
            continue
        parsed = pd.to_datetime(df[column], errors="raise")
        new_columns = []
        for item in derive:
            new_name = f"{column}_{item}"
            if item == "month":
                df[new_name] = parsed.dt.month
            elif item == "day_of_week":
                df[new_name] = parsed.dt.day_name()
            elif item == "is_weekend":
                df[new_name] = parsed.dt.dayofweek >= 5
            elif item == "days_elapsed":
                df[new_name] = (parsed - parsed.min()).dt.days
            else:
                raise ValueError(f"알 수 없는 date derive 항목: {item}")
            new_columns.append(new_name)
        if new_columns:
            date_labels.append(f"{column} -> [{', '.join(new_columns)}]")
    if date_labels:
        applied_actions.append("date_derived_features: " + "; ".join(date_labels))

    business_labels = []
    for action in plan.get("business_derived_features") or []:
        name = action["name"]
        op = action["op"]
        if op == "ratio":
            denom = df[action["denominator"]].replace(0, np.nan)
            df[name] = df[action["numerator"]] / denom
        elif op == "difference":
            df[name] = df[action["left"]] - df[action["right"]]
        elif op == "sum":
            df[name] = df[action["left"]] + df[action["right"]]
        else:
            raise ValueError(f"알 수 없는 business_derived_features op: {op}")
        business_labels.append(name)
    if business_labels:
        applied_actions.append("business_derived_features: " + ", ".join(business_labels))

    return df, applied_actions, warnings


def _render_read_snippet(fmt: str, encoding: str | None) -> str:
    if fmt == "csv":
        return f"    df = pd.read_csv(input_path, encoding={encoding!r})"
    if fmt == "excel":
        return "    df = pd.read_excel(input_path)"
    return "    df = pd.read_parquet(input_path)"


def _render_future_pipeline_snippet(future_pipeline_columns: dict | None) -> str:
    if not future_pipeline_columns:
        return ""
    scale_columns = future_pipeline_columns.get("scale") or []
    one_hot_columns = future_pipeline_columns.get("one_hot_encode") or []
    transformer_lines = []
    if scale_columns:
        transformer_lines.append(f'        ("scale", StandardScaler(), {scale_columns!r}),')
    if one_hot_columns:
        transformer_lines.append(
            f'        ("one_hot_encode", OneHotEncoder(handle_unknown="ignore"), {one_hot_columns!r}),'
        )
    transformers_block = "\n".join(transformer_lines)
    return f'''# 아래 Pipeline은 train/test 분리 후 학습 데이터에만 fit()하라 — 지금 이 스크립트에서는 실행하지 않는다.
def build_future_pipeline():
    from sklearn.compose import ColumnTransformer
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    return ColumnTransformer(
        transformers=[
{transformers_block}
        ]
    )


'''


def _render_preprocess_script(plan: dict, fmt: str, encoding: str | None) -> str:
    plan_literal = json.dumps(plan, ensure_ascii=False, indent=2)
    apply_plan_source = inspect.getsource(_apply_plan)
    read_snippet = _render_read_snippet(fmt, encoding)
    future_pipeline_snippet = _render_future_pipeline_snippet(plan.get("future_pipeline_columns"))

    return f'''#!/usr/bin/env python3
"""
Analytica 재현 가능한 전처리 스크립트 (apply_preprocess.py가 자동 생성)

사용법: python preprocess.py <input_path> <output_csv_path>
"""

import sys

import numpy as np
import pandas as pd

PLAN = {plan_literal}


{apply_plan_source}

{future_pipeline_snippet}def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("사용법: python preprocess.py <input_path> <output_csv_path>", file=sys.stderr)
        return 1
    input_path, output_path = argv

{read_snippet}

    cleaned_df, _applied_actions, _warnings = _apply_plan(df, PLAN)
    cleaned_df.to_csv(output_path, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def apply_preprocess(path: str, plan: dict, out_dir: str) -> dict:
    try:
        df, encoding, fmt = read_dataframe(path)
    except DataLoadError as exc:
        return _error(path, exc.reason, exc.hint)

    shape_before = {"rows": int(df.shape[0]), "columns": int(df.shape[1])}

    try:
        cleaned_df, applied_actions, warnings = _apply_plan(df, plan)
    except ValueError as exc:
        return _error(path, str(exc), "plan 설정을 확인하세요.")

    out_path = Path(out_dir)
    data_dir = out_path / "data"
    scripts_dir = out_path / "scripts"
    data_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = data_dir / "cleaned.csv"
    cleaned_df.to_csv(cleaned_path, index=False)

    preprocess_script_path = scripts_dir / "preprocess.py"
    preprocess_script_path.write_text(
        _render_preprocess_script(plan, fmt, encoding), encoding="utf-8"
    )

    return {
        "status": "ok",
        "cleaned_path": str(cleaned_path),
        "preprocess_script_path": str(preprocess_script_path),
        "shape_before": shape_before,
        "shape_after": {"rows": int(cleaned_df.shape[0]), "columns": int(cleaned_df.shape[1])},
        "applied_actions": applied_actions,
        "warnings": warnings,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analytica 누출 점검 & 전처리 적용")
    parser.add_argument("path", help="분석할 데이터 파일 경로 (csv/xlsx/xls/parquet)")
    parser.add_argument("--plan", required=True, help="처리 계획 JSON 파일 경로")
    parser.add_argument("--out", required=True, help="출력 디렉토리")
    args = parser.parse_args(argv)

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    result = apply_preprocess(args.path, plan, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
