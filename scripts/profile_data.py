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
     "null_ratio": float, "n_unique": int, "sample_values": [...],
     # 수치 컬럼만: 분포 통계(전부 결측이면 각 값 null)
     "min": float | null, "max": float | null, "mean": float | null,
     "std": float | null, "skew": float | null}
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
  # HIGH_CARDINALITY_LIMIT 초과로 범주 진단·그룹 차이에서 제외된 식별자성 컬럼
  "high_cardinality_columns": [{"name": str, "n_unique": int}],
  # 누출 후보(결정적 규칙 기반, 확정·기각은 LLM/인터뷰가 담당)
  "leakage_candidates": [
    {"name": str, "reasons": [str, ...], "signal": "strong" | "weak",
     "association": {"metric": str, "value": float, "strength": float} | null}
  ],
  "correlation": {
    "high_correlation_pairs": [
      {"column_a": str, "column_b": str, "correlation": float}
    ]
  },
  # 타깃 타입은 dtype이 아니라 고유값 수로 판별한다. 지표는 타입별로 다르지만
  # strength(0~1)는 항상 비교 가능하고, value는 지표 원값이라 방향을 담는다.
  "target_relationship": {
    "target": str,
    "target_type": "binary" | "multiclass" | "continuous"
                 | "degenerate" | "high_cardinality_label",
    "positive_class": Any | null,
    "positive_class_rule": str | null,
    "baseline": {"stat": "positive_rate" | "mean", "value": float} | null,
    "numeric_associations": [
      {"name": str, "metric": "auc" | "spearman" | "eta_squared",
       "value": float, "strength": float,
       "direction": "positive" | "negative" | null,
       "pearson": float | null,   # continuous 타깃에만
       "n": int}
    ],
    "numeric_columns_skipped": [str],
    "categorical_associations": [
      {"name": str, "metric": "cramers_v" | "eta_squared",
       "value": float, "strength": float}
    ],
    "categorical_group_differences": [
      {"name": str,
       "stat": "positive_rate" | "mean" | "class_distribution",
       "groups": [
         # stat이 class_distribution이면 value 대신 distribution이 온다
         {"category": str, "count": int, "value": float}
         | {"category": str, "count": int, "distribution": {str: float}}
       ]}
    ],
    "class_balance": {
      "classes": [{"value": Any, "count": int, "ratio": float}],
      "imbalance_ratio": float | null
    } | null
  } | null,
  "time_pattern": {
    "column": str,
    "stat": "positive_rate" | "mean" | null,
    "monthly": [{"month": str, "count": int, "value": float}],
    "day_of_week": [{"day": str, "count": int, "value": float}]
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
# 이 고유값 수를 넘는 object/category 컬럼은 식별자로 보고 범주 진단·그룹 차이에서 제외한다.
HIGH_CARDINALITY_LIMIT = 50
# 이 고유값 수 이하의 비연속 타깃을 다중분류로 본다.
DEFAULT_MULTICLASS_LIMIT = 20
# 연관도를 계산하기 위해 필요한 최소 유효 표본(특성·타깃 모두 값이 있는 행).
DEFAULT_MIN_ASSOCIATION_SAMPLE = 30
# 다중분류 그룹 분포에서 보고할 상위 클래스 수.
GROUP_DISTRIBUTION_TOP_N = 5
# 누출 자동 플래깅 임계값: 타깃 연관도 strength가 이 값 이상이거나,
# 고유값 비율이 아래 값 이상이면 후보로 표시.
LEAKAGE_STRENGTH_THRESHOLD = 0.8
NEAR_UNIQUE_RATIO = 0.98
# 예측 시점 이후에 생성될 가능성을 시사하는 컬럼명 힌트(사후 결과 변수).
LEAKAGE_NAME_HINTS = (
    "after",
    "post_",
    "_post",
    "refund",
    "review",
    "outcome",
    "resolved",
    "settled",
    "chargeback",
    "_actual",
)


def _error(path: str, reason: str, hint: str) -> dict:
    return {"status": "error", "path": path, "error": {"reason": reason, "hint": hint}}


def _sample_values(series: pd.Series, limit: int = 3) -> list:
    return series.dropna().head(limit).tolist()


def _num_or_none(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _py(value):
    """numpy 스칼라를 JSON 직렬화 가능한 파이썬 값으로 변환한다."""
    return value.item() if hasattr(value, "item") else value


def _numeric_stats(column: pd.Series) -> dict:
    """수치 컬럼의 분포 통계. 전부 결측이거나 계산 불가면 None을 반환한다."""
    clean = column.dropna()
    if clean.empty:
        return {"min": None, "max": None, "mean": None, "std": None, "skew": None}
    return {
        "min": _num_or_none(clean.min()),
        "max": _num_or_none(clean.max()),
        "mean": _num_or_none(clean.mean()),
        "std": _num_or_none(clean.std()),
        "skew": _num_or_none(clean.skew()),
    }


def _build_data_dictionary(df: pd.DataFrame) -> list:
    entries = []
    for name in df.columns:
        column = df[name]
        non_null_count = int(column.notna().sum())
        null_count = int(column.isna().sum())
        total = len(column)
        entry = {
            "name": str(name),
            "dtype": str(column.dtype),
            "non_null_count": non_null_count,
            "null_count": null_count,
            "null_ratio": (null_count / total) if total else 0.0,
            "n_unique": int(column.nunique(dropna=True)),
            "sample_values": _sample_values(column),
        }
        if pd.api.types.is_numeric_dtype(column):
            entry.update(_numeric_stats(column))
        entries.append(entry)
    return entries


def _high_cardinality_names(df: pd.DataFrame) -> set:
    """식별자로 볼 만한 고카디널리티 object/category 컬럼명 집합."""
    names = set()
    for name in df.select_dtypes(include=["object", "category"]).columns:
        if df[name].nunique(dropna=True) > HIGH_CARDINALITY_LIMIT:
            names.add(name)
    return names


def _build_high_cardinality_columns(df: pd.DataFrame) -> list:
    entries = []
    for name in df.select_dtypes(include=["object", "category"]).columns:
        n_unique = int(df[name].nunique(dropna=True))
        if n_unique > HIGH_CARDINALITY_LIMIT:
            entries.append({"name": str(name), "n_unique": n_unique})
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


def _build_categorical_issues(df: pd.DataFrame, skip_columns: set = frozenset()) -> list:
    entries = []
    categorical_columns = df.select_dtypes(include=["object", "category"]).columns
    for name in categorical_columns:
        if name in skip_columns:
            continue
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


def _infer_target_type(series: pd.Series, multiclass_limit: int) -> str:
    """타깃 타입을 dtype이 아니라 구조(고유값 수)로 판별한다.

    dtype만 보면 "Yes"/"No" 같은 문자열 이진 타깃에서 관계 분석이 통째로 꺼진다.
    반환값: binary | multiclass | continuous | degenerate | high_cardinality_label
    """
    n_unique = series.nunique(dropna=True)
    if n_unique <= 1:
        return "degenerate"
    if n_unique == 2:
        return "binary"
    if pd.api.types.is_numeric_dtype(series):
        # 정수형이고 고유값이 적으면 등급·코드 같은 다중분류로 본다.
        if pd.api.types.is_integer_dtype(series) and n_unique <= multiclass_limit:
            return "multiclass"
        return "continuous"
    return "multiclass" if n_unique <= multiclass_limit else "high_cardinality_label"


def _resolve_positive_class(series: pd.Series, requested) -> tuple:
    """이진 타깃의 양성 클래스를 정하고 (값, 결정 규칙)을 반환한다.

    관심 사건이 무엇인지는 데이터만으로 확정할 수 없으므로 결정 규칙을 함께 남겨
    보고서가 사용자에게 재확인할 수 있게 한다.
    """
    values = list(series.dropna().unique())

    if requested is not None:
        for value in values:
            if str(value) == str(requested):
                return value, "user_specified"
        raise ValueError(
            f"positive_class 값을 타깃에서 찾을 수 없습니다: {requested} "
            f"(사용 가능한 값: {', '.join(str(v) for v in values)})"
        )

    labels = {str(value).strip().lower() for value in values}
    if labels in ({"0", "1"}, {"0.0", "1.0"}):
        return next(v for v in values if str(v).strip().lower() in ("1", "1.0")), "numeric_convention"
    if labels == {"false", "true"}:
        return next(v for v in values if str(v).strip().lower() == "true"), "boolean_convention"

    counts = series.value_counts(dropna=True)
    if counts.iloc[0] == counts.iloc[-1]:
        # 완전 동률이면 문자열 정렬로 결정론을 보장한다.
        return sorted(values, key=str)[-1], "tie_broken_by_sort"
    return counts.index[-1], "minority_class"


def _encode_target(series: pd.Series, target_type: str, positive_class):
    """연관도 계산에 쓸 타깃 시리즈. 이진은 양성=1로 코드화하고 나머지는 원값을 쓴다."""
    if target_type == "binary":
        return (series == positive_class).astype(float).where(series.notna())
    return series


def _eta_squared(values: pd.Series, groups: pd.Series) -> float | None:
    """그룹 간 분산이 전체 분산에서 차지하는 비율(0~1). 방향은 없다."""
    grand_mean = values.mean()
    total = float(((values - grand_mean) ** 2).sum())
    if not total:
        return None
    between = float(
        sum(len(group) * (group.mean() - grand_mean) ** 2 for _, group in values.groupby(groups))
    )
    return between / total


def _cramers_v(a: pd.Series, b: pd.Series) -> float | None:
    """범주형 두 컬럼의 연관도(0~1). 카이제곱 통계량을 표본 수로 정규화한다."""
    table = pd.crosstab(a, b)
    if table.size == 0 or min(table.shape) < 2:
        return None
    n = int(table.values.sum())
    if not n:
        return None
    expected = np.outer(table.sum(axis=1), table.sum(axis=0)) / n
    chi2 = float(
        np.sum(
            np.where(expected > 0, (table.values - expected) ** 2 / np.where(expected > 0, expected, 1), 0.0)
        )
    )
    denominator = n * (min(table.shape) - 1)
    return float(np.sqrt(chi2 / denominator)) if denominator else None


def _numeric_association(
    feature: pd.Series, y: pd.Series, target_type: str, min_sample: int
) -> dict | None:
    """수치 특성과 타깃의 연관도.

    타깃 타입별로 지표가 다르지만 `strength`(0~1, 클수록 강함)는 항상 비교 가능하게
    정규화한다. `value`는 지표 원값이라 방향을 담고, `direction`이 그것을 명시한다.
    AUC 0.2를 "약한 관계"로 오독하지 않게 하는 것이 이 분리의 목적이다.
    """
    pair = pd.concat([feature, y], axis=1).dropna()
    if len(pair) < min_sample:
        return None
    values, target = pair.iloc[:, 0], pair.iloc[:, 1]
    if values.nunique() < 2:
        return None

    if target_type == "continuous":
        # Spearman은 순위에 대한 Pearson이다. 직접 계산해 scipy 의존을 피한다.
        spearman = values.rank().corr(target.rank())
        if pd.isna(spearman):
            return None
        return {
            "metric": "spearman",
            "value": float(spearman),
            "strength": float(abs(spearman)),
            "direction": "positive" if spearman >= 0 else "negative",
            "pearson": _num_or_none(values.corr(target)),
            "n": int(len(pair)),
        }

    if target_type == "binary":
        positive = target == 1
        n_positive = int(positive.sum())
        n_negative = int(len(target) - n_positive)
        if not n_positive or not n_negative:
            return None
        # Mann-Whitney U를 정규화한 AUC. 순위 기반이라 단조 비선형 관계도 잡는다.
        ranks = values.rank()
        auc = (ranks[positive].sum() - n_positive * (n_positive + 1) / 2) / (
            n_positive * n_negative
        )
        return {
            "metric": "auc",
            "value": float(auc),
            "strength": float(abs(auc - 0.5) * 2),
            "direction": "positive" if auc >= 0.5 else "negative",
            "n": int(len(pair)),
        }

    eta_squared = _eta_squared(values, target)
    if eta_squared is None:
        return None
    return {
        "metric": "eta_squared",
        "value": float(eta_squared),
        "strength": float(eta_squared),
        "direction": None,
        "n": int(len(pair)),
    }


def _build_numeric_associations(
    df: pd.DataFrame, target: str, y: pd.Series, target_type: str, min_sample: int
) -> tuple[list, list]:
    entries, skipped = [], []
    for name in df.select_dtypes(include=[np.number]).columns:
        if name == target:
            continue
        association = _numeric_association(df[name], y, target_type, min_sample)
        if association is None:
            skipped.append(str(name))
            continue
        entries.append({"name": str(name), **association})
    entries.sort(key=lambda item: item["strength"], reverse=True)
    return entries, skipped


def _group_stat_name(target_type: str) -> str:
    if target_type == "binary":
        return "positive_rate"
    if target_type == "continuous":
        return "mean"
    return "class_distribution"


def _build_group_stats(feature: pd.Series, y: pd.Series, target_type: str) -> dict | None:
    """범주별 타깃 요약.

    다중분류는 단일 스칼라로 요약하지 않고 클래스 분포를 그대로 전달한다. 그룹마다
    다른 최빈 클래스의 순도를 같은 필드로 내보내면 비교 불가능한 값이 비교 가능한
    것처럼 보이기 때문이다.
    """
    pair = pd.concat([feature, y], axis=1).dropna()
    if pair.empty:
        return None
    categories, target = pair.iloc[:, 0], pair.iloc[:, 1]

    stat = _group_stat_name(target_type)
    groups = []
    for category, group in target.groupby(categories):
        row = {"category": str(category), "count": int(len(group))}
        if stat == "class_distribution":
            distribution = group.value_counts(normalize=True).head(GROUP_DISTRIBUTION_TOP_N)
            row["distribution"] = {str(k): float(v) for k, v in distribution.items()}
        else:
            row["value"] = float(group.mean())
        groups.append(row)
    return {"name": str(feature.name), "stat": stat, "groups": groups}


def _build_categorical_blocks(
    df: pd.DataFrame,
    target: str,
    target_series: pd.Series,
    y: pd.Series,
    target_type: str,
    skip_columns: set,
) -> tuple[list, list]:
    """(범주형 연관도, 범주별 그룹 차이)를 함께 만든다."""
    associations, group_differences = [], []
    for name in df.select_dtypes(include=["object", "category"]).columns:
        if name == target or name in skip_columns:
            continue

        if target_type in ("binary", "multiclass"):
            value = _cramers_v(df[name], target_series)
            metric = "cramers_v"
        else:
            value = _eta_squared(y.dropna(), df[name])
            metric = "eta_squared"
        if value is not None:
            associations.append(
                {"name": str(name), "metric": metric, "value": value, "strength": abs(value)}
            )

        stats = _build_group_stats(df[name], y, target_type)
        if stats is not None:
            group_differences.append(stats)

    associations.sort(key=lambda item: item["strength"], reverse=True)
    return associations, group_differences


def _build_class_balance(target_series: pd.Series, target_type: str) -> dict | None:
    if target_type not in ("binary", "multiclass"):
        return None
    total = int(target_series.notna().sum())
    if not total:
        return None
    counts = target_series.value_counts(dropna=True)
    classes = [
        {"value": _py(value), "count": int(count), "ratio": int(count) / total}
        for value, count in counts.items()
    ]
    minority = int(counts.iloc[-1])
    return {
        "classes": classes,
        "imbalance_ratio": (float(counts.iloc[0]) / minority) if minority else None,
    }


def _build_target_relationship(
    df: pd.DataFrame,
    target: str,
    target_type: str,
    y: pd.Series,
    positive_class,
    positive_class_rule: str | None,
    skip_columns: set = frozenset(),
    min_sample: int = DEFAULT_MIN_ASSOCIATION_SAMPLE,
) -> dict:
    target_series = df[target]
    analysable = target_type in ("binary", "multiclass", "continuous")

    numeric_associations, numeric_skipped = (
        _build_numeric_associations(df, target, y, target_type, min_sample)
        if analysable
        else ([], [])
    )
    categorical_associations, group_differences = (
        _build_categorical_blocks(df, target, target_series, y, target_type, skip_columns)
        if analysable
        else ([], [])
    )

    baseline = None
    if target_type in ("binary", "continuous"):
        baseline = {"stat": _group_stat_name(target_type), "value": _num_or_none(y.mean())}

    return {
        "target": target,
        "target_type": target_type,
        "positive_class": _py(positive_class),
        "positive_class_rule": positive_class_rule,
        "baseline": baseline,
        "numeric_associations": numeric_associations,
        "numeric_columns_skipped": numeric_skipped,
        "categorical_associations": categorical_associations,
        "categorical_group_differences": group_differences,
        "class_balance": _build_class_balance(target_series, target_type),
    }


def _build_leakage_candidates(
    df: pd.DataFrame, target: str | None, numeric_associations: list | None = None
) -> list:
    """누출 후보를 결정적 규칙으로 플래깅한다(확정·기각은 LLM/인터뷰가 담당).

    - near_unique_identifier: 비-실수형 컬럼의 고유값 비율이 NEAR_UNIQUE_RATIO 이상 (ID 암기 위험)
    - post_outcome_name_hint: 컬럼명이 사후 결과를 시사 (LEAKAGE_NAME_HINTS)
    - near_perfect_target_separation: 타깃 연관도 strength가 LEAKAGE_STRENGTH_THRESHOLD 이상

    `signal`은 strong|weak이다. 이름 매칭은 도메인·언어에 따라 오탐이 잦으므로 weak으로
    두고, 구조에서 나온 신호만 strong으로 표시한다.
    """
    n_rows = len(df)
    candidates: dict[str, dict] = {}

    def _add(name, reason, signal, association=None):
        entry = candidates.setdefault(
            str(name),
            {"name": str(name), "reasons": [], "signal": "weak", "association": None},
        )
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        if signal == "strong":
            entry["signal"] = "strong"
        if association is not None:
            entry["association"] = association

    for name in df.columns:
        if target is not None and name == target:
            continue
        column = df[name]
        if (
            n_rows
            and not pd.api.types.is_float_dtype(column)
            and column.nunique(dropna=True) / n_rows >= NEAR_UNIQUE_RATIO
        ):
            _add(name, "near_unique_identifier", "strong")
        lowered = str(name).lower()
        if any(hint in lowered for hint in LEAKAGE_NAME_HINTS):
            _add(name, "post_outcome_name_hint", "weak")

    for item in numeric_associations or []:
        if item["strength"] >= LEAKAGE_STRENGTH_THRESHOLD:
            _add(
                item["name"],
                "near_perfect_target_separation",
                "strong",
                {
                    "metric": item["metric"],
                    "value": item["value"],
                    "strength": item["strength"],
                },
            )

    return list(candidates.values())


def _build_time_pattern(
    df: pd.DataFrame,
    time_column: str,
    y: pd.Series | None,
    target_type: str | None,
    parsed_time: pd.Series,
) -> dict:
    # 그룹 차이와 같은 stat/value 형태를 쓴다. 다중분류는 단일 스칼라로 요약하지 않는다.
    stat = _group_stat_name(target_type) if target_type in ("binary", "continuous") else None

    def _summarise(index) -> dict:
        entry = {"count": int(len(index))}
        if stat is not None:
            entry["value"] = _num_or_none(y.loc[index].mean())
        return entry

    month_key = parsed_time.dt.to_period("M").astype(str)
    monthly = [
        {"month": month, **_summarise(group.index)} for month, group in df.groupby(month_key)
    ]
    monthly.sort(key=lambda entry: entry["month"])

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_key = parsed_time.dt.day_name()
    day_of_week = []
    for day in day_names:
        group = df[day_key == day]
        if group.empty:
            continue
        day_of_week.append({"day": day, **_summarise(group.index)})

    return {"column": time_column, "stat": stat, "monthly": monthly, "day_of_week": day_of_week}


def profile_data(
    path: str,
    sample_threshold: int = DEFAULT_SAMPLE_THRESHOLD,
    key_columns: list[str] | None = None,
    target: str | None = None,
    time_column: str | None = None,
    positive_class: str | None = None,
    multiclass_limit: int = DEFAULT_MULTICLASS_LIMIT,
    min_association_sample: int = DEFAULT_MIN_ASSOCIATION_SAMPLE,
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

    high_card_names = _high_cardinality_names(analyzed)

    target_type = resolved_positive = positive_class_rule = None
    y = None
    if target is not None:
        target_type = _infer_target_type(analyzed[target], multiclass_limit)
        if target_type == "binary":
            try:
                resolved_positive, positive_class_rule = _resolve_positive_class(
                    analyzed[target], positive_class
                )
            except ValueError as exc:
                return _error(
                    path,
                    str(exc),
                    "타깃에 실제로 존재하는 값을 --positive-class로 지정하거나 옵션을 생략하세요.",
                )
        y = _encode_target(analyzed[target], target_type, resolved_positive)

    target_relationship = (
        _build_target_relationship(
            analyzed,
            target,
            target_type,
            y,
            resolved_positive,
            positive_class_rule,
            high_card_names,
            min_association_sample,
        )
        if target is not None
        else None
    )

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
        "categorical_issues": _build_categorical_issues(analyzed, high_card_names),
        "high_cardinality_columns": _build_high_cardinality_columns(analyzed),
        "leakage_candidates": _build_leakage_candidates(
            analyzed,
            target,
            target_relationship["numeric_associations"] if target_relationship else None,
        ),
        "correlation": _build_correlation(analyzed),
        "target_relationship": target_relationship,
        "time_pattern": (
            _build_time_pattern(analyzed, time_column, y, target_type, analyzed_parsed_time)
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
    parser.add_argument(
        "--positive-class",
        default=None,
        help="이진 타깃에서 양성으로 볼 값. 생략하면 자동 판별한다",
    )
    parser.add_argument(
        "--multiclass-limit",
        type=int,
        default=DEFAULT_MULTICLASS_LIMIT,
        help="이 고유값 수 이하의 비연속 타깃을 다중분류로 본다",
    )
    parser.add_argument(
        "--min-association-sample",
        type=int,
        default=DEFAULT_MIN_ASSOCIATION_SAMPLE,
        help="연관도 계산에 필요한 최소 유효 표본",
    )
    args = parser.parse_args(argv)

    key_columns = args.key_columns.split(",") if args.key_columns else None
    result = profile_data(
        args.path,
        sample_threshold=args.sample_threshold,
        key_columns=key_columns,
        target=args.target,
        time_column=args.time_column,
        positive_class=args.positive_class,
        multiclass_limit=args.multiclass_limit,
        min_association_sample=args.min_association_sample,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
