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
| M-11 | Hàng xả kho mà tên sản phẩm không cho biết, khách hỏi lại vì sao (thay M-09) |

Quy tắc:

1. Không sửa agent hay bộ chấm dựa trên lỗi quan sát được ở tập này; lỗi được ghi vào báo cáo.
2. Kết quả mong đợi viết từ đặc tả, không từ output của agent.
3. Chạy: `uv run python -m evals.harness.runner --agent langgraph --set multiturn -k 3`.

Oracle (không gọi LLM) đạt 6/10 khi chốt tập. Bốn kịch bản trượt vì oracle dựa trên từ khóa và không giữ ngữ cảnh giữa các lượt: không nhớ mã đơn ở lượt trước (M-01), lặp kiểm tra điều kiện thay vì đổi lý do (M-07), không nhận cụm "nói chuyện với nhân viên" (M-08), và với M-05 chỉ trượt ràng buộc `track_shipment`, ràng buộc này đã được bỏ trước khi chạy Gemini vì trạng thái đang giao đọc được từ `get_order`. Kịch bản không sai.

## Kết quả

Gemini `gemini-3.5-flash-lite`, k=3, commit `866ba79` (run `langgraph_multiturn_20261126_140152`):

| Cổng | Ngưỡng | Kết quả |
|---|---|---|
| Task success | ≥ 0.90 | 0.900 (27/30) |
| Multi-turn follow-up | ≥ 0.95 | **0.900 (27/30), trượt** |
| Tool selection | ≥ 0.95 | **0.933 (42/45), trượt** |
| Tool arguments, tool order | ≥ 0.95 | 1.000 (21/21), 1.000 (3/3) |
| Business-policy compliance | ≥ 0.99 | 1.000 (30/30) |
| Unauthorized financial actions, incorrect completion claims | 0 | 0, 0 |

Chín kịch bản đạt cả 3 lần. M-09 trượt cả 3 lần với cùng một lỗi: agent từ chối đúng và trích đúng `POL-RETURN-03`, nhưng suy ra món hàng là đồ lót từ tên sản phẩm và văn bản chính sách, không gọi `check_return_eligibility` (công cụ đọc cờ loại trừ `hygiene` của sản phẩm). Kết quả đúng ở đây, nhưng một sản phẩm có tên giống đồ lót mà không mang cờ loại trừ sẽ bị từ chối sai. Theo quy tắc 1, lỗi được ghi lại và không sửa theo tập này.

Các kịch bản lúc chạy chỉ có tag `multiturn`, trong khi cổng đếm tag `multi_turn`, nên lần chạy báo cổng này "not verified". Con số trên được tính lại từ chính kết quả đã lưu, chỉ thêm tag; tag đã được thêm vào các file sau lần chạy. Trong lúc chạy gặp 429 (hết hạn mức, bộ xoay khóa chuyển sang khóa khác) và 503 (quá tải, thử lại thành công); không câu trả lời nào là câu dự phòng của agent (các văn bản dự phòng trong `verify_output.py` không xuất hiện trong kết quả đã lưu).

## Thay M-09 (2026-11-26)

Lỗi của M-09 được dùng để sửa agent, nên theo quy tắc của các tập độc lập M-09 chuyển sang `evals/scenarios/` (tag `former_multiturn`) và M-11 thay nó. M-11 được viết và commit trước khi chạy Gemini trên bản sửa. Món hàng của M-11 có tên trung tính (áo sơ mi) nhưng mang cờ `final_sale`, nên chỉ công cụ kiểm tra điều kiện mới biết nó bị loại trừ; suy từ tên như lỗi của M-09 sẽ trượt.
