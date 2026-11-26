# Tập nghiệm thu nhiều lượt (multiturn)

10 kịch bản `M-*`, viết từ đặc tả nghiệp vụ (`docs/business-spec.md`) ngày 2026-11-26 và commit **trước** lần chạy Gemini đầu tiên trên tập này. Tập acceptance (`A-*`) không có kịch bản nhiều lượt; tập phát triển có, nhưng đã dùng để sửa agent.

Mỗi kịch bản có 2–4 lượt khách, mỗi lượt một phần thông tin:

| Kịch bản | Điều được đo |
|---|---|
| M-01, M-06, M-10 | Gom thông tin qua nhiều lượt: đơn, món, lý do |
| M-02 | Hỏi chính sách rồi chuyển sang làm thủ tục |
| M-03 | Khách từ chối thẻ đề xuất: đơn giữ nguyên |
| M-04 | Khách sửa mã đơn sau khi đã có thẻ cho đơn sai |
| M-05 | "Hủy nó được không" chỉ đơn ở lượt trước; đơn đang giao không hủy được |
| M-07 | Đổi ý quá 7 ngày bị từ chối, lượt sau khách báo lỗi (còn trong 15 ngày) |
| M-08 | Bị từ chối rồi xin gặp nhân viên |
| M-09 | Hàng loại trừ khi đổi ý, khách hỏi lại vì sao |

Quy tắc:

1. Không sửa agent hay bộ chấm dựa trên lỗi quan sát được ở tập này; lỗi được ghi vào báo cáo.
2. Kết quả mong đợi viết từ đặc tả, không từ output của agent.
3. Chạy: `uv run python -m evals.harness.runner --agent langgraph --set multiturn -k 3`.

Oracle (không gọi LLM) đạt 6/10 khi chốt tập. Bốn kịch bản trượt vì oracle dựa trên từ khóa và không giữ ngữ cảnh giữa các lượt: không nhớ mã đơn ở lượt trước (M-01), lặp kiểm tra điều kiện thay vì đổi lý do (M-07), không nhận cụm "nói chuyện với nhân viên" (M-08), và với M-05 chỉ trượt ràng buộc `track_shipment`, ràng buộc này đã được bỏ trước khi chạy Gemini vì trạng thái đang giao đọc được từ `get_order`. Kịch bản không sai.
