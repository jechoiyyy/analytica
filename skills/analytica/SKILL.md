# Analytica 데이터 분석 오케스트레이션

## 개요

데이터·도메인 지식이 얕은 분석 입문자와 분석 결과를 보고·발표에 활용해야 하는 실무자가 데이터 파일 하나로 다음을 마치도록 돕는다.

- 데이터 구성과 품질을 이해한다.
- 결측치·이상치·범주 오류·누출 위험을 진단하고, 원본을 보존하면서 정제 데이터와 재현 스크립트를 만든다.
- 데이터에 맞는 분리 전략, baseline 모델 후보, 평가지표를 추천한다.
- 전체 상세 `report.md`와 핵심 발견만 선별한 자체 완결 `report.html`을 만든다.

CSV, Excel, Parquet 파일을 대상으로 체크리스트 A~F, G·H 일부, L을 수행한다. 실제 모델 학습·튜닝·평가, 오류 분석, 배포·운영은 수행하지 않는다. 이 영역은 보고서의 다음 단계로만 안내한다.

통계·프로파일링·차트·전처리·HTML 렌더링 계산은 반드시 `analytica/scripts/`의 공통 Python 스크립트로 수행한다. 에이전트는 스크립트가 반환한 JSON을 해석해 질문, 처리 계획, 권고, 서술을 담당하며 수치를 직접 계산하거나 만들어내지 않는다. 판단 기준 원문은 `references/checklist.md`를 따른다.

## 사용 가능한 스크립트

명령은 저장소 루트에서 `.venv/bin/python`으로 실행한다. 성공 결과는 표준 출력 JSON이며 종료 코드는 0이다. 오류 결과는 표준 출력 JSON이며 종료 코드는 1이다.

### `load_data.py`

정확한 CLI:

```bash
.venv/bin/python analytica/scripts/load_data.py <path> [--sample-size <int>]
```

- `<path>`: CSV, XLSX, XLS, Parquet 파일 경로
- `--sample-size`: 출력할 샘플 행 수, 기본값 5

성공 JSON:

```text
status, path, format, encoding,
shape {rows, columns},
columns [{name, dtype}],
sample [{<column>: <value>}]
```

`format`은 `csv|excel|parquet`, `encoding`은 `utf-8|cp949|null`이다. 실패 JSON은 `status`, `path`, `error {reason, hint}`이다.

### `profile_data.py`

현재 구현된 정확한 CLI:

```bash
.venv/bin/python analytica/scripts/profile_data.py <path> \
  [--sample-threshold <int>] \
  [--key-columns <column1,column2,...>] \
  [--target <column>] \
  [--time-column <column>]
```

- `--sample-threshold`: 이 행 수를 초과하면 고정 난수 42로 샘플링한다. 기본값 500000
- `--key-columns`: 중복 키를 판단할 컬럼의 쉼표 구분 목록
- `--target`: 타깃 관계와 클래스 균형을 분석할 컬럼
- `--time-column`: 월별·요일별 시간 패턴을 분석할 날짜 컬럼

성공 JSON:

```text
status, path,
shape {rows, columns},
sampled, rows_analyzed,
data_dictionary [{
  name, dtype, non_null_count, null_count, null_ratio, n_unique, sample_values
}],
missing {
  overall_null_ratio, rows_with_any_null,
  by_column [{name, null_count, null_ratio}]
},
duplicates {
  duplicate_row_count, duplicate_row_ratio,
  duplicate_key_count, key_columns
},
outliers [{
  name, iqr_outlier_count, iqr_outlier_ratio,
  zscore_outlier_count, zscore_outlier_ratio
}],
categorical_issues [{
  name, has_leading_trailing_whitespace,
  case_variant_groups, rare_categories [{value, count}]
}],
correlation {
  high_correlation_pairs [{column_a, column_b, correlation}]
},
target_relationship {
  target,
  numeric_correlations [{name, correlation}],
  categorical_group_differences [{
    name, group_means [{category, mean_target, count}]
  }],
  class_balance {classes [{value, count, ratio}]} | null
} | null,
time_pattern {
  column,
  monthly [{month, count, mean_target?}],
  day_of_week [{day, count, mean_target?}]
} | null
```

