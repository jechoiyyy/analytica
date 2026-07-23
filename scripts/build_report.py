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
import html as html_lib
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
    :root {
      color-scheme: light;
      --blue: #3e6ae1;
      --blue-dark: #2f57c8;
      --blue-soft: #eef3ff;
      --carbon: #171a20;
      --graphite: #393c41;
      --pewter: #5c5e62;
      --silver: #8e8e8e;
      --mist: #f8f9fb;
      --border: #d0d1d2;
      --green: #2c7a62;
      --amber: #a15c00;
      --red: #b42318;
    }
    * { box-sizing: border-box; }
    html {
      background: #e9ebef;
      scroll-behavior: smooth;
    }
    body {
      margin: 0;
      color: var(--graphite);
      background: #e9ebef;
      font-family: "Apple SD Gothic Neo", "Malgun Gothic", Arial, system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.72;
      -webkit-font-smoothing: antialiased;
    }
    .report-hero {
      position: relative;
      overflow: hidden;
      color: #ffffff;
      background:
        radial-gradient(circle at 88% 18%, rgba(107, 139, 232, .38), transparent 26%),
        linear-gradient(128deg, var(--carbon) 0%, #26304a 70%, #344f8f 100%);
    }
    .report-hero::after {
      position: absolute;
      right: -96px;
      bottom: -170px;
      width: 420px;
      height: 420px;
      border: 1px solid rgba(255, 255, 255, .12);
      border-radius: 50%;
      content: "";
    }
    .report-hero__inner {
      position: relative;
      z-index: 1;
      max-width: 1120px;
      margin: 0 auto;
      padding: 72px 36px 84px;
    }
    .report-eyebrow {
      margin: 0;
      color: #aebff0;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .13em;
    }
    .report-title {
      max-width: 880px;
      margin: 18px 0 0;
      color: #ffffff;
      font-size: clamp(36px, 5vw, 58px);
      line-height: 1.12;
      letter-spacing: -.04em;
    }
    .report-lead {
      max-width: 720px;
      margin: 22px 0 0;
      color: #d8deea;
      font-size: 17px;
      line-height: 1.65;
    }
    .report-flow {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 34px;
      color: #dfe7ff;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .08em;
    }
    .report-flow span {
      padding: 7px 10px;
      border: 1px solid rgba(255, 255, 255, .18);
      background: rgba(255, 255, 255, .06);
    }
    .report-flow i {
      color: #9bb3f5;
      font-style: normal;
    }
    .report-main {
      position: relative;
      z-index: 2;
      max-width: 1120px;
      margin: -34px auto 0;
      padding: 0 36px 72px;
    }
    .report-intro,
    .report-section {
      margin: 0 0 22px;
      padding: 32px 36px;
      border: 1px solid var(--border);
      background: #ffffff;
      box-shadow: 0 8px 28px rgba(23, 26, 32, .055);
    }
    .report-intro:empty { display: none; }
    .report-section--summary {
      border-top: 3px solid var(--blue);
    }
    .section-heading {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      margin-bottom: 24px;
    }
    .section-index {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 42px;
      height: 30px;
      color: var(--blue);
      background: var(--blue-soft);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .06em;
    }
    h2, h3, h4 {
      color: var(--carbon);
      line-height: 1.35;
      letter-spacing: -.02em;
    }
    h2 {
      margin: 0;
      font-size: 25px;
      font-weight: 700;
    }
    h3 {
      margin: 30px 0 12px;
      padding-top: 4px;
      font-size: 18px;
    }
    h4 {
      margin: 24px 0 10px;
      font-size: 16px;
    }
    p { margin: 0 0 15px; }
    ul, ol {
      margin: 12px 0 20px;
      padding-left: 22px;
    }
    li + li { margin-top: 7px; }
    .report-section--summary > ul {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      padding: 0;
      list-style: none;
    }
    .report-section--summary > ul > li {
      margin: 0;
      padding: 14px 16px;
      border-left: 2px solid var(--blue);
      background: var(--mist);
    }
    strong { color: var(--carbon); }
    a {
      color: var(--blue-dark);
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }
    code, pre {
      font-family: ui-monospace, "D2Coding", monospace;
    }
    code {
      padding: 2px 5px;
      border-radius: 3px;
      background: #f0f1f3;
      font-size: .92em;
    }
    pre {
      overflow-x: auto;
      padding: 18px;
      border: 1px solid var(--border);
      background: var(--carbon);
      color: #f4f6fb;
      font-size: 13px;
      line-height: 1.55;
    }
    blockquote {
      margin: 20px 0;
      padding: 14px 18px;
      border-left: 3px solid var(--blue);
      background: var(--blue-soft);
    }
    blockquote > :last-child { margin-bottom: 0; }
    img {
      display: block;
      max-width: 100%;
      max-height: 520px;
      margin: 24px auto 30px;
      padding: 16px;
      object-fit: contain;
      border: 1px solid var(--border);
      background: #ffffff;
    }
    .table-wrap {
      overflow-x: auto;
      margin: 20px 0 26px;
      border: 1px solid var(--border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      line-height: 1.55;
    }
    thead { background: var(--mist); }
    th {
      color: var(--carbon);
      text-align: left;
      font-weight: 700;
    }
    td, th {
      padding: 11px 13px;
      border-bottom: 1px solid #e4e5e7;
      vertical-align: top;
    }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:hover { background: #fbfcff; }
    .tag {
      display: inline-block;
      margin: 1px 3px 1px 0;
      padding: 2px 7px;
      border: 1px solid currentColor;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.45;
      vertical-align: 1px;
    }
    .tag-evidence { color: var(--green); background: #eaf6f1; }
    .tag-assumption { color: var(--amber); background: #fff7e8; }
    .tag-verify { color: var(--red); background: #fff0ee; }
    .report-footer {
      padding: 28px 24px 34px;
      color: var(--silver);
      background: var(--carbon);
      font-size: 11px;
      text-align: center;
      letter-spacing: .04em;
    }
    @media (max-width: 720px) {
      body { font-size: 14px; }
      .report-hero__inner { padding: 50px 20px 68px; }
      .report-title { font-size: 36px; }
      .report-lead { font-size: 15px; }
      .report-main { margin-top: -24px; padding: 0 14px 46px; }
      .report-intro,
      .report-section { padding: 24px 20px; }
      .section-heading {
        grid-template-columns: 36px minmax(0, 1fr);
        gap: 10px;
      }
      .section-index { width: 36px; height: 27px; }
      h2 { font-size: 21px; }
      .report-section--summary > ul { grid-template-columns: 1fr; }
      td, th { padding: 9px 10px; }
    }
    @media print {
      * {
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }
      html, body { background: #ffffff; }
      .report-hero { break-after: page; }
      .report-main { margin-top: 0; padding-top: 28px; }
      .report-intro,
      .report-section {
        break-inside: avoid;
        box-shadow: none;
      }
      .report-footer { background: #ffffff; }
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


def _extract_hero_title(body_html: str, fallback_title: str) -> tuple[str, str]:
    match = re.match(r"\s*<h1>(.*?)</h1>\s*", body_html, flags=re.DOTALL)
    if match is None:
        return html_lib.escape(fallback_title), body_html
    return match.group(1), body_html[match.end() :]


def _wrap_report_sections(body_html: str) -> str:
    headings = list(re.finditer(r"<h2>(.*?)</h2>", body_html, flags=re.DOTALL))
    if not headings:
        return f'<section class="report-intro">{body_html}</section>'

    parts: list[str] = []
    preamble = body_html[: headings[0].start()].strip()
    if preamble:
        parts.append(f'<section class="report-intro">{preamble}</section>')

    for index, heading in enumerate(headings, start=1):
        end = headings[index].start() if index < len(headings) else len(body_html)
        section_body = body_html[heading.end() : end].strip()
        modifier = " report-section--summary" if index == 1 else ""
        parts.append(
            f'<section class="report-section{modifier}">'
            '<div class="section-heading">'
            f'<span class="section-index">{index:02d}</span>'
            f"<h2>{heading.group(1)}</h2>"
            "</div>"
            f"{section_body}"
            "</section>"
        )
    return "\n".join(parts)


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
    hero_title, body_html = _extract_hero_title(body_html, title)
    body_html = _wrap_report_sections(body_html)
    document_title = html_lib.escape(title)

    html_doc = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{document_title}</title>
<style>{CSS}</style>
</head>
<body>
<header class="report-hero">
  <div class="report-hero__inner">
    <p class="report-eyebrow">ANALYTICA · DECISION REPORT</p>
    <h1 class="report-title">{hero_title}</h1>
    <p class="report-lead">데이터 근거, 판단 조건, 다음 액션을 한 흐름으로 정리한 자체완결 분석 보고서입니다.</p>
    <div class="report-flow" aria-label="보고서 흐름">
      <span>EVIDENCE</span><i>→</i><span>DECISION</span><i>→</i><span>ACTION</span>
    </div>
  </div>
</header>
<main class="report-main">
{body_html}
</main>
<footer class="report-footer">ANALYTICA · SELF-CONTAINED DATA ANALYSIS REPORT</footer>
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
