#!/usr/bin/env python3
"""
Analytica [0] 데이터 로드 & 검증

지원 포맷: CSV(.csv), Excel(.xlsx/.xls), Parquet(.parquet)
CSV는 utf-8 -> cp949 순으로 인코딩을 자동 감지한다.

출력 JSON 스키마 (성공):
{
  "status": "ok",
  "path": "<입력 경로>",
  "format": "csv" | "excel" | "parquet",
  "encoding": "utf-8" | "cp949" | null,
  "shape": {"rows": int, "columns": int},
  "columns": [{"name": str, "dtype": str}, ...],
  "sample": [ {컬럼명: 값, ...}, ... ]
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
from pathlib import Path

import pandas as pd

CSV_ENCODINGS = ("utf-8", "cp949")
SUPPORTED_SUFFIXES = {".csv": "csv", ".xlsx": "excel", ".xls": "excel", ".parquet": "parquet"}
DEFAULT_SAMPLE_SIZE = 5


class DataLoadError(Exception):
    """지원하지 않는 형식, 파일 없음, 디코딩 실패 등 로드 실패 사유를 reason/hint로 전달한다."""

    def __init__(self, reason: str, hint: str):
        self.reason = reason
        self.hint = hint
        super().__init__(reason)


def _error(path: str, reason: str, hint: str) -> dict:
    return {"status": "error", "path": path, "error": {"reason": reason, "hint": hint}}


def _read_csv(path: Path):
    last_exc: Exception | None = None
    for encoding in CSV_ENCODINGS:
        try:
            return pd.read_csv(path, encoding=encoding), encoding
        except UnicodeDecodeError as exc:
            last_exc = exc
    raise ValueError(
        f"지원하는 인코딩({', '.join(CSV_ENCODINGS)})으로 디코딩할 수 없습니다."
    ) from last_exc


def _to_sample(df: pd.DataFrame, sample_size: int) -> list:
    head = df.head(sample_size)
    return json.loads(head.to_json(orient="records", force_ascii=False))


def read_dataframe(path: str) -> tuple[pd.DataFrame, str | None, str]:
    """파일을 로드해 (DataFrame, encoding_or_None, format) 을 반환한다.
    format은 "csv"|"excel"|"parquet". 실패 시 DataLoadError를 발생시킨다."""
    file_path = Path(path)

    if not file_path.is_file():
        raise DataLoadError(
            f"파일을 찾을 수 없습니다: {path}",
            "경로를 다시 확인하거나 파일이 존재하는지 확인하세요.",
        )

    suffix = file_path.suffix.lower()
    fmt = SUPPORTED_SUFFIXES.get(suffix)
    if fmt is None:
        supported = ", ".join(sorted(set(SUPPORTED_SUFFIXES.values())))
        raise DataLoadError(
            f"지원하지 않는 파일 형식입니다: {suffix or '(확장자 없음)'}",
            f"지원 형식: {supported} (확장자: {', '.join(sorted(SUPPORTED_SUFFIXES))})",
        )

    encoding = None
    try:
        if fmt == "csv":
            df, encoding = _read_csv(file_path)
        elif fmt == "excel":
            df = pd.read_excel(file_path)
        else:
            df = pd.read_parquet(file_path)
    except ImportError as exc:
        raise DataLoadError(
            f"{fmt} 형식을 읽는 데 필요한 라이브러리가 없습니다: {exc}",
            "requirements-dev.txt의 의존성을 설치한 뒤 다시 시도하세요 "
            "(.venv/bin/python -m pip install -r requirements-dev.txt).",
        ) from exc
    except ValueError as exc:
        raise DataLoadError(
            str(exc), "파일 인코딩을 확인하거나 원본 데이터를 다시 내보내세요."
        ) from exc
    except Exception as exc:
        raise DataLoadError(
            f"파일을 읽는 중 오류가 발생했습니다: {exc}",
            "파일이 손상되지 않았는지 확인하세요.",
        ) from exc

    return df, encoding, fmt


def load_data(path: str, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict:
    try:
        df, encoding, fmt = read_dataframe(path)
    except DataLoadError as exc:
        return _error(path, exc.reason, exc.hint)

    columns = [{"name": str(name), "dtype": str(dtype)} for name, dtype in df.dtypes.items()]

    return {
        "status": "ok",
        "path": path,
        "format": fmt,
        "encoding": encoding,
        "shape": {"rows": int(df.shape[0]), "columns": int(df.shape[1])},
        "columns": columns,
        "sample": _to_sample(df, sample_size),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analytica 데이터 로드 & 검증")
    parser.add_argument("path", help="분석할 데이터 파일 경로 (csv/xlsx/xls/parquet)")
    parser.add_argument(
        "--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE, help="샘플로 출력할 행 수"
    )
    args = parser.parse_args(argv)

    result = load_data(args.path, sample_size=args.sample_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
