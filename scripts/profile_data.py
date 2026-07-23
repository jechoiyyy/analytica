#!/usr/bin/env python3
"""
Analytica [2] 프로파일링 & 품질 진단

read_dataframe()으로 데이터를 로드해 결측·중복·이상치·범주 오류를 진단하고
JSON-호환 dict로 반환한다. 통계 계산만 수행하며 해석·서술은 담당하지 않는다
(ADR-004: 계산은 Python, 해석은 LLM).

출력 JSON 스키마 (성공):
{
  "status": "ok",
  "path": "<입력 경로>",
  "shape": {"rows": int, "columns": int},
  "sampled": bool,
  "rows_analyzed": int,
  "data_dictionary": [
    {"name": str, "dtype": str, "non_null_count": int, "null_count": int,
     "null_ratio": float, "n_unique": int, "sample_values": [...]}
  ],
  "missing": {
    "overall_null_ratio": float,
    "rows_with_any_null": int,
    "by_column": [{"name": str, "null_count": int, "null_ratio": float}]
  },
  "duplicates": {
    "duplicate_row_count": int,
    "duplicate_row_ratio": float,
    "duplicate_key_count": int | null,
    "key_columns": [str] | null
  },
  "outliers": [
    {"name": str, "iqr_outlier_count": int, "iqr_outlier_ratio": float,
     "zscore_outlier_count": int, "zscore_outlier_ratio": float}
  ],
  "categorical_issues": [
    {"name": str, "has_leading_trailing_whitespace": bool,
     "case_variant_groups": [[str, ...], ...],
     "rare_categories": [{"value": str, "count": int}]}
  ]
}

출력 JSON 스키마 (실패):
{
  "status": "error",
  "path": "<입력 경로>",
  "error": {"reason": str, "hint": str}
}
"""

import argparse
import json
import sys

import numpy as np
import pandas as pd

from load_data import DataLoadError, read_dataframe

DEFAULT_SAMPLE_THRESHOLD = 500_000
RANDOM_STATE = 42
RARE_CATEGORY_RATIO = 0.01
RARE_CATEGORY_MAX_COUNT = 5


def _error(path: str, reason: str, hint: str) -> dict:
    return {"status": "error", "path": path, "error": {"reason": reason, "hint": hint}}


def _sample_values(series: pd.Series, limit: int = 3) -> list:
    return series.dropna().head(limit).tolist()


def _build_data_dictionary(df: pd.DataFrame) -> list:
    entries = []
    for name in df.columns:
        column = df[name]
        non_null_count = int(column.notna().sum())
        null_count = int(column.isna().sum())
        total = len(column)
        entries.append(
            {
                "name": str(name),
                "dtype": str(column.dtype),
                "non_null_count": non_null_count,
                "null_count": null_count,
                "null_ratio": (null_count / total) if total else 0.0,
                "n_unique": int(column.nunique(dropna=True)),
                "sample_values": _sample_values(column),
            }
        )
    return entries


def _build_missing(df: pd.DataFrame) -> dict:
    total_cells = df.shape[0] * df.shape[1]
    total_nulls = int(df.isna().sum().sum())
    by_column = []
    for name in df.columns:
        null_count = int(df[name].isna().sum())
        by_column.append(
            {
                "name": str(name),
                "null_count": null_count,
                "null_ratio": (null_count / len(df)) if len(df) else 0.0,
            }
        )
    return {
        "overall_null_ratio": (total_nulls / total_cells) if total_cells else 0.0,
        "rows_with_any_null": int(df.isna().any(axis=1).sum()),
        "by_column": by_column,
    }


def _build_duplicates(df: pd.DataFrame, key_columns: list[str] | None) -> dict:
    duplicate_row_count = int(df.duplicated().sum())
    duplicate_key_count = None
    if key_columns:
        duplicate_key_count = int(df.duplicated(subset=key_columns).sum())
    return {
        "duplicate_row_count": duplicate_row_count,
        "duplicate_row_ratio": (duplicate_row_count / len(df)) if len(df) else 0.0,
        "duplicate_key_count": duplicate_key_count,
        "key_columns": key_columns,
    }


