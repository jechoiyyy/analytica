"""타깃 타입 추상화 테스트.

기존 구현은 `is_numeric_dtype(target)`을 전제해 문자열 타깃에서 수치 연관 분석과
상관 기반 누출 탐지가 통째로 비활성화됐다. 이 테스트들은 타깃 타입을 dtype이 아니라
구조(고유값 수)로 판별하고, 타입별로 적절한 연관도 지표를 쓰는 것을 고정한다.
"""

import numpy as np
import pandas as pd
import pytest

from profile_data import (
    _cramers_v,
    _eta_squared,
    _infer_target_type,
    _numeric_association,
    _resolve_positive_class,
)


class TestInferTargetType:
    @pytest.mark.parametrize(
        "values, expected",
        [
            ([0, 1, 1, 0, 1], "binary"),
            (["Yes", "No", "Yes", "No"], "binary"),
            ([True, False, True, False], "binary"),
            ([0.0, 1.0, 0.0, 1.0], "binary"),
            (["A", "B", "C", "A", "B"], "multiclass"),
            ([1, 2, 3, 1, 2, 3], "multiclass"),
            ([7, 7, 7, 7], "degenerate"),
        ],
    )
    def test_structural_types(self, values, expected):
        assert _infer_target_type(pd.Series(values), multiclass_limit=20) == expected

    def test_float_with_many_values_is_continuous(self):
        series = pd.Series(np.linspace(0.0, 100.0, 500))
        assert _infer_target_type(series, multiclass_limit=20) == "continuous"

    def test_integer_beyond_multiclass_limit_is_continuous(self):
        series = pd.Series(range(200))
        assert _infer_target_type(series, multiclass_limit=20) == "continuous"

    def test_high_cardinality_string_is_not_a_usable_target(self):
        series = pd.Series([f"free text {i}" for i in range(50)])
        assert _infer_target_type(series, multiclass_limit=20) == "high_cardinality_label"

    def test_multiclass_limit_is_respected(self):
        series = pd.Series([f"c{i}" for i in range(10)])
        assert _infer_target_type(series, multiclass_limit=20) == "multiclass"
        assert _infer_target_type(series, multiclass_limit=5) == "high_cardinality_label"

    def test_nulls_are_ignored(self):
        series = pd.Series(["Yes", "No", None, "Yes"])
        assert _infer_target_type(series, multiclass_limit=20) == "binary"


class TestResolvePositiveClass:
    def test_zero_one_follows_numeric_convention(self):
        value, rule = _resolve_positive_class(pd.Series([0, 0, 0, 1]), None)
        assert value == 1
        assert rule == "numeric_convention"

    def test_boolean_convention(self):
        value, rule = _resolve_positive_class(pd.Series([True, False, False]), None)
        assert bool(value) is True
        assert rule == "boolean_convention"

    def test_falls_back_to_minority_class(self):
        # 관심 사건은 대개 희소하다.
        series = pd.Series(["retained"] * 90 + ["churned"] * 10)
        value, rule = _resolve_positive_class(series, None)
        assert value == "churned"
        assert rule == "minority_class"

    def test_user_override_wins(self):
        series = pd.Series(["retained"] * 90 + ["churned"] * 10)
        value, rule = _resolve_positive_class(series, "retained")
        assert value == "retained"
        assert rule == "user_specified"

    def test_user_override_accepts_string_form_of_numeric_value(self):
        value, rule = _resolve_positive_class(pd.Series([0, 0, 1]), "0")
        assert value == 0
        assert rule == "user_specified"

    def test_unknown_override_raises(self):
        with pytest.raises(ValueError):
            _resolve_positive_class(pd.Series(["a", "b"]), "zzz")

    def test_exact_tie_is_deterministic(self):
        series = pd.Series(["alpha", "beta"] * 50)
        first, rule = _resolve_positive_class(series, None)
        second, _ = _resolve_positive_class(series.sample(frac=1, random_state=1), None)
        assert first == second
        assert rule == "tie_broken_by_sort"


