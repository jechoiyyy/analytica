import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parent / "build_report.py"


def _module():
    assert MODULE_PATH.is_file(), "build_report 스크립트가 존재해야 합니다"
    spec = importlib.util.spec_from_file_location("build_report", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_markdown(tmp_path: Path, text: str) -> Path:
    md_path = tmp_path / "report.md"
    md_path.write_text(text, encoding="utf-8")
    return md_path


def test_basic_markdown_renders_title_table_and_list(tmp_path):
    mod = _module()
    md_path = _write_markdown(
        tmp_path,
        "# 분석 보고서\n\n"
        "| 컬럼 | 타입 |\n| --- | --- |\n| age | int |\n\n"
        "- 첫 번째 항목\n- 두 번째 항목\n",
    )
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="테스트 보고서")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    assert "<table>" in html
    assert "<li>" in html
    assert "테스트 보고서" in html
    assert "분석 보고서" in html


def test_existing_image_is_embedded_as_base64(tmp_path):
    mod = _module()
    chart_dir = tmp_path / "charts"
    figures_dir = chart_dir / "figures"
    figures_dir.mkdir(parents=True)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000155273de50000000049454e44ae426082"
    )
    (figures_dir / "hist_age.png").write_bytes(png_bytes)

    md_path = _write_markdown(tmp_path, "# 제목\n\n![age histogram](figures/hist_age.png)\n")
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(
        str(md_path), str(out_path), title="t", chart_dir=str(chart_dir)
    )

    assert result["status"] == "ok"
    assert result["embedded_charts"] == 1
    html = out_path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html


def test_missing_image_reference_adds_warning_but_continues(tmp_path):
    mod = _module()
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()

    md_path = _write_markdown(
        tmp_path, "# 제목\n\n![missing](figures/missing_chart.png)\n\n본문 계속.\n"
    )
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(
        str(md_path), str(out_path), title="t", chart_dir=str(chart_dir)
    )

    assert result["status"] == "ok"
    assert result["embedded_charts"] == 0
    assert any("missing_chart.png" in w for w in result["warnings"])
    html = out_path.read_text(encoding="utf-8")
    assert "본문 계속" in html


def test_confidence_tags_are_replaced_with_styled_spans(tmp_path):
    mod = _module()
    md_path = _write_markdown(
        tmp_path,
        "- 관찰 A [데이터 근거]\n"
        "- 관찰 B [도메인 지식 추정]\n"
        "- 관찰 C [현업 확인 필요]\n",
    )
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="t")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    assert '<span class="tag tag-evidence">데이터 근거</span>' in html
    assert '<span class="tag tag-assumption">도메인 지식 추정</span>' in html
    assert '<span class="tag tag-verify">현업 확인 필요</span>' in html
    assert "[데이터 근거]" not in html
    assert "[도메인 지식 추정]" not in html
    assert "[현업 확인 필요]" not in html


def test_no_external_resource_references_in_output(tmp_path):
    mod = _module()
    md_path = _write_markdown(tmp_path, "# 제목\n\n일반 본문 텍스트입니다.\n")
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="t")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "<link ", "@import", "<script src="):
        assert forbidden not in html


def test_visual_report_shell_promotes_first_heading_to_hero(tmp_path):
    mod = _module()
    md_path = _write_markdown(
        tmp_path,
        "# 환자별 사망확률 분석\n\n"
        "보고서 도입 문장입니다.\n\n"
        "## 의사결정 요약\n\n"
        "조건부 준비\n",
    )
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="브라우저 제목")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    assert '<header class="report-hero">' in html
    assert '<p class="report-eyebrow">ANALYTICA · DECISION REPORT</p>' in html
    assert '<h1 class="report-title">환자별 사망확률 분석</h1>' in html
    assert html.count("환자별 사망확률 분석") == 1
    assert "<title>브라우저 제목</title>" in html


def test_visual_report_shell_wraps_h2_sections_as_numbered_cards(tmp_path):
    mod = _module()
    md_path = _write_markdown(
        tmp_path,
        "# 분석 보고서\n\n"
        "## 의사결정 요약\n\n"
        "- 첫 번째 판단\n- 두 번째 판단\n\n"
        "## 품질 scorecard\n\n"
        "| 항목 | 상태 |\n| --- | --- |\n| 결측 | 주의 |\n",
    )
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="분석 보고서")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    assert html.count('<section class="report-section') == 2
    assert '<section class="report-section report-section--summary">' in html
    assert '<span class="section-index">01</span>' in html
    assert '<span class="section-index">02</span>' in html
    assert '<main class="report-main">' in html


def test_visual_report_css_uses_responsive_offline_design_tokens(tmp_path):
    mod = _module()
    md_path = _write_markdown(tmp_path, "# 제목\n\n## 요약\n\n본문\n")
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(md_path), str(out_path), title="제목")

    assert result["status"] == "ok"
    html = out_path.read_text(encoding="utf-8")
    assert "--blue: #3e6ae1;" in html
    assert "--carbon: #171a20;" in html
    assert "@media (max-width: 720px)" in html
    assert "@media print" in html
    assert "<script" not in html
    assert "<link " not in html


def test_missing_markdown_path_returns_error(tmp_path):
    mod = _module()
    missing_path = tmp_path / "missing.md"
    out_path = tmp_path / "report.html"

    result = mod.build_html_report(str(missing_path), str(out_path), title="t")

    assert result["status"] == "error"
    assert result["error"]["reason"]
    assert result["error"]["hint"]


def test_cli_main_prints_json_and_returns_zero_on_success(tmp_path, capsys):
    mod = _module()
    md_path = _write_markdown(tmp_path, "# 제목\n\n본문\n")
    out_path = tmp_path / "report.html"

    exit_code = mod.main([str(md_path), "--out", str(out_path), "--title", "제목"])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured["status"] == "ok"
    assert out_path.is_file()


def test_cli_main_returns_nonzero_on_error(tmp_path, capsys):
    mod = _module()
    missing_path = tmp_path / "missing.md"
    out_path = tmp_path / "report.html"

    exit_code = mod.main([str(missing_path), "--out", str(out_path), "--title", "제목"])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["status"] == "error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