def _build_outliers(df: pd.DataFrame) -> list:
    entries = []
    numeric_columns = df.select_dtypes(include=[np.number]).columns
    for name in numeric_columns:
        column = df[name].dropna()
        total = len(column)
        if total == 0:
            continue

        q1 = column.quantile(0.25)
        q3 = column.quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        iqr_outlier_count = int(((column < lower) | (column > upper)).sum())

        std = column.std()
        if std and not np.isnan(std):
            z_scores = (column - column.mean()) / std
            zscore_outlier_count = int((z_scores.abs() > 3).sum())
        else:
            zscore_outlier_count = 0

        entries.append(
            {
                "name": str(name),
                "iqr_outlier_count": iqr_outlier_count,
                "iqr_outlier_ratio": iqr_outlier_count / total,
                "zscore_outlier_count": zscore_outlier_count,
                "zscore_outlier_ratio": zscore_outlier_count / total,
            }
        )
    return entries


def _case_variant_groups(values: pd.Series) -> list:
    groups: dict[str, set] = {}
    for value in values.dropna().unique():
        if not isinstance(value, str):
            continue
        key = value.lower()
        groups.setdefault(key, set()).add(value)
    return [sorted(variants) for variants in groups.values() if len(variants) > 1]


def _rare_categories(values: pd.Series) -> list:
    total = len(values.dropna())
    if total == 0:
        return []
    counts = values.dropna().value_counts()
    threshold_ratio = total * RARE_CATEGORY_RATIO
    rare = counts[(counts <= RARE_CATEGORY_MAX_COUNT) & (counts < threshold_ratio)]
    return [{"value": str(value), "count": int(count)} for value, count in rare.items()]


def _build_categorical_issues(df: pd.DataFrame) -> list:
    entries = []
    categorical_columns = df.select_dtypes(include=["object", "category"]).columns
    for name in categorical_columns:
        column = df[name]
        string_values = column.dropna().apply(lambda v: isinstance(v, str))
        has_whitespace = bool(
            column.dropna()[string_values]
            .apply(lambda v: v != v.strip())
            .any()
        )
        entries.append(
            {
                "name": str(name),
                "has_leading_trailing_whitespace": has_whitespace,
                "case_variant_groups": _case_variant_groups(column),
                "rare_categories": _rare_categories(column),
            }
        )
    return entries


def profile_data(
    path: str,
    sample_threshold: int = DEFAULT_SAMPLE_THRESHOLD,
    key_columns: list[str] | None = None,
) -> dict:
    try:
        df, _encoding, _fmt = read_dataframe(path)
    except DataLoadError as exc:
        return _error(path, exc.reason, exc.hint)

    if key_columns:
        missing_columns = [col for col in key_columns if col not in df.columns]
        if missing_columns:
            return _error(
                path,
                f"key_columns에 존재하지 않는 컬럼이 있습니다: {', '.join(missing_columns)}",
                "데이터에 실제로 존재하는 컬럼명을 지정하세요.",
            )

    total_rows = df.shape[0]
    sampled = total_rows > sample_threshold
    analyzed = df.sample(n=sample_threshold, random_state=RANDOM_STATE) if sampled else df

    return {
        "status": "ok",
        "path": path,
        "shape": {"rows": int(total_rows), "columns": int(df.shape[1])},
        "sampled": sampled,
        "rows_analyzed": int(len(analyzed)),
        "data_dictionary": _build_data_dictionary(analyzed),
        "missing": _build_missing(analyzed),
        "duplicates": _build_duplicates(analyzed, key_columns),
        "outliers": _build_outliers(analyzed),
        "categorical_issues": _build_categorical_issues(analyzed),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analytica 프로파일링 & 품질 진단")
    parser.add_argument("path", help="분석할 데이터 파일 경로 (csv/xlsx/xls/parquet)")
    parser.add_argument(
        "--sample-threshold",
        type=int,
        default=DEFAULT_SAMPLE_THRESHOLD,
        help="이 행 수를 초과하면 샘플링 기반으로 프로파일링한다",
    )
    parser.add_argument(
        "--key-columns",
        type=str,
        default=None,
        help="중복 판단 기준이 되는 컬럼(쉼표로 구분)",
    )
    args = parser.parse_args(argv)

    key_columns = args.key_columns.split(",") if args.key_columns else None
    result = profile_data(
        args.path, sample_threshold=args.sample_threshold, key_columns=key_columns
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