class TestNumericAssociationBinary:
    def _y(self, values):
        return pd.Series(values, dtype=float)

    def test_perfect_separation_reaches_auc_one(self):
        feature = pd.Series([1, 2, 3, 4, 5, 6] * 10)
        y = self._y([0, 0, 0, 1, 1, 1] * 10)
        result = _numeric_association(feature, y, "binary", min_sample=30)
        assert result["metric"] == "auc"
        assert result["value"] == pytest.approx(1.0)
        assert result["strength"] == pytest.approx(1.0)
        assert result["direction"] == "positive"

    def test_reversed_separation_keeps_strength_but_flips_direction(self):
        feature = pd.Series([6, 5, 4, 3, 2, 1] * 10)
        y = self._y([0, 0, 0, 1, 1, 1] * 10)
        result = _numeric_association(feature, y, "binary", min_sample=30)
        assert result["value"] == pytest.approx(0.0)
        # 방향은 음수지만 신호의 세기는 완전 분리와 동일해야 한다 (누출 탐지가 놓치면 안 된다).
        assert result["strength"] == pytest.approx(1.0)
        assert result["direction"] == "negative"

    def test_no_signal_sits_near_half(self):
        rng = np.random.default_rng(42)
        feature = pd.Series(rng.normal(size=2000))
        y = self._y(rng.integers(0, 2, size=2000))
        result = _numeric_association(feature, y, "binary", min_sample=30)
        assert result["value"] == pytest.approx(0.5, abs=0.05)
        assert result["strength"] < 0.1

    def test_returns_none_below_min_sample(self):
        feature = pd.Series([1, 2, 3])
        y = self._y([0, 1, 1])
        assert _numeric_association(feature, y, "binary", min_sample=30) is None

    def test_returns_none_for_constant_feature(self):
        feature = pd.Series([5] * 100)
        y = self._y([0, 1] * 50)
        assert _numeric_association(feature, y, "binary", min_sample=30) is None

    def test_returns_none_when_one_class_is_absent(self):
        feature = pd.Series(range(100))
        y = self._y([0] * 100)
        assert _numeric_association(feature, y, "binary", min_sample=30) is None

    def test_pairs_are_aligned_after_dropping_nulls(self):
        feature = pd.Series([1.0, np.nan, 3.0] * 40)
        y = self._y([0, 1, 1] * 40)
        result = _numeric_association(feature, y, "binary", min_sample=30)
        assert result["n"] == 80


class TestNumericAssociationContinuous:
    def test_monotonic_nonlinear_relation_is_caught_by_spearman(self):
        # Pearson 단독이었을 때 놓치던 관계. Spearman은 1.0, Pearson은 뚜렷하게 낮다.
        feature = pd.Series(np.arange(1, 101, dtype=float))
        y = pd.Series(np.exp(np.arange(1, 101) / 10.0))
        result = _numeric_association(feature, y, "continuous", min_sample=30)
        assert result["metric"] == "spearman"
        assert result["value"] == pytest.approx(1.0)
        assert result["strength"] == pytest.approx(1.0)
        assert result["direction"] == "positive"
        assert result["pearson"] < 0.95

    def test_negative_relation_reports_direction(self):
        feature = pd.Series(np.arange(100, dtype=float))
        y = pd.Series(np.arange(100, 0, -1, dtype=float))
        result = _numeric_association(feature, y, "continuous", min_sample=30)
        assert result["value"] == pytest.approx(-1.0)
        assert result["strength"] == pytest.approx(1.0)
        assert result["direction"] == "negative"


class TestNumericAssociationMulticlass:
    def test_group_separation_produces_high_eta_squared(self):
        feature = pd.Series([1.0] * 50 + [10.0] * 50 + [20.0] * 50)
        y = pd.Series(["a"] * 50 + ["b"] * 50 + ["c"] * 50)
        result = _numeric_association(feature, y, "multiclass", min_sample=30)
        assert result["metric"] == "eta_squared"
        assert result["strength"] == pytest.approx(1.0)
        assert result["direction"] is None

    def test_no_group_separation_produces_low_eta_squared(self):
        rng = np.random.default_rng(7)
        feature = pd.Series(rng.normal(size=300))
        y = pd.Series(["a", "b", "c"] * 100)
        result = _numeric_association(feature, y, "multiclass", min_sample=30)
        assert result["strength"] < 0.1


class TestCramersV:
    def test_perfect_association(self):
        a = pd.Series(["x"] * 50 + ["y"] * 50)
        b = pd.Series(["p"] * 50 + ["q"] * 50)
        assert _cramers_v(a, b) == pytest.approx(1.0)

    def test_independent_columns_are_near_zero(self):
        a = pd.Series(["x", "y"] * 200)
        b = pd.Series(["p", "p", "q", "q"] * 100)
        assert _cramers_v(a, b) == pytest.approx(0.0, abs=0.05)

    def test_single_level_returns_none(self):
        a = pd.Series(["x"] * 100)
        b = pd.Series(["p", "q"] * 50)
        assert _cramers_v(a, b) is None


class TestEtaSquared:
    def test_full_separation(self):
        values = pd.Series([1.0] * 50 + [9.0] * 50)
        groups = pd.Series(["a"] * 50 + ["b"] * 50)
        assert _eta_squared(values, groups) == pytest.approx(1.0)

    def test_constant_values_return_none(self):
        values = pd.Series([3.0] * 100)
        groups = pd.Series(["a", "b"] * 50)
        assert _eta_squared(values, groups) is None