실패 JSON은 `status`, `path`, `error {reason, hint}`이다. `correlation.high_correlation_pairs`는 절댓값이 0.9를 초과하는 상위 20쌍이다. `class_balance`는 타깃의 고유값이 10개 이하일 때만 만들어진다.

### `visualize.py`

정확한 CLI:

```bash
.venv/bin/python analytica/scripts/visualize.py <path> --out <output_dir> \
  [--target <column>] \
  [--time-column <column>] \
  [--histogram-columns <column1,column2,...>] \
  [--boxplot-columns <column1,column2,...>] \
  [--correlation-columns <column1,column2,...>]
```

에이전트가 프로파일링 결과를 근거로 실제로 필요한 컬럼만 지정한다. `--target`은 수치·범주를 자동 판별해 타깃 분포를 그리고, `--time-column`은 월별 건수 패턴을 그린다. 상관 히트맵은 최대 20개 컬럼을 사용한다.

성공 JSON:

```text
status, path, out_dir, korean_font_used,
charts [{file, kind, columns, truncated?}],
warnings?
```

`charts[].file`은 `figures/*.png` 상대 경로이고 `kind`는 `histogram|boxplot|correlation_heatmap|target_distribution|time_pattern`이다. 존재하지 않는 컬럼이나 날짜 파싱 실패는 가능한 차트 생성을 계속하고 `warnings`에 기록한다. 실패 JSON은 `status`, `path`, `error {reason, hint}`이다.

### `apply_preprocess.py`

정확한 CLI:

```bash
.venv/bin/python analytica/scripts/apply_preprocess.py <path> \
  --plan <plan.json> \
  --out <analytica_output/task_name>
```

`plan.json`은 다음 키를 사용한다.

```json
{
  "drop_columns": ["column"],
  "missing_value_actions": [
    {"column": "column", "strategy": "median|mode|constant|drop_column|drop_rows", "fill_value": "constant 전략에서 필수"}
  ],
  "outlier_actions": [
    {"column": "column", "strategy": "clip_iqr|drop_rows"}
  ],
  "categorical_cleanup": [
    {"column": "column", "mapping": {"old": "new"}}
  ],
  "date_derived_features": [
    {"column": "column", "derive": ["month", "day_of_week", "is_weekend", "days_elapsed"]}
  ],
  "business_derived_features": [
    {"name": "new", "op": "ratio", "numerator": "a", "denominator": "b"},
    {"name": "new", "op": "difference|sum", "left": "a", "right": "b"}
  ],
  "future_pipeline_columns": {
    "scale": ["numeric_column"],
    "one_hot_encode": ["categorical_column"]
  }
}
```

처리 순서는 `drop_columns` → `missing_value_actions` → `outlier_actions` → `categorical_cleanup` → `date_derived_features` → `business_derived_features`로 고정된다. `future_pipeline_columns`는 데이터에 적용하지 않고, train/test 분리 후 학습 데이터에만 fit할 `ColumnTransformer` 골격을 생성한다.

성공 JSON:

```text
status,
cleaned_path,
preprocess_script_path,
shape_before {rows, columns},
shape_after {rows, columns},
applied_actions,
warnings
```

실패 JSON은 `status`, `path`, `error {reason, hint}`이다. 원본 파일은 절대 수정하지 않는다.

### `build_report.py`

정확한 CLI:

```bash
.venv/bin/python analytica/scripts/build_report.py <markdown_path> \
  --out <output_html_path> \
  --title <report_title> \
  [--chart-dir <image_base_dir>]
```

`markdown_path`의 마크다운을 읽고 CSS를 인라인으로 포함한다. 이미지 경로는 `--chart-dir`을 기준으로 찾아 base64로 임베드한다.

성공 JSON:

```text
status, out_path, embedded_charts, warnings
```

실패 JSON은 `status`, `error {reason, hint}`이다.

## 워크플로우

다음 6단계를 순서대로 수행한다. 이전 단계가 실패하면 성공한 것처럼 다음 단계로 넘어가지 않는다.

### [0] 데이터 로드와 검증

