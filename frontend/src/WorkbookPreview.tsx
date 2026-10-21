import { useEffect, useRef, useState } from "react";
import { SheetDetails } from "./SheetDetails";
import { ProjectComparison } from "./ProjectComparison";
import { chronologyLabel, mappingLabel, periodLabel, sheetStateLabel } from "./sourcePresentation";

export type SourceRow = {
  actual: string | null;
  next_plan: string | null;
  issue_risk: string | null;
  project_label: string | null;
  role_label: string | null;
  pic: string | null;
  source_row: number;
  source_refs: Record<string, string | null>;
  field_provenance: Record<string, unknown>;
  subgroup: unknown[];
  review_flags: string[];
};

export type WorkbookSheet = {
  snapshot_id: number;
  sheet_index: number;
  sheet_name: string;
  template_id: string | null;
  mapping_status: string;
  mapping_version: string | null;
  mapping_source_reference: string | null;
  sheet_state: string;
  chronology_status: string;
  reporting_period: { label?: string | null; year?: number | null; conflicts?: string[] } | null;
  mapping: Record<string, {
    status?: string;
    column_letter?: string;
    additional_columns?: string[];
    original_header_text?: string | null;
    header_cell_address?: string | null;
    [key: string]: unknown;
  }>;
  warnings: string[];
  row_count: number;
  rows: SourceRow[];
  cell_preview?: Array<{ row: number; cells: Array<{ address: string; column: string; value: string | null; truncated: boolean }> }>;
};

export type WorkbookPreviewData = {
  workbook_id: number;
  original_filename: string;
  imported_at_utc: string;
  sha256: string;
  sheets: WorkbookSheet[];
  tool_notice?: { operation: "mapping" | "alias"; snapshot_id: number };
};

type Props = {
  workbook: WorkbookPreviewData;
  selectedSnapshotId: number | null;
  onSelectSheet: (snapshotId: number) => void;
};

type SheetFilter = "all" | "attention" | "year";

function sheetAttentionReason(sheet: WorkbookSheet): string | null {
  if (sheet.mapping_status === "mapping_required") return "Cần thiết lập cột";
  if (sheet.chronology_status === "conflict") return "Thông tin kỳ mâu thuẫn";
  if (sheet.chronology_status === "unknown") return "Chưa xác định kỳ";
  if (sheet.sheet_state === "draft_or_incomplete") return "Nháp hoặc chưa đầy đủ";
  return null;
}

