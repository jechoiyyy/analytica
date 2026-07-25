"""profile_data() 전체 출력 회귀 테스트.

가장 중요한 회귀는 `test_string_binary_target_still_produces_associations` 다.
기존 구현에서 문자열 이진 타깃("Yes"/"No")은 numeric_correlations 가 빈 배열이 되고
상관 기반 누출 탐지도 꺼졌다. ICU 케이스는 타깃이 int64 0/1 이라 이 구멍을 드러내지
못했다.
"""

import numpy as np
import pandas as pd
import pytest

from profile_data import profile_data


def _write(tmp_path, df, name="data.csv"):
    path = tmp_path / name
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def churn_frame():
    """이탈 예측 형태: 문자열 이진 타깃 + 강한 수치 신호 + 강한 범주 신호."""
    rng = np.random.default_rng(11)
    n = 600
    churned = np.array([True] * 120 + [False] * 480)
    rng.shuffle(churned)
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(n)],
            "tenure_months": np.where(churned, rng.normal(4, 1.5, n), rng.normal(30, 6, n)),
            "monthly_fee": rng.normal(50, 12, n),
            "plan": np.where(churned, "prepaid", "annual"),
            "region": rng.choice(["north", "south", "east"], size=n),
            "churn": np.where(churned, "Yes", "No"),
        }
    )


class TestStringBinaryTarget:
    def test_string_binary_target_still_produces_associations(self, tmp_path, churn_frame):
        result = profile_data(_write(tmp_path, churn_frame), target="churn")

        assert result["status"] == "ok"
        rel = result["target_relationship"]
        assert rel["target_type"] == "binary"
        # 회귀의 핵심: 예전에는 빈 배열이었다.
        assert len(rel["numeric_associations"]) > 0

        by_name = {item["name"]: item for item in rel["numeric_associations"]}
        assert by_name["tenure_months"]["metric"] == "auc"
        # 가입기간이 짧을수록 이탈 → 음의 방향, 강한 신호
        assert by_name["tenure_months"]["direction"] == "negative"
        assert by_name["tenure_months"]["strength"] > 0.8
        assert by_name["monthly_fee"]["strength"] < 0.3

    def test_associations_are_sorted_by_strength(self, tmp_path, churn_frame):
        rel = profile_data(_write(tmp_path, churn_frame), target="churn")["target_relationship"]
        strengths = [item["strength"] for item in rel["numeric_associations"]]
        assert strengths == sorted(strengths, reverse=True)

    def test_positive_class_is_resolved_and_recorded(self, tmp_path, churn_frame):
        rel = profile_data(_write(tmp_path, churn_frame), target="churn")["target_relationship"]
        assert rel["positive_class"] == "Yes"
        assert rel["positive_class_rule"] == "minority_class"
        assert rel["baseline"]["stat"] == "positive_rate"
        assert rel["baseline"]["value"] == pytest.approx(0.2, abs=0.01)

    def test_positive_class_can_be_overridden(self, tmp_path, churn_frame):
        rel = profile_data(
            _write(tmp_path, churn_frame), target="churn", positive_class="No"
        )["target_relationship"]
        assert rel["positive_class"] == "No"
        assert rel["positive_class_rule"] == "user_specified"
        assert rel["baseline"]["value"] == pytest.approx(0.8, abs=0.01)

    def test_unknown_positive_class_is_a_clean_error(self, tmp_path, churn_frame):
        result = profile_data(
            _write(tmp_path, churn_frame), target="churn", positive_class="Maybe"
        )
        assert result["status"] == "error"
        assert "Maybe" in result["error"]["reason"]

    def test_categorical_association_uses_cramers_v(self, tmp_path, churn_frame):
        rel = profile_data(_write(tmp_path, churn_frame), target="churn")["target_relationship"]
        by_name = {item["name"]: item for item in rel["categorical_associations"]}
        assert by_name["plan"]["metric"] == "cramers_v"
        assert by_name["plan"]["strength"] > 0.9
        assert by_name["region"]["strength"] < 0.2

    def test_leakage_detection_is_active_for_string_targets(self, tmp_path, churn_frame):
        # plan 은 타깃과 완전히 겹치는 사후 변수다. 예전에는 문자열 타깃이라 탐지되지 않았다.
        frame = churn_frame.copy()
        frame["settlement_amount"] = np.where(frame["churn"] == "Yes", 0.0, 100.0)
        result = profile_data(_write(tmp_path, frame), target="churn")

        candidates = {c["name"]: c for c in result["leakage_candidates"]}
        assert "settlement_amount" in candidates
        assert "near_perfect_target_separation" in candidates["settlement_amount"]["reasons"]
        assert candidates["settlement_amount"]["signal"] == "strong"

    def test_identifier_is_still_flagged(self, tmp_path, churn_frame):
        result = profile_data(_write(tmp_path, churn_frame), target="churn")
        candidates = {c["name"]: c for c in result["leakage_candidates"]}
        assert "near_unique_identifier" in candidates["customer_id"]["reasons"]

    def test_class_balance_reports_imbalance_ratio(self, tmp_path, churn_frame):
        rel = profile_data(_write(tmp_path, churn_frame), target="churn")["target_relationship"]
        assert rel["class_balance"]["imbalance_ratio"] == pytest.approx(4.0, abs=0.2)