1. 첫 번째 인자로 받은 데이터 파일 경로를 `load_data.py`에 전달한다.
2. 성공 JSON의 `format`, `encoding`, `shape`, `columns`, `sample`을 확인한다.
3. `status=error`이면 `error.reason`과 `error.hint`를 사용자 언어로 설명하고 분석을 중단한다.
4. 필요한 pandas, numpy, matplotlib 또는 포맷별 라이브러리가 없다는 오류면 `.venv/bin/python -m pip install -r requirements-dev.txt` 안내 후 중단한다.
5. 원본 경로는 이후 모든 단계에서 읽기 전용으로 취급한다.

### [1] 인터뷰

로드 JSON을 먼저 살펴보고 다음 정보를 사용자에게 묻는다. 모든 질문에 `모름`을 허용한다. `모름`인 항목은 JSON에서 추론하되 가정과 근거를 별도로 기록해 보고서에 남긴다. 컬럼명, dtype, 샘플값에서 감지한 후보를 가능한 한 선택지로 제시한다.

| 질문 | 목적 | 진행 기준 |
|---|---|---|
| 작업명 | 산출물 폴더명 | 기본값은 `<파일명>-<YYYYMMDD>`이다. 경로에 안전한 이름으로 확정한다. |
| 도메인 분야 | 도메인 인사이트 앵커링 | 커머스/금융/제조/의료/기타/모름 중 선택하게 한다. |
| 분석 목적 | 이후 전 단계의 판단 기준 | 회귀 예측/분류/군집/단순 탐색/모름 중 선택하게 한다. |
| 타깃 변수 | 타깃 관계와 다음 단계 설계 | 데이터의 컬럼 후보를 제시한다. 군집·단순 탐색이면 없음도 허용한다. |
| 데이터 단위 | 중복·집계 판단 기준 | 행 1개가 고객/주문/일자 등 무엇인지 묻는다. |
| 시간 컬럼 여부 | 시간 패턴과 분리 전략 | 날짜형 또는 날짜처럼 보이는 컬럼 후보와 없음을 제시한다. |
| 컬럼 의미 확인 | 데이터 사전 검증 | 추정한 컬럼 의미를 보여주고 틀린 항목만 정정받는다. |
| 개인정보/식별자 컬럼 제외 확인 | 개인정보 최소화와 ID 누출 방지 | 이름·전화·이메일·주민번호·고유 ID로 보이는 후보를 제시하고 제외 여부를 확인한다. |

추가로 데이터 출처, 사용 권한, 보관 정책은 자동 판단하지 말고 보고서의 사용자 기입/현업 확인 항목으로 남긴다.

### [2] 프로파일링과 품질 진단

1. `profile_data.py` CLI에 인터뷰에서 확정한 `--target`, `--time-column`, 필요하면 데이터 단위의 `--key-columns`를 전달한다.
2. 50만 행을 초과해 `sampled=true`이면 `rows_analyzed`와 함께 샘플링 기반 결과임을 이후 보고서에 명시한다.
3. 다음 JSON을 해석한다.
   - `data_dictionary`: 타입, 고유값, 결측, 샘플값
   - `missing`: 전체 및 컬럼별 결측
   - `duplicates`: 전체 행과 키 기준 중복
   - `outliers`: IQR 및 z-score 이상치
   - `categorical_issues`: 공백, 대소문자 변형, 희귀 범주
   - `correlation`: 강한 수치 상관
   - `target_relationship`: 타깃 상관, 범주 그룹 차이, 클래스 균형
   - `time_pattern`: 월별·요일별 건수와 수치 타깃 평균
4. 결과를 근거로 `visualize.py`에 타깃, 시간, 주요 수치 컬럼만 지정한다. 모든 컬럼을 기계적으로 그리지 않는다.
5. 차트는 `analytica_output/<작업명>/figures/`에 생성한다. 경고가 있으면 누락된 차트와 이유를 기록하고 나머지 결과를 사용한다.

### [3] 누출 점검과 전처리 적용

