import csv
import html
import io
import json
from pathlib import Path
from typing import Any, cast

JsonDict = dict[str, Any]


CSV_FIELDS = (
    "plan_id", "planned_commitment", "source_reported_status", "match_confidence", "reporting_clarity",
    "findings", "plan_evidence", "update_evidence", "review_reasons", "policy_questions",
)


def _display(value: object) -> str:
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _cell(value: object) -> str:
    return html.escape(str(value or ""), quote=False).replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>").replace("\r", "<br>")


def _location(evidence: list[JsonDict], sources: JsonDict) -> str:
    locations = []
    for item in evidence:
        source = sources[item["source_id"]]
        locations.append(f"{source['sheet']}!{source['cell']} (hàng {source['source_row']}, {source['field']})")
    return "; ".join(dict.fromkeys(locations)) or "—"


def _finding_text(findings: list[JsonDict], sources: JsonDict) -> str:
    if not findings:
        return "Không có phát hiện"
    rendered = []
    for finding in findings:
        evidence = "<br>↳ " + "<br>↳ ".join(
            f"“{_cell(item['quote'])}” ({_cell(_location([item], sources))})" for item in finding["evidence"]
        ) if finding["evidence"] else ""
        rendered.append(
            f"{_cell(finding['rule'])} — {_cell(finding['severity'] or 'Chưa xác định')} "
            f"({_cell(finding['severity_status'])}): {_cell(finding['explanation'])} "
            f"[{_cell(finding.get('question_for_PM') or 'Không có câu hỏi')}]" + evidence
        )
    return "<br>".join(rendered)


def write_reports(
    run_dir: Path, scope: JsonDict, sources: JsonDict, commitments: list[JsonDict], assessments: list[JsonDict], metadata: JsonDict
) -> None:
    by_id = {item["plan_id"]: item for item in assessments}
    findings_count = sum(len(item.get("findings", [])) for item in assessments)
    affected_count = sum(bool(item.get("findings")) for item in assessments)
    comparison = {
        "metadata": metadata,
        "scope": {
            "project": scope["project"], "project_aliases": sorted(scope["project_aliases"]),
            "previous_sheet": scope["previous_sheet"], "current_sheet": scope["current_sheet"],
            "previous_rows": scope["previous_rows"], "current_rows": scope["current_rows"],
            "selection_warnings": scope["selection_warnings"],
        },
        "sources": sources,
        "commitments": commitments,
        "assessments": assessments,
        "counts": {"commitments": len(commitments), "affected_commitments": affected_count, "findings": findings_count},
    }
    comparison_text = json.dumps(comparison, ensure_ascii=False, indent=2) + "\n"

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for commitment in commitments:
        assessment = by_id[commitment["plan_id"]]
        row = {
            "plan_id": commitment["plan_id"],
            "planned_commitment": commitment["planned_commitment"],
            "source_reported_status": assessment["source_reported_status"] or "",
            "match_confidence": assessment["match_confidence"],
            "reporting_clarity": assessment["reporting_clarity"],
            "findings": json.dumps(assessment["findings"], ensure_ascii=False),
            "plan_evidence": json.dumps(commitment["plan_evidence"], ensure_ascii=False),
            "update_evidence": json.dumps(assessment["update_evidence"], ensure_ascii=False),
            "review_reasons": json.dumps(assessment["review_reasons"], ensure_ascii=False),
            "policy_questions": json.dumps(assessment["policy_questions"], ensure_ascii=False),
        }
        writer.writerow(cast(Any, {key: _display(value) for key, value in row.items()}))
    csv_text = stream.getvalue()
    markdown_rows = []
    for commitment in commitments:
        assessment = by_id[commitment["plan_id"]]
        update = assessment["update_evidence"]
        quotes = "<br>".join(_cell(item["quote"]) for item in update) or "Không tìm thấy trích dẫn cập nhật"
        update_locations = _cell(_location(update, sources))
        plan_locations = _cell(_location(commitment["plan_evidence"], sources))
        markdown_rows.append("| " + " | ".join((
            _cell(commitment["plan_id"]), _cell(commitment["planned_commitment"]), quotes,
            _cell(assessment["source_reported_status"] or "Không được báo cáo"),
            _cell(assessment["match_confidence"]), _cell(assessment["reporting_clarity"]),
            _finding_text(assessment["findings"], sources),
            f"Kế hoạch: {plan_locations}<br>Cập nhật: {update_locations}",
        )) + " |")
    mentor = "\n".join([
        "# Rà soát chất lượng báo cáo tuần",
        "",
        f"- Dự án: {_cell(scope['project'])} (alias: {_cell(', '.join(sorted(scope['project_aliases'])))})",
        f"- Kỳ kế hoạch: {_cell(scope['previous_sheet'])} → kỳ cập nhật: {_cell(scope['current_sheet'])}",
        f"- Số cam kết: {len(commitments)}; số cam kết có phát hiện: {affected_count}; tổng số phát hiện: {findings_count}.",
        "- Đánh giá chất lượng thông tin được báo cáo; không xác minh kết quả thực hiện ngoài nguồn báo cáo.",
        "- Mức độ nghiêm trọng được đánh dấu đề xuất/chưa xác định không phải chính sách đã phê duyệt.",
        "",
        "| Mã | Cam kết kỳ trước | Cập nhật được báo cáo | Trạng thái nguồn | Độ tin cậy ghép | Độ rõ báo cáo | Phát hiện / câu hỏi | Nguồn |",
        "|---|---|---|---|---|---|---|---|",
        *markdown_rows,
        "",
        "## Phạm vi tìm kiếm",
        f"Đã tìm trong Actual, Issue/Risk và Next Plan của mọi vai trò ở `{_cell(scope['current_sheet'])}`.",
        "",
    ])

    (run_dir / "comparison.json").write_text(comparison_text, encoding="utf-8")
    (run_dir / "comparison.csv").write_text("\ufeff" + csv_text, encoding="utf-8", newline="")
    (run_dir / "mentor_review.md").write_text(mentor, encoding="utf-8")
    write_run_notes(run_dir, metadata, scope, assessments)


