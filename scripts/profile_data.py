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
  ],
  "correlation": {
    "high_correlation_pairs": [
      {"column_a": str, "column_b": str, "correlation": float}
    ]
  },
  "target_relationship": {
    "target": str,
    "numeric_correlations": [{"name": str, "correlation": float}],
    "categorical_group_differences": [
      {"name": str, "group_means": [
        {"category": str, "mean_target": float, "count": int}
      ]}
    ],
    "class_balance": {"classes": [{"value": Any, "count": int, "ratio": float}]} | null
  } | null,
  "time_pattern": {
    "column": str,
    "monthly": [{"month": str, "count": int, "mean_target": float}],
    "day_of_week": [{"day": str, "count": int, "mean_target": float}]
  } | null
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


def _build_correlation(df: pd.DataFrame) -> dict:
    numeric_columns = list(df.select_dtypes(include=[np.number]).columns)
    pairs = []
    if len(numeric_columns) >= 2:
        corr_matrix = df[numeric_columns].corr()
        for i, column_a in enumerate(numeric_columns):
            for column_b in numeric_columns[i + 1 :]:
                value = corr_matrix.loc[column_a, column_b]
                if pd.isna(value) or abs(value) <= 0.9:
                    continue
                pairs.append(
                    {
                        "column_a": str(column_a),
                        "column_b": str(column_b),
                        "correlation": float(value),
                    }
                )
    pairs.sort(key=lambda pair: abs(pair["correlation"]), reverse=True)
    return {"high_correlation_pairs": pairs[:20]}


def _build_numeric_correlations(df: pd.DataFrame, target: str, target_series: pd.Series) -> list:
    entries = []
    numeric_columns = df.select_dtypes(include=[np.number]).columns
    for name in numeric_columns:
        if name == target:
            continue
        value = df[name].corr(target_series)
        if pd.isna(value):
            continue
        entries.append({"name": str(name), "correlation": float(value)})
    entries.sort(key=lambda item: abs(item["correlation"]), reverse=True)
    return entries


def _build_categorical_group_differences(
    df: pd.DataFrame, target: str, target_series: pd.Series, target_is_numeric: bool
) -> list:
    entries = []
    categorical_columns = df.select_dtypes(include=["object", "category"]).columns
    for name in categorical_columns:
        if name == target:
            continue
        subset = df[[name, target]].dropna()
        if subset.empty:
            continue
        group_means = []
        for category, group_df in subset.groupby(name):
            count = int(len(group_df))
            if target_is_numeric:
                mean_target = float(group_df[target].mean())
            else:
                top_value = group_df[target].mode().iloc[0]
                mean_target = float((group_df[target] == top_value).mean())
            group_means.append(
                {"category": str(category), "mean_target": mean_target, "count": count}
            )
        entries.append({"name": str(name), "group_means": group_means})
    return entries


def _build_class_balance(target_series: pd.Series) -> dict | None:
    if target_series.nunique(dropna=True) > 10:
        return None
    total = int(target_series.notna().sum())
    classes = []
    for value, count in target_series.value_counts(dropna=True).items():
        value_out = value.item() if hasattr(value, "item") else value
        classes.append(
            {
                "value": value_out,
                "count": int(count),
                "ratio": (int(count) / total) if total else 0.0,
            }
        )
    return {"classes": classes}


def _build_target_relationship(df: pd.DataFrame, target: str) -> dict:
    target_series = df[target]
    target_is_numeric = pd.api.types.is_numeric_dtype(target_series)

    numeric_correlations = (
        _build_numeric_correlations(df, target, target_series) if target_is_numeric else []
    )
    categorical_group_differences = _build_categorical_group_differences(
        df, target, target_series, target_is_numeric
    )

    return {
        "target": target,
        "numeric_correlations": numeric_correlations,
        "categorical_group_differences": categorical_group_differences,
        "class_balance": _build_class_balance(target_series),
    }


def _build_time_pattern(
    df: pd.DataFrame, time_column: str, target: str | None, parsed_time: pd.Series
) -> dict:
    target_is_numeric = target is not None and pd.api.types.is_numeric_dtype(df[target])
    working = df.copy()
    working["_analytica_parsed_time"] = parsed_time

    monthly = []
    month_key = working["_analytica_parsed_time"].dt.to_period("M").astype(str)
    for month, group_df in working.groupby(month_key):
        entry = {"month": month, "count": int(len(group_df))}
        if target_is_numeric:
            entry["mean_target"] = float(group_df[target].mean())
        monthly.append(entry)
    monthly.sort(key=lambda entry: entry["month"])

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_key = working["_analytica_parsed_time"].dt.day_name()
    day_of_week = []
    for day in day_names:
        group_df = working[day_key == day]
        if group_df.empty:
            continue
        entry = {"day": day, "count": int(len(group_df))}
        if target_is_numeric:
            entry["mean_target"] = float(group_df[target].mean())
        day_of_week.append(entry)

    return {"column": time_column, "monthly": monthly, "day_of_week": day_of_week}


def profile_data(
    path: str,
    sample_threshold: int = DEFAULT_SAMPLE_THRESHOLD,
    key_columns: list[str] | None = None,
    target: str | None = None,
    time_column: str | None = None,
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

    if target is not None and target not in df.columns:
        return _error(
            path,
            f"target 컬럼이 데이터에 존재하지 않습니다: {target}",
            "데이터에 실제로 존재하는 컬럼명을 target으로 지정하세요.",
        )

    parsed_time = None
    if time_column is not None:
        if time_column not in df.columns:
            return _error(
                path,
                f"time_column이 데이터에 존재하지 않습니다: {time_column}",
                "데이터에 실제로 존재하는 컬럼명을 time_column으로 지정하세요.",
            )
        try:
            parsed_time = pd.to_datetime(df[time_column], errors="raise")
        except (ValueError, TypeError):
            return _error(
                path,
                f"{time_column} 컬럼을 날짜로 해석할 수 없습니다.",
                "날짜로 해석할 수 없는 값이 포함되어 있습니다.",
            )

    total_rows = df.shape[0]
    sampled = total_rows > sample_threshold
    analyzed = df.sample(n=sample_threshold, random_state=RANDOM_STATE) if sampled else df
    analyzed_parsed_time = parsed_time.loc[analyzed.index] if parsed_time is not None else None

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
        "correlation": _build_correlation(analyzed),
        "target_relationship": (
            _build_target_relationship(analyzed, target) if target is not None else None
        ),
        "time_pattern": (
            _build_time_pattern(analyzed, time_column, target, analyzed_parsed_time)
            if time_column is not None
            else None
        ),
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
    parser.add_argument("--target", default=None, help="관계 분석에 사용할 타깃 컬럼")
    parser.add_argument("--time-column", default=None, help="시간 패턴 분석에 사용할 날짜 컬럼")
    args = parser.parse_args(argv)

    key_columns = args.key_columns.split(",") if args.key_columns else None
    result = profile_data(
        args.path,
        sample_threshold=args.sample_threshold,
        key_columns=key_columns,
        target=args.target,
        time_column=args.time_column,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
