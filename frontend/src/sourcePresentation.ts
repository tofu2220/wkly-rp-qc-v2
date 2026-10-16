export type WarningExplanation = {
  title: string;
  action: string;
};

export function mappingLabel(status: string): string {
  switch (status) {
    case "recognized": return "Đã nhận diện";
    case "confirmed": return "Đã thiết lập cột";
    case "mapping_required": return "Cần thiết lập cột";
    default: return `Chưa có diễn giải (${status})`;
  }
}

export function sheetStateLabel(state: string): string {
  switch (state) {
    case "not_assessed": return "Chưa phân tích";
    case "draft_or_incomplete": return "Nháp hoặc chưa đầy đủ";
    case "mapping_required": return "Chưa đọc được theo cột";
    default: return `Chưa có diễn giải (${state})`;
  }
}

export function chronologyLabel(status: string): string {
  switch (status) {
    case "unknown": return "Chưa xác định kỳ";
    case "conflict": return "Thông tin kỳ mâu thuẫn";
    case "year_unknown": return "Chưa rõ năm";
    case "certain": return "Đã xác định kỳ và năm";
    default: return `Chưa có diễn giải (${status})`;
  }
}

export function warningExplanation(warning: string): WarningExplanation {
  switch (warning) {
    case "Unknown worksheet layout; mapping is required":
      return { title: "Chưa nhận diện được bố cục sheet", action: "Kiểm tra bố cục ô nguồn và thiết lập cột cho sheet này." };
    case "Project and role share a physical header; use the recorded hierarchy":
      return { title: "Cột dự án và vai trò dùng chung cấu trúc", action: "Kiểm tra cách các dòng tiêu đề phân cấp được ghi nhận trong nguồn." };
    case "Column D can contain a more-specific role without an independent header":
      return { title: "Cột D có thể chứa vai trò chi tiết hơn", action: "Đối chiếu cột D với nội dung vai trò trong workbook gốc." };
    case "Explicit flat mapping; blank project/role labels are not inferred":
      return { title: "Nhãn trống không được tự điền", action: "Kiểm tra nhãn dự án/vai trò tại từng dòng trong workbook gốc." };
    default:
      return { title: "Thông tin nguồn cần kiểm tra", action: "Xem nguyên văn cảnh báo và đối chiếu sheet gốc; chưa có diễn giải cụ thể." };
  }
}

export function periodLabel(period: { label?: string | null } | null): string {
  return period?.label || "Chưa xác định kỳ";
}

export function displaySourceValue(value: string | null): string {
  return value === null ? "—" : value === "" ? "Chuỗi rỗng" : value;
}
