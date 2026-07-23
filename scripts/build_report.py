#!/usr/bin/env python3
"""
Analytica [5] 보고서 렌더링 (report.html)

report.md로 이미 큐레이션된 마크다운(SKILL.md 오케스트레이션 단계에서 LLM이 작성)을
읽어 docs/UI_GUIDE.md 스펙에 맞는 자체완결 HTML로 "렌더링"만 한다. 어떤 내용을
리포트에 담을지는 이 스크립트의 책임이 아니다 (ADR-004: 계산은 Python, 해석은 LLM).

출력 JSON 스키마 (성공):
{
  "status": "ok",
  "out_path": "<out_path>",
  "embedded_charts": int,
  "warnings": [str, ...]
}

출력 JSON 스키마 (실패):
{
  "status": "error",
  "error": {"reason": str, "hint": str}
}
"""

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import markdown

CONFIDENCE_TAGS = [
    (re.compile(r"\[데이터 근거\]"), '<span class="tag tag-evidence">데이터 근거</span>'),
    (re.compile(r"\[도메인 지식 추정\]"), '<span class="tag tag-assumption">도메인 지식 추정</span>'),
    (re.compile(r"\[현업 확인 필요\]"), '<span class="tag tag-verify">현업 확인 필요</span>'),
]

IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}

CSS = '''
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    html, body {
      background: #ffffff;
      color: #1f2937;
      font-family: Pretendard, "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.7;
      margin: 0;
      padding: 0;
    }
    .container { max-width: 860px; margin: 0 auto; padding: 32px 24px; }
    h1, h2, h3 { color: #111827; font-weight: 600; }
    .container h1 { font-size: 28px; }
    .container h2 { font-size: 20px; margin-top: 48px; }
    .container h3 { font-size: 17px; margin-top: 32px; }
    .container > *:not(:first-child) { margin-top: 16px; }
    a { color: #1d4ed8; }
    .meta { color: #6b7280; font-size: 13px; }
    code, pre { font-family: ui-monospace, "D2Coding", monospace; }
    img { max-width: 100%; border: 1px solid #e5e7eb; }
    .table-wrap { overflow-x: auto; }
    table { font-size: 14px; border-collapse: collapse; width: 100%; }
    th { background: #f8f9fa; text-align: left; }
    td, th { border-bottom: 1px solid #e5e7eb; padding: 8px 12px; }
    .tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 4px; }
    .tag-evidence { color: #16a34a; background: #f0fdf4; border: 1px solid #bbf7d0; }
    .tag-assumption { color: #d97706; background: #fffbeb; border: 1px solid #fde68a; }
    .tag-verify { color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; }
    @media print {
      * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    }
'''


def _error(reason: str, hint: str) -> dict:
    return {"status": "error", "error": {"reason": reason, "hint": hint}}


def _replace_confidence_tags(text: str) -> str:
    for pattern, replacement in CONFIDENCE_TAGS:
        text = pattern.sub(replacement, text)
    return text


def _embed_images(text: str, chart_dir: str | None) -> tuple[str, int, list[str]]:
    warnings: list[str] = []
    embedded_count = 0

    def _replace(match: re.Match) -> str:
        nonlocal embedded_count
        alt, rel_path = match.group(1), match.group(2)
        file_path = Path(chart_dir) / rel_path if chart_dir is not None else None
        if file_path is None or not file_path.is_file():
            warnings.append(f"{rel_path} 를 찾을 수 없어 건너뜀")
            return ""
        mime = MIME_TYPES.get(file_path.suffix.lower(), "image/png")
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        embedded_count += 1
        return f"![{alt}](data:{mime};base64,{data})"

    new_text = IMAGE_PATTERN.sub(_replace, text)
    return new_text, embedded_count, warnings


def _wrap_tables(html_body: str) -> str:
    html_body = html_body.replace("<table>", '<div class="table-wrap">\n<table>')
    html_body = html_body.replace("</table>", "</table>\n</div>")
    return html_body


def build_html_report(
    markdown_path: str,
    out_path: str,
    title: str,
    chart_dir: str | None = None,
) -> dict:
    md_file = Path(markdown_path)
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError as exc:
        return _error(f"마크다운 파일을 읽을 수 없습니다: {exc}", "markdown_path 경로를 확인하세요.")

    text = _replace_confidence_tags(text)
    text, embedded_charts, warnings = _embed_images(text, chart_dir)

    body_html = markdown.markdown(text, extensions=["tables", "fenced_code"])
    body_html = _wrap_tables(body_html)

    html_doc = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
{body_html}
</div>
</body>
</html>
'''

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html_doc, encoding="utf-8")

    return {
        "status": "ok",
        "out_path": str(out_file),
        "embedded_charts": embedded_charts,
        "warnings": warnings,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analytica 보고서 HTML 렌더링")
    parser.add_argument("markdown_path", help="렌더링할 마크다운 파일 경로 (report.md)")
    parser.add_argument("--out", required=True, help="출력 HTML 파일 경로")
    parser.add_argument("--title", required=True, help="리포트 제목")
    parser.add_argument("--chart-dir", default=None, help="이미지 상대경로의 기준 디렉토리")
    args = parser.parse_args(argv)

    result = build_html_report(
        args.markdown_path, args.out, title=args.title, chart_dir=args.chart_dir
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