1. 누출 위험 후보를 자동 삭제하지 않는다. 먼저 근거와 함께 사용자에게 제시하고 제외 여부를 확인한다.
2. `correlation.high_correlation_pairs`에서 타깃이 포함되고 절댓값 상관이 0.9를 초과하는 쌍, `target_relationship.numeric_correlations`의 고상관 변수는 타깃 파생 또는 사후 변수 가능성으로 경고한다.
3. `data_dictionary`에서 고유값 비율이 거의 1인 컬럼과 인터뷰에서 식별자로 확인된 컬럼은 ID 누출 가능성으로 경고한다.
4. 컬럼이 예측 시점 이후에만 생성되는지는 데이터만으로 확정하지 않는다. `[현업 확인 필요]`로 표시하고 예측 시점에 조회 가능한지 묻는다.
5. 사용자 확인과 프로파일링 JSON에 따라 `plan.json`을 만든다.
   - 결측 대체·이상치 처리는 타입, 비율, 분포, 업무 의미를 근거로 선택한다.
   - `median`과 `mode`는 분석용 `cleaned.csv`를 만들기 위해 제공된 전체 데이터에서 계산하는 의도적 편의 처리임을 기록한다.
   - 범주 매핑은 관찰된 공백·대소문자·오탈자만 명시적으로 적는다.
   - 날짜 파생변수는 인터뷰에서 확인한 시간 의미에 맞게 선택한다.
   - 비즈니스 파생변수는 관찰 가능한 컬럼 조합과 도메인 가설을 함께 적고 `[도메인 지식 추정]`으로 표시한다.
   - 스케일링과 원-핫 인코딩은 `future_pipeline_columns`에만 기록한다.
   - cleaned.csv를 모델 학습 입력으로 그대로 재사용하지 않는다. 모델 검증·학습은 원본 데이터를 먼저 train/test 분리한 뒤 결측 대체·스케일링·인코딩을 학습 데이터에 다시 fit하는 Pipeline으로 수행한다.
6. `apply_preprocess.py <원본> --plan <plan.json> --out <작업 디렉터리>`를 실행한다.
7. `cleaned_path`, `preprocess_script_path`, 전후 shape, `applied_actions`, `warnings`를 확인하고 처리 이유와 경고를 보고서에 기록한다.

### [4] 다음 단계 설계

실제 모델을 학습하거나 튜닝하지 않는다. 다음 권고만 작성하며 각각 프로파일링 JSON과 인터뷰 답변을 근거로 연결한다.

- 미래 예측이고 시간 컬럼이 있으면 과거 학습/미래 평가와 `TimeSeriesSplit`을 우선 검토한다.
- 고객·매장·상품처럼 같은 그룹이 양쪽 split에 들어가면 안 되면 그룹 기준 분리와 `GroupKFold` 계열을 권고한다.
- 그 외에는 랜덤 분리를 검토한다.
- 분류이며 `target_relationship.class_balance`가 있으면 split에 stratify를 적용하고 `StratifiedKFold`를 검토한다.
- 회귀 baseline은 단순 기준값, 선형회귀, 얕은 트리부터 제안하고 MAE/RMSE/R² 중 업무 비용과 해석에 맞는 지표를 권고한다.
- 분류 baseline은 다수 클래스 기준, 로지스틱 회귀, 얕은 트리부터 제안한다. 불균형이면 Accuracy만 사용하지 말고 PR-AUC, Recall, F1을 중심으로 권고한다.
- 군집은 스케일링 후 단순 군집 후보와 Silhouette, Davies-Bouldin, Calinski-Harabasz를 권고하되 이 단계에서는 실행하지 않는다.
- 테스트셋은 최종 평가를 위해 봉인하고 튜닝에 반복 사용하지 말라고 명시한다.

### [5] 보고서 생성

#### 상세 `report.md`

`analytica_output/<작업명>/report.md`는 다음 순서의 전체 상세 기록으로 작성한다.

1. 문서 메타데이터: 작업명, 작성일, 원본 상대경로, 포맷·인코딩, shape,
   프로파일링 샘플링 여부와 분석 상태를 기록한다.
2. 분석 계약: 분석 목적, 사용자·의사결정, 타깃, 데이터 단위, 판단·예측 시점,
   성공 기준, 범위·제외 항목을 `확정|추정|미확정` 상태와 근거로 기록한다.
