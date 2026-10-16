import type { MouseEvent } from "react";
import { useEffect, useRef } from "react";
import { SheetDetails } from "./SheetDetails";
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

export function WorkbookPreview({ workbook, selectedSnapshotId, onSelectSheet }: Props) {
  const previewHeading = useRef<HTMLHeadingElement>(null);
  const sheets = [...workbook.sheets].sort((a, b) => a.sheet_index - b.sheet_index);
  const selectedSheet = sheets.find((sheet) => sheet.snapshot_id === selectedSnapshotId) ?? null;
  const attentionSheets = sheets.filter((sheet) => sheet.mapping_status === "mapping_required" || sheet.warnings.length > 0);
  const aliasUrl = selectedSnapshotId === null
    ? `/workbooks/${workbook.workbook_id}`
    : `/workbooks/${workbook.workbook_id}?return_app=1&sheet=${selectedSnapshotId}&tool=aliases#aliases`;

  useEffect(() => {
    if (selectedSnapshotId !== null) {
      document.getElementById(`sheet-detail-${selectedSnapshotId}`)?.focus();
    } else {
      previewHeading.current?.focus();
    }
  }, [workbook.workbook_id, selectedSnapshotId]);

  function selectLink(event: MouseEvent<HTMLAnchorElement>, snapshotId: number) {
    event.preventDefault();
    onSelectSheet(snapshotId);
  }

  return (
    <section className="workbook-preview" aria-labelledby="preview-title">
      <header className="workbook-heading">
        <p className="eyebrow">Nguồn đã lưu · chưa chạy phân tích</p>
        <h2 id="preview-title" ref={previewHeading} tabIndex={-1}>{workbook.original_filename}</h2>
        <p>Chọn một sheet để hiểu tình trạng nguồn và mở đúng dữ liệu cần kiểm tra.</p>
      </header>

      <section className="attention-summary" aria-labelledby="attention-title">
        <h3 id="attention-title">Mục cần chú ý</h3>
        {attentionSheets.length === 0 ? (
          <p>Chưa ghi nhận mục cần chú ý từ dữ liệu nguồn. Đây không phải kết quả phân tích chất lượng.</p>
        ) : (
          <ul>
            {attentionSheets.map((sheet) => (
              <li key={sheet.snapshot_id}>
                <a href={`?sheet=${sheet.snapshot_id}`} onClick={(event) => selectLink(event, sheet.snapshot_id)}>
                  {sheet.sheet_name} — {sheet.mapping_status === "mapping_required" ? "cần thiết lập cột" : `${sheet.warnings.length} lưu ý nguồn`}
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="table-scroll" role="region" aria-label="Tổng quan các sheet" tabIndex={0}>
        <table className="sheet-summary">
          <caption>Tổng quan các sheet theo thứ tự trong Excel</caption>
          <thead><tr>
            <th scope="col">Sheet</th><th scope="col">Kỳ</th><th scope="col">Số dòng</th>
            <th scope="col">Thiết lập cột</th><th scope="col">Nội dung nguồn</th><th scope="col">Thông tin kỳ</th>
            <th scope="col">Cảnh báo nguồn</th><th scope="col">Thao tác</th>
          </tr></thead>
          <tbody>
            {sheets.map((sheet) => (
              <tr key={sheet.snapshot_id} className={selectedSnapshotId === sheet.snapshot_id ? "is-selected" : undefined} aria-current={selectedSnapshotId === sheet.snapshot_id ? "true" : undefined}>
                <th scope="row">{sheet.sheet_name}{selectedSnapshotId === sheet.snapshot_id && <span className="selected-marker"> · Đang xem</span>}</th>
                <td>{periodLabel(sheet.reporting_period)}</td>
                <td>{sheet.row_count}</td>
                <td><span className="status-label">{mappingLabel(sheet.mapping_status)}</span></td>
                <td><span className="status-label">{sheetStateLabel(sheet.sheet_state)}</span></td>
                <td><span className="status-label">{chronologyLabel(sheet.chronology_status)}</span></td>
                <td>{sheet.warnings.length} cảnh báo nguồn</td>
                <td className="sheet-actions">
                  <button type="button" onClick={() => onSelectSheet(sheet.snapshot_id)} aria-label={`Xem sheet ${sheet.sheet_name}`}>Xem sheet</button>
                  {sheet.mapping_status === "mapping_required" && <a href={`/workbooks/${workbook.workbook_id}?return_app=1&sheet=${sheet.snapshot_id}&tool=mapping#mapping-${sheet.snapshot_id}`} aria-label={`Thiết lập cột cho sheet ${sheet.sheet_name}`}>Thiết lập cột</a>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedSheet ? <SheetDetails sheet={selectedSheet} /> : <p className="selection-hint">Chọn một sheet trong bảng để xem chi tiết bên dưới.</p>}

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
    </section>
  );
}