def write_run_notes(run_dir: Path, metadata: JsonDict, scope: JsonDict, assessments: list[JsonDict], failure: JsonDict | None = None) -> None:
    unresolved = [question for item in assessments for question in item.get("policy_questions", [])]
    lines = [
        "# Ghi chú lần chạy", "",
        f"- Trạng thái: {'THẤT BẠI' if failure else 'Hoàn tất kiểm tra cấu trúc'}",
        f"- Thời điểm UTC: {metadata.get('created_at_utc', 'không có')}",
        f"- Model: {metadata.get('model', 'không gọi')}",
        f"- Phạm vi: {scope.get('previous_sheet', 'chưa xác định')} → {scope.get('current_sheet', 'chưa xác định')}",
        f"- SHA-256 input: {metadata.get('input_sha256', 'không có')}",
        f"- SHA-256 rules: {metadata.get('rules_sha256', 'không có')}",
        f"- Token usage: {json.dumps(metadata.get('usage', {}), ensure_ascii=False)}",
        f"- Bất định kỳ báo cáo: {json.dumps(metadata.get('period_context', {}), ensure_ascii=False)}",
        "",
        "## Cảnh báo và chính sách chưa chốt",
        *[f"- {warning}" for warning in scope.get("selection_warnings", [])],
        *[f"- Câu hỏi chính sách: {question}" for question in unresolved],
        "- Kiểm tra cấu trúc/trích dẫn không chứng minh tính đầy đủ ngữ nghĩa hoặc việc hoàn thành thực tế.",
    ]
    if failure:
        lines.extend(["", "## Chẩn đoán lỗi", f"- Giai đoạn: {failure.get('stage', 'unknown')}", f"- Lỗi: {failure.get('error', 'unknown')}"])
    (run_dir / "run_notes.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