3. Executive summary와 준비도 판정:
   - `준비 완료`: 다음 단계 진입을 막는 알려진 문제가 없다.
   - `조건부 준비`: 진행 가능하지만 명시된 조건이나 잔여 위험이 있다.
   - `준비 미완료`: 타깃, 분석 단위, 판단 시점, 권한 또는 누출 blocker가 있다.
   - 준비도에는 근거와 다음 단계 진입 조건을 함께 쓴다.
4. 데이터 범위와 거버넌스: 데이터 기간, 단위, 키, 타깃, 시간·그룹·식별자,
   출처, 권한, 개인정보, 보관정책 상태를 기록한다. 알 수 없는 항목은 생략하지
   않고 `미확정`으로 표시한다.
5. 데이터 품질 scorecard: 결측, 중복·키, 이상치, 범주, 분포, 상관,
   시간·그룹, 타깃·불균형을 `정상|주의|차단|해당 없음` 상태, 근거, 영향과
   함께 요약한다.
6. 상세 품질·EDA: 각 항목을 `관찰 → 영향 → 처리 또는 유지 결정 → 잔여 위험`
   순서로 작성한다.
7. 타깃 관계와 인사이트: 주요 관계, 후보 신호, 결측·표본·단순 상관 한계,
   그룹 차이 및 선택적 `도메인 위험·가설` 블록을 기록한다. 타깃이 없으면 주요
   분포·구조 인사이트로 대체한다.
8. 누출·가용성·식별자 점검: 컬럼 또는 그룹별 역할, 위험, 결정, 사용 위치를
   표로 기록한다.
9. 이슈·결정 등록부: `ID, 이슈, 근거, 영향, 결정, 적용 결과, 잔여 위험`을
   기록한다. 전처리의 모든 변경과 의도적인 미처리는 이슈 ID에 연결한다.
10. 전처리 전후 결과: 전후 shape, 적용·미적용 처리와 이유, 경고, 원본 보존,
    `cleaned.csv` 제한, `plan.json`, `cleaned.csv`, `preprocess.py` 상대경로를
    기록한다.
11. 모델링 handoff: 모델링 입력 원본, 타깃, 포함·제외 특성, 그룹·시간 키,
    split·교차검증, fold 내 전처리, baseline, 주·보조 지표, 테스트셋 정책,
    미확정 사항을 한 표로 기록한다. 실제 학습은 수행하지 않았음을 명시한다.
12. 액션 계획: 데이터·분석 보완 액션과 결과를 활용할 업무 액션 또는 액션
    가설을 구분하고, 우선순위·담당자·완료 조건을 기록한다.
13. 한계와 주장하지 않는 내용: 답하지 못한 질문, 수행하지 않은 단계,
    일반화 한계, 인과·중요도·운영 성능처럼 주장할 수 없는 내용을 기록한다.
14. 현업 확인 목록: 본문의 모든 `[현업 확인 필요]`를 `ID, 우선순위,
    미확인 시 영향, 담당자, 상태`와 함께 모은다.
15. 재현성 manifest: 실행 스크립트와 옵션, 원본 상대경로, 분석 행 수와
    샘플링 여부, 전처리 계획·스크립트, 생성 산출물, 검증 명령과 결과를 기록한다.
16. 부록: 전체 데이터 사전, 상세 상관·그룹 결과, 본문 미사용 차트 목록,
    전체 처리 계획을 둔다. 전체 데이터 사전은 부록에만 둔다.

#### 계층형 `report.html`

HTML용 큐레이션 마크다운은 다음 순서로 작성한다.

1. 의사결정 요약: 분석 목적, 준비도, 핵심 수치, 핵심 발견 최대 5개,
   blocker 최대 3개, 첫 번째 우선 액션.
2. 분석 계약 요약: 목적, 단위, 타깃, 판단시점, 사용자, 성공 기준과 상태.
3. 품질 scorecard: 상세 보고서와 동일한 상태를 사용한 요약.
4. 핵심 발견과 차트: 관찰, 중요성, 결정·권고와 이를 직접 뒷받침하는 차트.
5. 처리 결과: 제거, 대체, 범주 정리, 유지·보류의 이유와 결과, 전후 shape,
   `cleaned.csv` 제한.
6. 모델링 handoff 요약: split, baseline, 지표, fold 내 전처리,
   테스트셋 정책.
