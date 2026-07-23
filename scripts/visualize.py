#!/usr/bin/env python3
"""
Analytica [2] 차트 생성

profile_data.py가 아닌, 호출자(LLM 오케스트레이션)가 지정한 컬럼 목록을 받아
결정적으로 PNG 차트를 렌더링한다. 어떤 컬럼을 그릴지 판단하지 않는다
(ADR-004: 계산은 Python, 해석은 LLM).

출력 JSON 스키마 (성공):
{
  "status": "ok",
  "path": "<입력 경로>",
  "out_dir": "<out_dir>",
  "korean_font_used": str | null,
  "charts": [
    {"file": "figures/hist_age.png", "kind": "histogram", "columns": ["age"]},
    ...
    {"file": "figures/correlation_heatmap.png", "kind": "correlation_heatmap",
     "columns": [...], "truncated": true}
  ],
  "warnings": [str, ...]   // 문제가 있었을 때만 포함
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

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import pandas as pd

from load_data import DataLoadError, read_dataframe

KOREAN_FONT_CANDIDATES = ("NanumGothic", "Malgun Gothic", "AppleGothic", "AppleSDGothicNeo")
CORRELATION_COLUMN_LIMIT = 20

COLOR_ACCENT = "#1d4ed8"
COLOR_SUCCESS = "#16a34a"
COLOR_WARNING = "#d97706"
COLOR_DANGER = "#dc2626"
COLOR_NEUTRAL = "#6b7280"


def _error(path: str, reason: str, hint: str) -> dict:
    return {"status": "error", "path": path, "error": {"reason": reason, "hint": hint}}


def _detect_korean_font() -> str | None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in KOREAN_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return None


def _select_existing_columns(df: pd.DataFrame, columns: list[str], warnings: list[str]) -> list[str]:
    existing = []
    for name in columns:
        if name in df.columns:
            existing.append(name)
        else:
            warnings.append(f"데이터에 존재하지 않는 컬럼을 건너뜁니다: {name}")
    return existing


def _save(fig, out_dir: Path, filename: str) -> str:
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    file_path = figures_dir / filename
    fig.savefig(file_path)
    plt.close(fig)
    return str(Path("figures") / filename)


def _generate_histograms(
    df: pd.DataFrame, columns: list[str], out_dir: Path, warnings: list[str]
) -> list[dict]:
    charts = []
    for name in _select_existing_columns(df, columns, warnings):
        values = df[name].dropna()
        fig, ax = plt.subplots()
        ax.hist(values, color=COLOR_ACCENT)
        ax.set_title(name)
        file_ref = _save(fig, out_dir, f"hist_{name}.png")
        charts.append({"file": file_ref, "kind": "histogram", "columns": [name]})
    return charts


def _generate_boxplots(
    df: pd.DataFrame, columns: list[str], out_dir: Path, warnings: list[str]
) -> list[dict]:
    charts = []
    for name in _select_existing_columns(df, columns, warnings):
        values = df[name].dropna()
        fig, ax = plt.subplots()
        box = ax.boxplot(values, patch_artist=True)
        for patch in box["boxes"]:
            patch.set_facecolor(COLOR_ACCENT)
        ax.set_title(name)
        file_ref = _save(fig, out_dir, f"box_{name}.png")
        charts.append({"file": file_ref, "kind": "boxplot", "columns": [name]})
    return charts


def _generate_correlation_heatmap(
    df: pd.DataFrame, columns: list[str], out_dir: Path, warnings: list[str]
) -> list[dict]:
    existing = _select_existing_columns(df, columns, warnings)
    if len(existing) < 2:
        return []

    truncated = len(existing) > CORRELATION_COLUMN_LIMIT
    used_columns = existing[:CORRELATION_COLUMN_LIMIT]

    corr = df[used_columns].corr()
    fig, ax = plt.subplots()
    im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(used_columns)))
    ax.set_xticklabels(used_columns, rotation=90)
    ax.set_yticks(range(len(used_columns)))
    ax.set_yticklabels(used_columns)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    file_ref = _save(fig, out_dir, "correlation_heatmap.png")

    chart = {"file": file_ref, "kind": "correlation_heatmap", "columns": used_columns}
    if truncated:
        chart["truncated"] = True
    return [chart]


def _generate_target_distribution(
    df: pd.DataFrame, target: str, out_dir: Path, warnings: list[str]
) -> list[dict]:
    existing = _select_existing_columns(df, [target], warnings)
    if not existing:
        return []

    values = df[target].dropna()
    fig, ax = plt.subplots()
    if pd.api.types.is_numeric_dtype(values) and values.nunique() > 10:
        ax.hist(values, color=COLOR_ACCENT)
    else:
        counts = values.value_counts()
        ax.bar(counts.index.astype(str), counts.values, color=COLOR_ACCENT)
    ax.set_title(target)
    file_ref = _save(fig, out_dir, "target_distribution.png")
    return [{"file": file_ref, "kind": "target_distribution", "columns": [target]}]


def _generate_time_pattern(
    df: pd.DataFrame, time_column: str, out_dir: Path, warnings: list[str]
) -> list[dict]:
    existing = _select_existing_columns(df, [time_column], warnings)
    if not existing:
        return []

    try:
        parsed = pd.to_datetime(df[time_column], errors="raise")
    except (ValueError, TypeError):
        warnings.append("time_column을 날짜로 해석할 수 없어 건너뜀")
        return []

    monthly_counts = parsed.dt.to_period("M").astype(str).value_counts().sort_index()
    fig, ax = plt.subplots()
    ax.plot(monthly_counts.index, monthly_counts.values, marker="o", color=COLOR_ACCENT)
    ax.set_title(time_column)
    ax.tick_params(axis="x", rotation=90)
    file_ref = _save(fig, out_dir, "time_pattern.png")
    return [{"file": file_ref, "kind": "time_pattern", "columns": [time_column]}]


def generate_charts(
    path: str,
    out_dir: str,
    target: str | None = None,
    time_column: str | None = None,
    histogram_columns: list[str] | None = None,
    boxplot_columns: list[str] | None = None,
    correlation_columns: list[str] | None = None,
) -> dict:
    try:
        df, _encoding, _fmt = read_dataframe(path)
    except DataLoadError as exc:
        return _error(path, exc.reason, exc.hint)

    korean_font = _detect_korean_font()
    if korean_font is not None:
        plt.rcParams["font.family"] = korean_font

    out_path = Path(out_dir)
    warnings: list[str] = []
    charts: list[dict] = []

    if histogram_columns:
        charts.extend(_generate_histograms(df, histogram_columns, out_path, warnings))
    if boxplot_columns:
        charts.extend(_generate_boxplots(df, boxplot_columns, out_path, warnings))
    if correlation_columns:
        charts.extend(_generate_correlation_heatmap(df, correlation_columns, out_path, warnings))
    if target is not None:
        charts.extend(_generate_target_distribution(df, target, out_path, warnings))
    if time_column is not None:
        charts.extend(_generate_time_pattern(df, time_column, out_path, warnings))

    result = {
        "status": "ok",
        "path": path,
        "out_dir": out_dir,
        "korean_font_used": korean_font,
        "charts": charts,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _split_columns(value: str | None) -> list[str] | None:
    return value.split(",") if value else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analytica 차트 생성")
    parser.add_argument("path", help="분석할 데이터 파일 경로 (csv/xlsx/xls/parquet)")
    parser.add_argument("--out", required=True, help="차트를 저장할 출력 디렉토리")
    parser.add_argument("--target", default=None, help="타깃 분포 차트를 그릴 컬럼")
    parser.add_argument("--time-column", default=None, help="시간 패턴 차트를 그릴 컬럼")
    parser.add_argument("--histogram-columns", default=None, help="히스토그램을 그릴 컬럼(쉼표로 구분)")
    parser.add_argument("--boxplot-columns", default=None, help="박스플롯을 그릴 컬럼(쉼표로 구분)")
    parser.add_argument(
        "--correlation-columns", default=None, help="상관 히트맵을 그릴 컬럼(쉼표로 구분)"
    )
    args = parser.parse_args(argv)

    result = generate_charts(
        args.path,
        args.out,
        target=args.target,
        time_column=args.time_column,
        histogram_columns=_split_columns(args.histogram_columns),
        boxplot_columns=_split_columns(args.boxplot_columns),
        correlation_columns=_split_columns(args.correlation_columns),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
