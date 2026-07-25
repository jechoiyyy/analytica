# Analytica

CSV, Excel, Parquet 데이터를 대상으로 데이터 품질 진단, 전처리, 시각화와
Markdown/HTML 보고서 생성을 안내하는 Codex·Claude Code 공용 분석
플러그인입니다.

원본 데이터는 수정하지 않으며 결과는 기본적으로
`analytica_output/<작업명>/`에 생성합니다.

## 사전 준비

- 이 private 저장소에 대한 GitHub 접근 권한
- Git
- Python 3.11 이상
- Codex CLI 또는 Claude Code

## 설치

분석할 프로젝트의 루트에서 저장소를 `analytica`라는 이름으로 clone합니다.

```bash
git clone git@github.com:jechoiyyy/analytica.git analytica
python3 -m venv .venv
.venv/bin/python -m pip install -r analytica/requirements-dev.txt
```

권장 디렉터리 구조는 다음과 같습니다.

```text
your-project/
├── .venv/
├── analytica/
├── data/
└── analytica_output/    # 분석 실행 후 생성
```

> 이후 명령은 `analytica/` 내부가 아니라 `your-project/`에서 실행합니다.

## Codex에서 사용

프로젝트 로컬 skill 경로를 연결합니다.

```bash
mkdir -p .agents/skills
ln -s "$(pwd)/analytica/skills/analytica" .agents/skills/analytica
```

새 Codex 세션을 시작한 뒤 데이터 경로와 함께 skill을 호출합니다.

```text
$analytica data/training.csv를 분석해줘
```

Codex가 분석 목적과 타깃, 데이터 단위 등을 확인하면 질문에 답하고 처리안을
승인합니다. 기본값으로 진행하려면 대화에서 그렇게 요청하면 됩니다.

## Claude Code에서 사용

프로젝트 루트에서 로컬 플러그인을 지정해 Claude Code를 시작합니다.

```bash
claude --plugin-dir ./analytica
```

세션 안에서 다음과 같이 실행합니다.

```text
/analytica:analyze data/training.csv
```

## 지원 범위

- 입력: CSV, Excel(`.xlsx`, `.xls`), Parquet
- 데이터 로드 및 스키마 확인
- 결측치, 중복, 이상치와 범주형 값 진단
- 타깃·시간 컬럼 관계 분석
- 승인된 전처리 계획 적용
- 재현 가능한 전처리 Python 스크립트 생성
- 차트와 Markdown/HTML 보고서 생성

생성되는 대표 산출물은 다음과 같습니다.

```text
analytica_output/<작업명>/
├── data/
│   └── cleaned.csv
├── scripts/
│   └── preprocess.py
├── figures/
│   └── *.png
├── report.md
└── report.html
```

실제 산출물은 분석 내용과 승인한 처리 계획에 따라 달라질 수 있습니다.

## 설치 확인

프로젝트 루트에서 다음 명령으로 데이터 로더가 실행되는지 확인할 수 있습니다.

```bash
.venv/bin/python analytica/scripts/load_data.py data/training.csv --sample-size 5
```

성공하면 표준 출력 JSON의 `status`가 `ok`로 표시됩니다.

스크립트의 계산 로직은 `analytica/tests/`에 테스트가 있습니다.

```bash
.venv/bin/python -m pytest analytica/tests/ -q
```

## 업데이트

```bash
git -C analytica pull
.venv/bin/python -m pip install -r analytica/requirements-dev.txt
```

업데이트된 skill을 확실히 반영하려면 Codex 또는 Claude Code 세션을 다시
시작합니다.

## 문제 해결

### `analytica/scripts/...` 파일을 찾을 수 없는 경우

현재 위치를 확인합니다. 모든 실행 명령은 clone한 `analytica/`의 상위
프로젝트 디렉터리에서 실행해야 합니다.

### Python 패키지를 찾을 수 없는 경우

가상환경의 Python으로 의존성을 다시 설치합니다.

```bash
.venv/bin/python -m pip install -r analytica/requirements-dev.txt
```

### Codex에서 `$analytica`가 보이지 않는 경우

심볼릭 링크와 경로를 확인한 뒤 새 Codex 세션을 시작합니다.

```bash
ls -l .agents/skills/analytica
```

### Claude Code에서 명령이 보이지 않는 경우

프로젝트 루트에서 `--plugin-dir ./analytica` 옵션으로 새 세션을
시작했는지 확인합니다.