7. 우선순위별 액션: 지금 결정할 것, 모델링 전 해결할 것, 이후 검증할 것.
8. 한계와 확인사항: 주장하지 않는 내용, 미확정 사항, 다음 단계 차단 조건.

HTML에는 전체 데이터 사전을 포함하지 않는다. 도메인별 위험과 가설은 선택적
블록으로 삽입하되 공통 골격을 대체하지 않는다. 발견을 직접 뒷받침하지 않는
차트는 제외한다.

#### 렌더링과 검증

1. 큐레이션 마크다운의 이미지 링크는 `figures/*.png` 상대경로로 쓰고
   `build_report.py --chart-dir`에는 작업 디렉터리를 전달한다.
2. `build_report.py`로 `analytica_output/<작업명>/report.html`을 생성하고
   `embedded_charts`와 `warnings`를 확인한다.
3. `report.md`와 `report.html`의 분석 계약, 준비도, 핵심 수치, 처리 결정,
   blocker 및 태그가 서로 모순되지 않는지 확인한다.
4. 모든 품질 상태, 준비도, 전처리 결정, 모델링 권고, 액션 제안에 신뢰도
   태그를 붙인다.
5. 모든 전처리 변경이 이슈·결정 등록부에 연결되고 모든 미확정 항목이 현업
   확인 목록에 집계됐는지 확인한다.
6. `report.html`에는 외부 CDN, 웹폰트, 스크립트 의존을 추가하지 않는다.

## 도메인 인사이트 규칙

모든 해석, 유의점, 중요 변수 후보, 파생변수 제안, 권고 문장에는 다음 태그 중 적절한 것을 붙인다. 한 문장에 근거와 추가 확인이 함께 있으면 태그를 둘 이상 붙일 수 있다.

- `[데이터 근거]`: 스크립트 JSON이나 생성 차트가 직접 뒷받침하는 관찰에 사용한다. 예: `[데이터 근거] age의 결측률은 profile JSON에서 18%로 관찰되었다.`
- `[도메인 지식 추정]`: 데이터만으로 확정할 수 없고 일반 도메인 지식에서 제안한 해석이나 파생변수에 사용한다. 예: `[도메인 지식 추정] 커머스 주문 데이터라면 구매금액/수량 비율을 객단가 후보로 검토할 수 있다.`
- `[현업 확인 필요]`: 컬럼 의미, 예측 시점 가용성, 누출 여부, 개인정보, 비용, 정책처럼 담당자 확인 없이는 확정할 수 없는 사항에 사용한다. 예: `[데이터 근거] paid_amount와 타깃의 상관은 0.97이다. [현업 확인 필요] 이 값이 결제 완료 후 생성된다면 사후 변수이므로 제외해야 한다.`

태그만 붙이고 근거를 생략하지 않는다. 수치에는 해당 JSON 필드나 차트를 함께 지목한다. 도메인 추정을 사실처럼 단정하지 않는다. `[현업 확인 필요]` 항목은 보고서 말미의 확인 목록에도 다시 모은다.

## 산출물 경로

작업명별로 다음 구조를 유지한다. 원본 데이터는 이 디렉터리로 옮기거나 덮어쓰지 않는다.

```text
analytica_output/
└── <작업명>/
    ├── report.md
    ├── report.html
    ├── data/
    │   └── cleaned.csv
    ├── scripts/
    │   └── preprocess.py
    └── figures/
        └── *.png
```

작업명 폴더로 분석을 격리해 다른 분석의 산출물을 덮어쓰지 않는다.

## 클라이언트 안내

이 지시문은 Claude Code와 Codex 등 다른 에이전트 환경에서 같은 순서와 공통 Python 구현을 사용한다.

- Claude Code에서 구조화된 질문 도구를 사용할 수 있으면 인터뷰 선택지와 확인 질문에 활용할 수 있다. 특정 도구 사용을 필수로 가정하지 않는다.
- Codex 등 다른 환경에서는 일반 대화형 질문으로 같은 8개 인터뷰 정보를 수집한다.
- 클라이언트별 차이는 질문 UI에만 한정한다. 로드, 프로파일링, 차트, 전처리, 보고서 렌더링은 모두 같은 `analytica/scripts/*.py`를 호출한다.
