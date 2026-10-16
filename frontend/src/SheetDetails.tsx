import type { WorkbookSheet, SourceRow } from "./WorkbookPreview";
import { chronologyLabel, displaySourceValue, mappingLabel, periodLabel, sheetStateLabel, warningExplanation } from "./sourcePresentation";

const FIELDS = [
  ["project_label", "Dự án"], ["role_label", "Vai trò"], ["pic", "Người phụ trách"],
  ["actual", "Kết quả tuần"], ["next_plan", "Kế hoạch tuần"], ["issue_risk", "Vấn đề / rủi ro"],
] as const;

function sourceCell(row: SourceRow, field: string, label: string) {
  const value = row[field as keyof SourceRow] as string | null;
  const reference = row.source_refs?.[field];
  return (
    <td key={field} data-field={field}>
      <div className="source-value">{value === null ? <span title="Không có giá trị">—</span> : value === "" ? <span>Chuỗi rỗng</span> : value}</div>
      {reference && <small className="cell-reference">{label}: ô {reference}</small>}
    </td>
  );
}

export function SheetDetails({ sheet }: { sheet: WorkbookSheet }) {
  return (
    <section className="selected-sheet-detail" data-testid="selected-sheet-detail" aria-labelledby={`sheet-detail-${sheet.snapshot_id}`}>
      <header className="detail-heading">
        <p className="eyebrow">Chi tiết nguồn đã chọn</p>
        <h3 id={`sheet-detail-${sheet.snapshot_id}`} tabIndex={-1}>Chi tiết sheet: {sheet.sheet_name}</h3>
      </header>

      <dl className="status-grid">
        <div><dt>Kỳ báo cáo</dt><dd>{periodLabel(sheet.reporting_period)}</dd></div>
        <div><dt>Thiết lập cột</dt><dd>{mappingLabel(sheet.mapping_status)}</dd></div>
        <div><dt>Nội dung nguồn</dt><dd>{sheetStateLabel(sheet.sheet_state)}</dd></div>
        <div><dt>Thông tin kỳ</dt><dd>{chronologyLabel(sheet.chronology_status)}</dd></div>
      </dl>

      {sheet.row_count === 0 && <p className="no-source-rows">{sheet.mapping_status === "mapping_required"
        ? "Cần thiết lập cột trước khi đọc dòng nguồn; số 0 không có nghĩa sheet Excel trống."
        : "Chưa có dòng nguồn đã đọc. Kiểm tra sheet gốc và các lưu ý nguồn."}</p>}

      {sheet.reporting_period?.conflicts && sheet.reporting_period.conflicts.length > 0 && (
        <section className="period-conflicts" aria-labelledby={`period-conflicts-${sheet.snapshot_id}`}>
          <h4 id={`period-conflicts-${sheet.snapshot_id}`}>Thông tin kỳ cần đối chiếu</h4>
          <ul>{sheet.reporting_period.conflicts.map((conflict, index) => <li key={`${index}-${conflict}`}>{conflict}</li>)}</ul>
        </section>
      )}

      <section aria-labelledby={`warnings-${sheet.snapshot_id}`}>
        <h4 id={`warnings-${sheet.snapshot_id}`}>Cảnh báo nguồn ({sheet.warnings.length})</h4>
        {sheet.warnings.length === 0 ? <p>0 cảnh báo nguồn. Đây không phải kết quả phân tích chất lượng.</p> : (
          <ul className="warning-list">
            {sheet.warnings.map((warning, index) => {
              const explanation = warningExplanation(warning);
              return <li key={`${index}-${warning}`}>
                <h5>{explanation.title}</h5>
                <p>{explanation.action}</p>
                <details><summary>Cảnh báo gốc</summary><pre>{warning}</pre></details>
              </li>;
            })}
          </ul>
        )}
      </section>

      {Object.keys(sheet.mapping).length > 0 && (
        <section className="mapping-preview" aria-labelledby={`mapping-preview-${sheet.snapshot_id}`}>
          <h4 id={`mapping-preview-${sheet.snapshot_id}`}>Cột được dùng để đọc nguồn</h4>
          <div className="table-scroll" role="region" aria-label="Bản xem trước thiết lập cột" tabIndex={0}>
            <table><caption>Tiêu đề và địa chỉ ô gốc</caption><thead><tr><th scope="col">Trường</th><th scope="col">Tiêu đề trong Excel</th><th scope="col">Ô tiêu đề</th><th scope="col">Cột</th></tr></thead>
              <tbody>{Object.entries(sheet.mapping).map(([field, mapping]) => <tr key={field}>
                <th scope="row">{fieldLabel(field)}</th><td>{mapping.original_header_text === null ? "Không có giá trị" : String(mapping.original_header_text ?? "Chuỗi rỗng")}</td>
                <td>{mapping.header_cell_address ?? "Chưa có địa chỉ ô"}</td><td>{mapping.column_letter ?? "Chưa thiết lập"}</td>
              </tr>)}</tbody>
            </table>
          </div>
          {sheet.mapping_version && <p>Phiên bản mapping: {sheet.template_id ?? "—"}@{sheet.mapping_version}</p>}
          {sheet.mapping_source_reference && <p>Nguồn quyết định mapping: {sheet.mapping_source_reference}</p>}
        </section>
      )}

      <details className="source-disclosure">
        <summary>Dòng nguồn và vị trí ô</summary>
        {sheet.rows.length === 0 ? (
          <p>Chưa có dòng đã đọc để hiển thị trong phần này.</p>
        ) : (
          <div className="table-scroll source-table-scroll" role="region" aria-label={`Dữ liệu nguồn gốc của ${sheet.sheet_name}`} tabIndex={0}>
            <table className="source-table"><caption>Dữ liệu nguồn gốc; giữ nguyên chữ, khoảng trắng và ký tự gốc</caption>
              <thead><tr><th scope="col">Dòng Excel</th>{FIELDS.map(([, label]) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
              <tbody>{sheet.rows.map((row) => <tr key={row.source_row}>
                <th scope="row">{row.source_row}</th>{FIELDS.map(([field, label]) => sourceCell(row, field, label))}
              </tr>)}</tbody>
            </table>
          </div>
        )}
        {sheet.rows.map((row) => <details className="provenance-details" key={`provenance-${row.source_row}`}>
          <summary>Chi tiết provenance dòng {row.source_row}</summary>
          <dl><dt>Tham chiếu ô</dt><dd><code>{JSON.stringify(row.source_refs)}</code></dd>
            <dt>Nguồn trường</dt><dd><code>{JSON.stringify(row.field_provenance)}</code></dd>
            <dt>Nhóm nguồn</dt><dd><code>{JSON.stringify(row.subgroup)}</code></dd>
            <dt>Lưu ý dòng</dt><dd>{row.review_flags.length ? row.review_flags.join(" · ") : "Không có"}</dd></dl>
        </details>)}
      </details>

      {sheet.mapping_status === "mapping_required" && sheet.cell_preview && (
        <details open className="raw-layout">
          <summary>Bố cục ô nguồn — bản xem trước giới hạn</summary>
          <p>Tối đa 20 dòng × 12 cột; mỗi ô hiển thị tối đa 240 ký tự. Phần còn lại xem trong workbook gốc bằng Excel.</p>
          <div className="table-scroll raw-table-scroll" role="region" aria-label={`Bố cục ô nguồn của ${sheet.sheet_name}`} tabIndex={0}>
            <table><caption>Bản xem trước ô nguồn; không phải toàn bộ nội dung</caption><thead><tr><th scope="col">Dòng</th>{Array.from(new Set(sheet.cell_preview.flatMap((row) => row.cells.map((cell) => cell.column)))).map((column) => <th scope="col" key={column}>{column}</th>)}</tr></thead>
              <tbody>{sheet.cell_preview.map((row) => <tr key={row.row}><th scope="row">{row.row}</th>{row.cells.map((cell) => <td key={cell.address}>
                <div className="source-value" data-address={cell.address}>{displaySourceValue(cell.value)}</div>
                {cell.truncated && <small>{cell.address}: cắt ngắn ở 240 ký tự</small>}
              </td>)}</tr>)}</tbody>
            </table>
          </div>
        </details>
      )}

      <details className="technical-details">
        <summary>Thông tin kỹ thuật của sheet</summary>
        <dl><dt>Mã sheet</dt><dd>{sheet.snapshot_id}</dd><dt>Mã gốc mapping</dt><dd><code>{sheet.mapping_status}</code></dd>
          <dt>Mã gốc nội dung</dt><dd><code>{sheet.sheet_state}</code></dd><dt>Mã gốc kỳ</dt><dd><code>{sheet.chronology_status}</code></dd>
          <dt>Số dòng nguồn</dt><dd>{sheet.row_count}</dd><dt>Số cảnh báo nguồn</dt><dd>{sheet.warnings.length}</dd></dl>
      </details>
    </section>
  );
}

function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    project: "Dự án", role: "Vai trò", pic: "Người phụ trách", actual: "Kết quả tuần",
    next_plan: "Kế hoạch tuần", issue_risk: "Vấn đề / rủi ro",
  };
  return labels[field] ?? field;
}
