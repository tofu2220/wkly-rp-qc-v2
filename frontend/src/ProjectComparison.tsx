import { useState } from "react";
import { SheetDetails } from "./SheetDetails";
import type { WorkbookSheet } from "./WorkbookPreview";
import { periodLabel } from "./sourcePresentation";

export function ProjectComparison({
  sheets,
  project,
  onChangeProject,
}: {
  sheets: WorkbookSheet[];
  project: string;
  onChangeProject: () => void;
}) {
  const matchingSheets = sheets.filter((sheet) => sheet.mapping_status !== "mapping_required"
    && sheet.rows.some((row) => row.project_label === project));
  const [selectedIds, setSelectedIds] = useState(() => new Set(matchingSheets.map((sheet) => sheet.snapshot_id)));
  const selectedSheets = matchingSheets.filter((sheet) => selectedIds.has(sheet.snapshot_id));

  function toggleSheet(snapshotId: number) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(snapshotId)) next.delete(snapshotId);
      else next.add(snapshotId);
      return next;
    });
  }

  return (
    <section className="project-comparison" aria-label={`Dự án ${project}`}>
      <header className="project-selection-heading">
        <div><p className="eyebrow">Chỉ đang xem dự án</p><h3>{project}</h3>
          <p>{matchingSheets.length} kỳ có dữ liệu · Không hiển thị dự án khác</p></div>
        <button type="button" className="secondary-button" onClick={onChangeProject}>Đổi dự án</button>
      </header>
      {sheets.some((sheet) => sheet.mapping_status === "mapping_required") && (
        <p className="mapping-project-notice">Có sheet cần thiết lập cột; chưa thể xác định dự án nào có trong sheet đó. Điều này không có nghĩa các dự án không có dữ liệu.</p>
      )}
      <div className="period-picker" aria-label="Chọn các kỳ để đối chiếu">
        {matchingSheets.map((sheet) => {
          const selected = selectedIds.has(sheet.snapshot_id);
          return <button key={sheet.snapshot_id} type="button" aria-pressed={selected} onClick={() => toggleSheet(sheet.snapshot_id)}>
            {selected ? "Bỏ chọn kỳ" : "Chọn kỳ"} {sheet.sheet_name} <span className="period-label">({periodLabel(sheet.reporting_period)})</span>
          </button>;
        })}
      </div>
      <p className="comparison-count">Đang xem {selectedSheets.length} / {matchingSheets.length} kỳ</p>
      {selectedSheets.length === 0
        ? <p className="selection-hint">Chưa chọn kỳ nào. Dữ liệu vẫn được giữ; chọn lại sheet ở phía trên.</p>
        : <div className="comparison-grid" role="region" aria-label="Đối chiếu các kỳ đã chọn">
          {selectedSheets.map((sheet) => {
            const rows = sheet.rows.filter((row) => row.project_label === project);
            return <div className="comparison-period" key={sheet.snapshot_id}>
              <h4>{sheet.sheet_name}</h4>
              <p className="eyebrow">Kỳ báo cáo · {periodLabel(sheet.reporting_period)}</p>
              <SheetDetails sheet={{ ...sheet, rows, row_count: rows.length }} sourceDisclosureOpen />
            </div>;
          })}
        </div>}
    </section>
  );
}