export function WorkbookPreview({ workbook, selectedSnapshotId, onSelectSheet }: Props) {
  const previewHeading = useRef<HTMLHeadingElement>(null);
  const [selectedProject, setSelectedProject] = useState<string | null>(null);
  const [projectQuery, setProjectQuery] = useState("");
  const [sheetQuery, setSheetQuery] = useState("");
  const [sheetFilter, setSheetFilter] = useState<SheetFilter>("all");
  const sheets = [...workbook.sheets].sort((a, b) => a.sheet_index - b.sheet_index);
  const selectedSheet = sheets.find((sheet) => sheet.snapshot_id === selectedSnapshotId) ?? null;
  const attentionSheets = [...new Map(sheets.filter((sheet) => sheetAttentionReason(sheet) !== null).map((sheet) => [sheet.snapshot_id, sheet])).values()];
  const yearContextSheets = sheets.filter((sheet) => sheet.chronology_status === "year_unknown" && sheetAttentionReason(sheet) === null);
  const visibleSheets = sheets.filter((sheet) => {
    const matchesFilter = sheetFilter === "all"
      || (sheetFilter === "attention" && sheetAttentionReason(sheet) !== null)
      || (sheetFilter === "year" && yearContextSheets.some((item) => item.snapshot_id === sheet.snapshot_id));
    return matchesFilter && sheet.sheet_name.toLocaleLowerCase("vi").includes(sheetQuery.trim().toLocaleLowerCase("vi"));
  });
  const aliasUrl = selectedSnapshotId === null
    ? `/workbooks/${workbook.workbook_id}`
    : `/workbooks/${workbook.workbook_id}?return_app=1&sheet=${selectedSnapshotId}&tool=aliases#aliases`;
  const projectNames = [...new Set(sheets
    .filter((sheet) => sheet.mapping_status !== "mapping_required")
    .flatMap((sheet) => sheet.rows.map((row) => row.project_label))
    .filter((project): project is string => project !== null && project !== ""))];
  const visibleProjectNames = projectNames.filter((project) => project.toLocaleLowerCase("vi").includes(projectQuery.toLocaleLowerCase("vi")));

  useEffect(() => {
    if (selectedSnapshotId !== null) {
      document.getElementById(`sheet-detail-${selectedSnapshotId}`)?.focus();
    } else {
      previewHeading.current?.focus();
    }
  }, [workbook.workbook_id, selectedSnapshotId]);

  useEffect(() => {
    setSelectedProject(null);
    setProjectQuery("");
    setSheetQuery("");
    setSheetFilter("all");
  }, [workbook.workbook_id]);

  function openWorkbookSheet(snapshotId: number) {
    if (!sheets.some((sheet) => sheet.snapshot_id === snapshotId)) return;
    setSelectedProject(null);
    setSheetQuery("");
    setSheetFilter("all");
    if (selectedSnapshotId === snapshotId) {
      requestAnimationFrame(() => document.getElementById(`sheet-detail-${snapshotId}`)?.focus());
    }
    onSelectSheet(snapshotId);
  }

  return (
    <section className="workbook-preview" aria-labelledby="preview-title">
      <header className="workbook-heading">
        <p className="eyebrow">Nguồn đã lưu · chưa chạy phân tích</p>
        <h2 id="preview-title" ref={previewHeading} tabIndex={-1}>{workbook.original_filename}</h2>
        <p>Chọn một sheet để hiểu tình trạng nguồn và mở đúng dữ liệu cần kiểm tra.</p>
      </header>

      {selectedProject !== null && (
        <button type="button" className="secondary-button workbook-scope-button" onClick={() => setSelectedProject(null)}>Xem nguồn workbook</button>
      )}

      {selectedProject !== null && (
        <ProjectComparison
          key={selectedProject}
          sheets={sheets}
          project={selectedProject}
          onChangeProject={() => setSelectedProject(null)}
        />
      )}

      {selectedProject !== null ? null : <>

      <section className="attention-summary" aria-label="Mục cần chú ý từ nguồn" aria-labelledby="attention-title">
        <h3 id="attention-title">Mục cần chú ý từ nguồn</h3>
        {attentionSheets.length === 0 ? (
          <p>Chưa ghi nhận mục cần chú ý từ dữ liệu nguồn. Đây không phải kết quả phân tích chất lượng.</p>
        ) : (
          <ul>
            {attentionSheets.map((sheet) => (
              <li key={sheet.snapshot_id} className="attention-item">
                <span><strong>{sheet.sheet_name}</strong> — {sheetAttentionReason(sheet)}{sheet.sheet_state === "draft_or_incomplete" && sheetAttentionReason(sheet) !== "Nháp hoặc chưa đầy đủ" ? ` · ${sheetStateLabel(sheet.sheet_state)}` : ""}</span>
                <button type="button" className="secondary-button" aria-label={`Đối chiếu nguồn ${sheet.sheet_name}`} onClick={() => openWorkbookSheet(sheet.snapshot_id)}>Xem nguồn</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {yearContextSheets.length > 0 && <section className="year-context" aria-label="Thông tin năm còn thiếu">
        <details>
          <summary>{yearContextSheets.length} sheet chưa rõ năm</summary>
          <p>Thông tin năm còn thiếu trong nguồn; đây không phải xung đột kỳ hay kết quả về chất lượng công việc.</p>
          <ul>{yearContextSheets.map((sheet) => <li key={sheet.snapshot_id}>{sheet.sheet_name} — {chronologyLabel(sheet.chronology_status)}</li>)}</ul>
          <details><summary>Vì sao?</summary><p>Thông tin nguồn hiện có chưa xác định được năm. Hãy đối chiếu workbook gốc; không tự suy ra năm còn thiếu.</p></details>
        </details>
      </section>}

      <section className="sheet-list-section" aria-labelledby="sheet-list-title">
        <h3 id="sheet-list-title">Các sheet trong nguồn</h3>
        <p>Giữ thứ tự Excel. Mở nguồn để xem căn cứ.</p>
        <label htmlFor={`sheet-search-${workbook.workbook_id}`}>Tìm tên sheet</label>
        <input id={`sheet-search-${workbook.workbook_id}`} type="search" value={sheetQuery} onChange={(event) => setSheetQuery(event.currentTarget.value)} placeholder="Nhập tên sheet" autoComplete="off" />
        {sheetQuery !== "" && <button type="button" className="secondary-button clear-sheet-search" aria-label="Xóa tìm kiếm tên sheet" onClick={() => setSheetQuery("")}>Xóa tìm kiếm</button>}
        <div className="sheet-filters" role="group" aria-label="Lọc sheet">
          {([ ["all", `Tất cả · ${sheets.length}`], ["attention", `Cần chú ý · ${attentionSheets.length}`], ["year", `Chưa rõ năm · ${yearContextSheets.length}`] ] as const).map(([filter, label]) => (
            <button type="button" key={filter} className="secondary-button" aria-pressed={sheetFilter === filter} onClick={() => setSheetFilter(filter)}>{label}</button>
          ))}
        </div>
        <p className="sheet-result-count" role="status" aria-label="Số sheet đang hiển thị" aria-live="polite">{visibleSheets.length} / {sheets.length} sheet</p>
        {visibleSheets.length === 0 ? <p className="sheet-empty">Không tìm thấy sheet. Thử tên khác hoặc xóa nội dung tìm kiếm.</p> : (
          <ol className="sheet-list" aria-label="Các sheet trong workbook">
            {visibleSheets.map((sheet) => {
              const reason = sheetAttentionReason(sheet);
              return <li key={sheet.snapshot_id} className={selectedSnapshotId === sheet.snapshot_id ? "sheet-row is-selected" : "sheet-row"} aria-current={selectedSnapshotId === sheet.snapshot_id ? "true" : undefined}>
                <div className="sheet-row-heading">
                  <h4>{sheet.sheet_name}{selectedSnapshotId === sheet.snapshot_id && <span className="selected-marker"> · Đang xem</span>}</h4>
                  <button type="button" className="secondary-button" aria-label={`Xem sheet ${sheet.sheet_name}`} onClick={() => openWorkbookSheet(sheet.snapshot_id)}>Xem nguồn</button>
                </div>
                <p className={reason ? "sheet-reason" : sheet.chronology_status === "year_unknown" ? "sheet-year-context" : "sheet-reason"}>{reason ?? (sheet.chronology_status === "year_unknown" ? "Chưa rõ năm" : "Không có mục nguồn cần chú ý được ghi nhận")}{sheet.sheet_state === "draft_or_incomplete" && reason !== "Nháp hoặc chưa đầy đủ" ? ` · ${sheetStateLabel(sheet.sheet_state)}` : ""}</p>
                <details className="sheet-metadata"><summary>Thông tin sheet</summary>
                  <dl><dt>Kỳ báo cáo</dt><dd>{periodLabel(sheet.reporting_period)}</dd><dt>Số dòng</dt><dd>{sheet.row_count}</dd>
                    <dt>Thiết lập cột</dt><dd>{mappingLabel(sheet.mapping_status)}</dd><dt>Nội dung nguồn</dt><dd>{sheetStateLabel(sheet.sheet_state)}</dd>
                    <dt>Thông tin kỳ</dt><dd>{chronologyLabel(sheet.chronology_status)}</dd><dt>Cảnh báo nguồn</dt><dd>{sheet.warnings.length} cảnh báo nguồn</dd>
                    <dt>Mã gốc</dt><dd><code>{sheet.mapping_status} · {sheet.sheet_state} · {sheet.chronology_status}</code></dd>
                    {sheet.mapping_version && <><dt>Phiên bản mapping</dt><dd>{sheet.template_id ?? "—"}@{sheet.mapping_version}</dd></>}
                    {sheet.mapping_source_reference && <><dt>Nguồn mapping</dt><dd>{sheet.mapping_source_reference}</dd></>}
                    <dt>Cảnh báo nguyên văn</dt><dd><ul>{sheet.warnings.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul></dd>
                  </dl>
                </details>
                {sheet.mapping_status === "mapping_required" && <a className="sheet-mapping-link" href={`/workbooks/${workbook.workbook_id}?return_app=1&sheet=${sheet.snapshot_id}&tool=mapping#mapping-${sheet.snapshot_id}`} aria-label={`Thiết lập cột cho sheet ${sheet.sheet_name}`}>Thiết lập cột</a>}
              </li>;
            })}
          </ol>
        )}
      </section>

      {selectedSheet ? <SheetDetails sheet={selectedSheet} /> : <p className="selection-hint">Chọn một sheet để xem chi tiết bên dưới.</p>}

      {projectNames.length > 0 && <section className="project-picker" aria-label="Chọn một dự án trong workbook">
        <h3>Bạn muốn xem dự án nào?</h3>
        <p>Chỉ hiện tên dự án đã đọc từ các sheet có thể nhận diện; tên được giữ nguyên như trong nguồn.</p>
        <label htmlFor={`project-search-${workbook.workbook_id}`}>Tìm dự án trong workbook</label>
        <input id={`project-search-${workbook.workbook_id}`} type="search" value={projectQuery} onChange={(event) => setProjectQuery(event.currentTarget.value)} placeholder="Gõ tên dự án…" autoComplete="off" />
        <div className="project-list">
          {visibleProjectNames.map((project) => <button type="button" key={project} aria-label={`Chọn dự án ${project}${project.endsWith(" ") ? " (khoảng trắng cuối)" : ""}`} onClick={() => setSelectedProject(project)}><span>{project}{project.endsWith(" ") && <small> · khoảng trắng cuối</small>}</span><span aria-hidden="true">Xem dự án →</span></button>)}
          {visibleProjectNames.length === 0 && <p>Không tìm thấy dự án khớp tên. Thử từ khóa khác.</p>}
        </div>
        {sheets.some((sheet) => sheet.mapping_status === "mapping_required") && <p className="mapping-project-notice">Có sheet cần thiết lập cột; chưa thể xác định dự án nào có trong sheet đó. Điều này không có nghĩa các dự án không có dữ liệu.</p>}
      </section>}

      <details className="technical-details">
        <summary>Thông tin kỹ thuật</summary>
        <dl><dt>Số workbook</dt><dd>{workbook.workbook_id}</dd><dt>SHA-256</dt><dd><code>{workbook.sha256}</code></dd>
          <dt>Thời điểm nhập</dt><dd><time dateTime={workbook.imported_at_utc}>{workbook.imported_at_utc}</time></dd></dl>
      </details>
      <details className="advanced-tools">
        <summary>Công cụ nâng cao</summary>
        <p>Alias là tùy chọn, không bắt buộc để xem nguồn. Chỉ xác nhận khi có nguồn quyết định phù hợp.</p>
        <a href={aliasUrl}>Mở công cụ alias</a>
      </details>
      </>}
    </section>
  );
}