class TestGroupDifferences:
    def test_binary_target_groups_report_positive_rate(self, tmp_path, churn_frame):
        rel = profile_data(_write(tmp_path, churn_frame), target="churn")["target_relationship"]
        entry = next(e for e in rel["categorical_group_differences"] if e["name"] == "plan")
        assert entry["stat"] == "positive_rate"
        rates = {g["category"]: g["value"] for g in entry["groups"]}
        assert rates["prepaid"] == pytest.approx(1.0)
        assert rates["annual"] == pytest.approx(0.0)

    def test_multiclass_target_reports_distribution_not_a_scalar(self, tmp_path):
        """예전 구현은 그룹마다 다른 최빈 클래스의 순도를 같은 mean_target 필드로 내보내
        비교 불가능한 값을 비교 가능한 것처럼 보이게 만들었다."""
        frame = pd.DataFrame(
            {
                "region": ["north"] * 100 + ["south"] * 100,
                "grade": ["A"] * 80 + ["B"] * 20 + ["C"] * 70 + ["B"] * 30,
            }
        )
        rel = profile_data(_write(tmp_path, frame), target="grade")["target_relationship"]
        assert rel["target_type"] == "multiclass"

        entry = next(e for e in rel["categorical_group_differences"] if e["name"] == "region")
        assert entry["stat"] == "class_distribution"
        groups = {g["category"]: g for g in entry["groups"]}
        assert "value" not in groups["north"]
        assert groups["north"]["distribution"]["A"] == pytest.approx(0.8)
        assert groups["south"]["distribution"]["C"] == pytest.approx(0.7)

    def test_continuous_target_groups_report_mean(self, tmp_path):
        frame = pd.DataFrame(
            {
                "segment": ["low"] * 100 + ["high"] * 100,
                "revenue": list(np.linspace(1, 10, 100)) + list(np.linspace(90, 100, 100)),
            }
        )
        rel = profile_data(_write(tmp_path, frame), target="revenue")["target_relationship"]
        assert rel["target_type"] == "continuous"
        entry = next(e for e in rel["categorical_group_differences"] if e["name"] == "segment")
        assert entry["stat"] == "mean"
        means = {g["category"]: g["value"] for g in entry["groups"]}
        assert means["high"] > means["low"]


class TestContinuousTarget:
    def test_continuous_target_uses_spearman_and_eta_squared(self, tmp_path):
        rng = np.random.default_rng(3)
        n = 400
        frame = pd.DataFrame(
            {
                "size": rng.uniform(1, 100, n),
                "noise": rng.normal(size=n),
                "grade": rng.choice(["a", "b"], size=n),
            }
        )
        frame["price"] = frame["size"] ** 2
        rel = profile_data(_write(tmp_path, frame), target="price")["target_relationship"]

        assert rel["target_type"] == "continuous"
        assert rel["baseline"]["stat"] == "mean"
        assert rel["class_balance"] is None

        by_name = {i["name"]: i for i in rel["numeric_associations"]}
        assert by_name["size"]["metric"] == "spearman"
        assert by_name["size"]["value"] == pytest.approx(1.0, abs=0.01)

        cats = {i["name"]: i for i in rel["categorical_associations"]}
        assert cats["grade"]["metric"] == "eta_squared"


class TestDegenerateTargets:
    def test_constant_target_does_not_crash(self, tmp_path):
        frame = pd.DataFrame({"x": range(100), "y": [1] * 100})
        result = profile_data(_write(tmp_path, frame), target="y")
        assert result["status"] == "ok"
        rel = result["target_relationship"]
        assert rel["target_type"] == "degenerate"
        assert rel["numeric_associations"] == []
        assert rel["class_balance"] is None

    def test_free_text_target_is_flagged_not_analysed(self, tmp_path):
        frame = pd.DataFrame(
            {"x": range(100), "note": [f"comment number {i}" for i in range(100)]}
        )
        rel = profile_data(_write(tmp_path, frame), target="note")["target_relationship"]
        assert rel["target_type"] == "high_cardinality_label"
        assert rel["numeric_associations"] == []


class TestUnchangedBehaviour:
    """타깃과 무관한 블록은 이번 변경의 영향을 받지 않아야 한다."""

    def test_core_blocks_are_intact(self, tmp_path, churn_frame):
        result = profile_data(_write(tmp_path, churn_frame), target="churn")
        assert result["shape"]["rows"] == 600
        assert result["sampled"] is False
        assert len(result["data_dictionary"]) == 6
        assert result["missing"]["overall_null_ratio"] == 0.0
        assert result["duplicates"]["duplicate_row_count"] == 0
        assert "high_cardinality_columns" in result

    def test_profiling_without_target_keeps_null_relationship(self, tmp_path, churn_frame):
        result = profile_data(_write(tmp_path, churn_frame))
        assert result["target_relationship"] is None
        # 타깃이 없어도 식별자 누출 탐지는 동작해야 한다.
        assert any(c["name"] == "customer_id" for c in result["leakage_candidates"])
