import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { WorkbookPreview, type WorkbookPreviewData } from "./WorkbookPreview";

type WorkbookVersion = {
  workbook_id: number;
  sha256: string;
  original_filename: string;
  imported_at_utc: string;
  sheet_count: number;
  mapping_required_count: number;
  warning_count: number;
  preview_url: string;
};

type ImportResult = {
  workbook_id: number;
  duplicate: boolean;
  preview_url: string;
};

async function readJson(response: Response) {
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("Máy chủ trả về phản hồi không phải JSON; tệp chưa được xác nhận là đã lưu. Hãy thử lại.");
  }
  return response.json();
}

function App() {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<WorkbookPreviewData | null>(null);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<number | null>(null);
  const [catalog, setCatalog] = useState<WorkbookVersion[]>([]);
  const [catalogError, setCatalogError] = useState("");
  const [message, setMessage] = useState("");
  const [retryWorkbookId, setRetryWorkbookId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const requestGeneration = useRef(0);
  const catalogGeneration = useRef(0);
  const previewRequest = useRef<AbortController | null>(null);
  const uploadInFlight = useRef(false);

  async function loadCatalog() {
    const generation = ++catalogGeneration.current;
    setCatalogError("");
    try {
      const response = await fetch("/api/workbooks");
      if (!response.ok) throw new Error("Không tải được danh sách workbook.");
      const versions = (await readJson(response)) as WorkbookVersion[];
      if (generation === catalogGeneration.current) setCatalog(versions);
    } catch (error) {
      if (generation === catalogGeneration.current) {
        setCatalogError(error instanceof Error ? error.message : "Không tải được danh sách workbook.");
      }
    }
  }

  async function openWorkbook(workbookId: number, savedMessage = "", navigate = true): Promise<boolean> {
    const generation = ++requestGeneration.current;
    previewRequest.current?.abort();
    const controller = new AbortController();
    previewRequest.current = controller;
    setPreview(null);
    setSelectedSnapshotId(null);
    setRetryWorkbookId(null);
    setMessage("Đang mở bản xem trước…");
    try {
      const response = await fetch(`/api/workbooks/${workbookId}`, { signal: controller.signal });
      if (!response.ok) throw new Error("Không thể tải bản xem trước đã lưu. Hãy thử mở lại workbook.");
      const nextPreview = (await readJson(response)) as WorkbookPreviewData;
      if (nextPreview.workbook_id !== workbookId) throw new Error("Máy chủ trả về workbook khác với bản đã chọn.");
      if (generation !== requestGeneration.current) return false;
      setRetryWorkbookId(null);
      setPreview(nextPreview);
      const pathMatch = window.location.pathname.match(/^\/app\/workbooks\/(\d+)$/);
      const requestedSheet = pathMatch && Number(pathMatch[1]) === workbookId
        ? new URLSearchParams(window.location.search).get("sheet")
        : null;
      const requestedSnapshotId = requestedSheet && /^\d+$/.test(requestedSheet) ? Number(requestedSheet) : null;
      const requestedSheetExists = requestedSnapshotId !== null
        && nextPreview.sheets.some((sheet) => sheet.snapshot_id === requestedSnapshotId);
      const noticeMatchesSelection = nextPreview.tool_notice !== undefined
        && nextPreview.tool_notice.snapshot_id === requestedSnapshotId
        && requestedSheetExists;
      setSelectedSnapshotId(requestedSheetExists ? requestedSnapshotId : null);
      setMessage(requestedSheet && !requestedSheetExists
        ? "Sheet được yêu cầu không còn trong phiên bản này. Chọn sheet từ bảng tổng quan."
        : noticeMatchesSelection
          ? nextPreview.tool_notice?.operation === "mapping" ? "Đã cập nhật thiết lập cột." : "Đã cập nhật alias."
          : savedMessage || "Đang xem nguồn đã lưu. Mở bản xem trước không chạy phân tích.");
      if (requestedSheet && !requestedSheetExists) {
        const url = new URL(window.location.href);
        url.searchParams.delete("sheet");
        window.history.replaceState({}, "", `${url.pathname}${url.search}`);
      }
      if (navigate) window.history.pushState({}, "", `/app/workbooks/${workbookId}`);
      return true;
    } catch (error) {
      if (generation !== requestGeneration.current) return false;
      setRetryWorkbookId(workbookId);
      setMessage("Không thể tải nguồn đã lưu. Bấm Thử tải lại để kiểm tra dữ liệu.");
      return false;
    }
  }

  useEffect(() => {
    void loadCatalog();
  }, []);

  useEffect(() => {
    const restoreFromLocation = () => {
      const match = window.location.pathname.match(/^\/app\/workbooks\/(\d+)$/);
      if (match) {
        void openWorkbook(Number(match[1]), "", false);
      } else {
        requestGeneration.current += 1;
        previewRequest.current?.abort();
        setPreview(null);
        setSelectedSnapshotId(null);
        setRetryWorkbookId(null);
        setMessage("");
      }
    };
    window.addEventListener("popstate", restoreFromLocation);
    restoreFromLocation();
    return () => {
      window.removeEventListener("popstate", restoreFromLocation);
      previewRequest.current?.abort();
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (uploadInFlight.current) return;
    const form = event.currentTarget;
    if (!file) {
      setMessage("Vui lòng chọn tệp Excel trước khi tải lên.");
      return;
    }
    uploadInFlight.current = true;
    const generation = ++requestGeneration.current;
    setBusy(true);
    setMessage("Đang tải và lưu workbook…");
    try {
      const body = new FormData();
      body.append("workbook", file);
      const response = await fetch("/api/workbooks", { method: "POST", body });
      if (![200, 201].includes(response.status)) {
        const error = await readJson(response).catch(() => null);
        throw new Error(typeof error?.detail === "string"
          ? error.detail
          : "Không thể nhập workbook. Hãy kiểm tra tệp rồi thử lại.");
      }
      const imported = (await readJson(response)) as ImportResult;
      void loadCatalog();
      if (generation !== requestGeneration.current) return;
      const target = new URL(imported.preview_url, window.location.origin);
      if (target.origin !== window.location.origin || target.pathname !== `/app/workbooks/${imported.workbook_id}`) {
        throw new Error("Máy chủ trả về đường dẫn workbook không hợp lệ.");
      }
      const opened = await openWorkbook(imported.workbook_id, imported.duplicate
        ? "Tệp này đã có, đang mở bản đã lưu"
        : "Đã lưu workbook, chưa chạy phân tích.", false);
      if (!opened) return;
      window.history.pushState({}, "", target.pathname);
      await loadCatalog();
      setFile(null);
      form.reset();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không thể nhập workbook. Hãy thử lại.");
    } finally {
      uploadInFlight.current = false;
      setBusy(false);
    }
  }

  function selectSheet(snapshotId: number) {
    setSelectedSnapshotId(snapshotId);
    const url = new URL(window.location.href);
    url.searchParams.set("sheet", String(snapshotId));
    window.history.pushState({}, "", `${url.pathname}${url.search}`);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Không gian workbook">
        <a className="brand" href="/app/" aria-label="tuần · Không gian rà soát báo cáo">
          <span className="brand-icon" aria-hidden="true">≋</span>
          <span>tuần<small>Không gian rà soát báo cáo</small></span>
        </a>
        <p className="eyebrow sidebar-title">Một báo cáo, từng bước rõ ràng</p>
        <nav className="steps" aria-label="Tiến trình">
          <a className="step active" href="/app/" aria-current="step">
            <span className="step-number">1</span><span>Tải báo cáo</span><span className="step-state">Đang thực hiện</span>
          </a>
          {[
            ["2", "Chọn hai kỳ"], ["3", "Xem kết quả"], ["4", "Review nhận xét"], ["5", "Xuất kết quả"],
          ].map(([number, label]) => (
            <button className="step unavailable" type="button" disabled key={number}>
              <span className="step-number">{number}</span><span>{label}</span><span className="step-state">Chưa khả dụng</span>
            </button>
          ))}
        </nav>
        <div className="side-note"><span className="local-dot" />Không gian local<br />Nguồn được lưu trên thiết bị này.<br />Không gửi dữ liệu tới AI mới.</div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <span>Không gian làm việc <span aria-hidden="true"> / </span> <strong>{preview?.original_filename ?? "Báo cáo tuần"}</strong></span>
          <span className="local-badge"><span className="local-dot" />Local · offline</span>
        </header>
        <main className="container">
          <section className="page-intro" aria-labelledby="page-title">
            <div className="eyebrow">Bước 01 / 05</div>
            <h1 id="page-title">Bắt đầu từ báo cáo của bạn.</h1>
            <p>Chọn một workbook đã lưu hoặc tải lên để xem sheet cần chú ý. Tải lên chỉ lưu nguồn và mở bản xem trước; chưa chạy phân tích.</p>
          </section>

          <div className="upload-grid">
            <form className="card upload-card" onSubmit={submit}>
              <div className="dropzone">
                <span className="upload-symbol" aria-hidden="true">↥</span>
                <h3>Chọn báo cáo Excel</h3>
                <label htmlFor="workbook-file">Chọn tệp Excel</label>
                <span className="hint">Tệp .xlsx, tối đa 32 MiB. Không cần ghi lại mã SHA.</span>
                <input
                  id="workbook-file"
                  type="file"
                  accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
                />
              </div>
              <div className="upload-actions">
                <button type="submit" disabled={busy}>{busy ? "Đang tải…" : "Tải và xem trước"}<span aria-hidden="true"> →</span></button>
                <span className="hint">Chỉ lưu nguồn, chưa chạy phân tích.</span>
              </div>
              <p className="step-note" role="status" aria-live="polite">{message}</p>
              {retryWorkbookId !== null && <button type="button" className="secondary-button" onClick={() => { void openWorkbook(retryWorkbookId, "", false); }}>Thử tải lại</button>}
            </form>

            <section className="card guidance-card" aria-labelledby="guidance-title">
              <p className="eyebrow">Bạn sẽ làm gì?</p>
              <h2 id="guidance-title">Ít thao tác. Rõ việc cần làm.</h2>
              <ol>
                <li><span>01</span>Chọn dự án và kỳ từ cùng một workbook.</li>
                <li><span>02</span>Xem tình trạng từng sheet và mở nguồn trích dẫn.</li>
                <li><span>03</span>Chỉ thiết lập cột khi sheet yêu cầu.</li>
              </ol>
              <p className="notice">Thông tin phiên bản và SHA-256 nằm trong phần kỹ thuật; không cần chép mã để tìm lại workbook.</p>
            </section>
          </div>

          <section className="catalog-card" aria-labelledby="catalog-title">
            <div className="section-heading">
              <div><p className="eyebrow">Nguồn đã lưu trên thiết bị này</p><h2 id="catalog-title">Workbook đã lưu</h2></div>
              {catalog.length > 0 && <span className="count-badge">{catalog.length} phiên bản</span>}
            </div>
            {catalogError && <p role="alert">{catalogError} <button className="text-button" type="button" onClick={() => { void loadCatalog(); }}>Thử tải lại</button></p>}
            {!catalogError && catalog.length === 0 && (
              <div className="empty-catalog"><h3>Chưa có workbook đã lưu</h3><p>Chọn tệp Excel rồi bấm Tải và xem trước để kiểm tra nguồn.</p></div>
            )}
            {catalog.length > 0 && (
              <div className="table-scroll" role="region" aria-label="Danh sách workbook đã lưu" tabIndex={0}>
                <table>
                  <caption>Mỗi dòng là một phiên bản workbook đã lưu</caption>
                  <thead>
                    <tr><th scope="col">Tệp</th><th scope="col">Ngày nhập (UTC)</th><th scope="col">Phiên bản</th><th scope="col">Số sheet</th><th scope="col">Tình trạng nguồn</th><th scope="col">Thao tác</th></tr>
                  </thead>
                  <tbody>
                    {catalog.map((version) => (
                      <tr key={version.workbook_id}>
                        <th scope="row">{version.original_filename}</th>
                        <td><time dateTime={version.imported_at_utc}>{formatImportedAt(version.imported_at_utc)}</time></td>
                        <td><code>{version.sha256.slice(0, 12)}</code><details><summary>Thông tin kỹ thuật</summary><p>SHA-256: {version.sha256}</p><p>Thời điểm nhập: {version.imported_at_utc}</p></details></td>
                        <td>{version.sheet_count}</td>
                        <td>{attentionText(version)}</td>
                        <td><a href={version.preview_url} aria-label={`Mở workbook ${version.original_filename}, phiên bản ${version.sha256.slice(0, 12)}`} onClick={(event) => { event.preventDefault(); void openWorkbook(version.workbook_id); }}>Mở workbook</a></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {preview && (preview.sheets.length === 0
            ? <section className="catalog-card" aria-labelledby="zero-sheet-title"><h2 id="zero-sheet-title">Workbook chưa có sheet để xem</h2><p>Phiên bản vẫn được lưu. Kiểm tra tệp gốc trong Excel hoặc quay lại danh sách workbook.</p></section>
            : <WorkbookPreview workbook={preview} selectedSnapshotId={selectedSnapshotId} onSelectSheet={selectSheet} />)}
        </main>
      </div>
    </div>
  );
}

function formatImportedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Chưa có thời điểm nhập";
  const parts = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23", timeZone: "UTC",
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("day")}/${part("month")}/${part("year")} ${part("hour")}:${part("minute")} UTC`;
}

function attentionText(version: WorkbookVersion): string {
  if (version.mapping_required_count > 0) return `${version.mapping_required_count} sheet cần thiết lập cột`;
  if (version.warning_count > 0) return `${version.warning_count} lưu ý nguồn`;
  return "Chưa ghi nhận mục cần chú ý từ dữ liệu nguồn; không phải kết quả phân tích chất lượng.";
}

createRoot(document.getElementById("root")!).render(<App />);
