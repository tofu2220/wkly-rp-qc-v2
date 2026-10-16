import { useEffect, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";

type SourceRow = {
  actual: string | null;
  source_row: number;
};

type Sheet = {
  sheet_name: string;
  mapping_status: string;
  sheet_state: string;
  row_count: number;
  rows: SourceRow[];
};

type WorkbookPreview = {
  workbook_id: number;
  original_filename: string;
  sha256: string;
  sheets: Sheet[];
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
  const [preview, setPreview] = useState<WorkbookPreview | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function openWorkbook(workbookId: number, savedMessage = "") {
    const response = await fetch(`/api/workbooks/${workbookId}`);
    if (!response.ok) throw new Error("Không thể tải bản xem trước đã lưu. Hãy thử mở lại workbook.");
    const nextPreview = (await readJson(response)) as WorkbookPreview;
    setPreview(nextPreview);
    setMessage(savedMessage || "Đã lưu. Workbook chưa được phân tích.");
  }

  useEffect(() => {
    const match = window.location.pathname.match(/^\/app\/workbooks\/(\d+)$/);
    if (!match) return;
    void openWorkbook(Number(match[1])).catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Không thể mở workbook đã lưu.");
    });
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!file) {
      setMessage("Vui lòng chọn tệp Excel trước khi tải lên.");
      return;
    }
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
      const target = new URL(imported.preview_url, window.location.origin);
      if (target.origin !== window.location.origin || target.pathname !== `/app/workbooks/${imported.workbook_id}`) {
        throw new Error("Máy chủ trả về đường dẫn workbook không hợp lệ.");
      }
      await openWorkbook(imported.workbook_id, imported.duplicate
        ? "Tệp này đã có, đang mở bản đã lưu."
        : "Đã lưu. Workbook chưa được phân tích.");
      window.history.pushState({}, "", target.pathname);
      setFile(null);
      form.reset();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không thể nhập workbook. Hãy thử lại.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>Mở workbook báo cáo</h1>
      <p>Tải workbook để xem trạng thái nguồn. Việc mở bản xem trước không chạy phân tích.</p>
      <form onSubmit={submit}>
        <label htmlFor="workbook-file">Chọn tệp Excel</label>
        <input
          id="workbook-file"
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)}
        />
        <button type="submit" disabled={busy}>{busy ? "Đang tải…" : "Tải và xem trước"}</button>
      </form>
      <p role="status" aria-live="polite">{message}</p>
      {preview && (
        <section aria-labelledby="preview-title">
          <h2 id="preview-title">{preview.original_filename}</h2>
          <p>Đã lưu, chưa phân tích. Số workbook: {preview.workbook_id}</p>
          <p>SHA-256: {preview.sha256}</p>
          <h3>Các sheet</h3>
          <ul>
            {preview.sheets.map((sheet) => (
              <li key={sheet.sheet_name}>
                <h4>{sheet.sheet_name}</h4>
                <p>{sheet.mapping_status} · {sheet.sheet_state} · {sheet.row_count} dòng</p>
                {sheet.rows.map((row) => (
                  <p key={row.source_row}>{row.actual}</p>
                ))}
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
