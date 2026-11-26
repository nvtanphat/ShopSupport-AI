## Bằng chứng và giới hạn của kết luận

Phần này viết tay; mọi con số ở trên được sinh từ artifact trong `evals/results/`. Mã được đo: `8ab64d9` (lần sửa cuối của agent; các commit sau chỉ tách module và sửa tài liệu, hành vi không đổi).

**Nghiệm thu độc lập.** Tập `evals/scenarios_acceptance/` (16 kịch bản `A-*`, viết từ đặc tả và commit ở `f423f8a` trước lần sửa cuối), Gemini thật, k=3: **48/48, mọi cổng đạt**, 0 hành động tài chính trái phép, 0 khẳng định hoàn tất sai, 0/3 lộ dữ liệu khách khác. Tập không có kịch bản nhiều lượt nên cổng hội thoại đa lượt không có mẫu ở đây; khả năng này được đo ở tập phát triển (10/10) và tập `F-*` (18/18, k=3).

**Đường đi tới kết quả đó.** Mỗi tập độc lập, khi lỗi của nó đã được phân tích để sửa agent, không còn độc lập; nó được giữ lại làm kết quả lịch sử và việc nghiệm thu chuyển sang một tập mới viết trước lần sửa kế tiếp:

| Tập | Mã đo | Kết quả | Lỗi tìm thấy (đã sửa qua tập phát triển) |
|---|---|---|---|
| `H-*` (holdout đầu, nay trong `dev`) | trước phiên này | 38/48 (k=3) | đoán đơn khi khách có nhiều đơn; lý do mô tả bằng triệu chứng; câu trả lời đúng bị chặn vì thiếu bằng chứng của lượt trước; phiếu hỗ trợ trùng |
| `F-*` (`evals/scenarios_holdout/`) | `bade1bf` | 101/105 (k=3), trượt cổng hỏi lại 14/15 | hỏi dồn một đơn sau khi liệt kê; nhãn trạng thái lệch với thẻ đơn |
| `G-*` (`evals/scenarios_final/`) | `1b58e7f` | 59/63 (k=3), trượt cổng hỏi lại 14/15 | khách nói hàng chật và muốn trả thì agent tự chuyển sang đổi size. Lần trượt của cổng hỏi lại là do Gemini trả 503 (quá tải) ba lần liên tiếp, vẫn được tính là trượt |
| `A-*` (`evals/scenarios_acceptance/`) | `8ab64d9` | **48/48 (k=3)** | không |

**Tập phát triển** (`dev`, 67 kịch bản, k=1, mã `8ab64d9`): 67/67. Tập này gồm các kịch bản đã dùng để sửa agent, nên không chứng minh khả năng tổng quát.

**Ứng dụng thật** (`evals/e2e/live_acceptance.py`: HTTP API, PostgreSQL, Gemini, mô hình retrieval thật; mã `bade1bf`): 16/16. S12 trượt ở lần chạy liền vì dữ liệu demo được seed theo mốc 11/11 nên đơn đã quá hạn đổi ý vào ngày chạy (agent từ chối đúng); seed lại theo ngày chạy thì đạt. Playwright với API thật: 4/4.

**Chi phí suy luận (đo 2026-11-26):** từ hai lần chạy Gemini gần nhất, mỗi lần gọi model nhận khoảng 8.500 token đầu vào và trả khoảng 80 token; một kịch bản tốn 3,8 lần gọi (tập phát triển) đến 5,3 lần gọi (tập acceptance, nhiều lượt hơn), p50 9–12 giây. System prompt cùng 7 quy trình chiếm khoảng 5.700 token mỗi lần gọi; phần thay đổi theo lượt (giờ, đơn gần đây, đề xuất đang mở) đã nằm cuối prompt nên phần đầu cố định. Cache ngầm của Gemini hiếm khi trúng: hai lần gọi qua bộ xoay khóa đều `cache_read: 0`; ba lần gọi cùng một khóa với prompt giống hệt chỉ lần thứ ba trúng 2.035/5.661 token. Chưa đổi gì: ghim khóa theo hội thoại làm yếu việc xoay khóa khi bị giới hạn tốc độ, còn cache tường minh tính phí lưu theo giờ và phải tạo cho từng khóa, trong khi lợi ích đo được chưa rõ.

**Lỗi tìm thấy khi rà soát ngày 2026-11-26 và đã sửa:**
- Xác nhận đề xuất không khóa dòng: 6 request xác nhận cùng một đề xuất hủy đơn cùng lúc đều trả 200 và tạo 6 khoản hoàn tiền; một đề xuất trả hàng mở 6 phiếu. Nay xác nhận khóa đề xuất rồi khóa đơn hàng; test PostgreSQL (`test_action_confirm_concurrency_postgres.py`) cho đúng một 200, các request còn lại 409, một khoản hoàn tiền, kể cả khi hai đề xuất khác nhau cho cùng một đơn được xác nhận cùng lúc.
- Sau khi khách xác nhận, huy hiệu trạng thái đơn trên giao diện chỉ đổi khi tải lại trang. Nay xóa bộ nhớ đệm đơn hàng cũng tải lại các đơn đang hiển thị (test giao diện, và kiểm chứng trong trình duyệt thật bằng `scripts/portfolio_capture/run.py`).
- Hoàn tiền khi khách tự hủy đơn đã thanh toán không bao giờ được chi: sự kiện `refund.disburse` không có handler nên worker bỏ qua, khoản hoàn tiền nằm ở trạng thái `approved` và đơn vẫn `paid`. Nay có handler; test chạy dispatcher thật cho đến khi khoản hoàn tiền `succeeded` và đơn `refunded`.
- Nhân viên duyệt hủy đơn đang đóng gói thì đơn bị hủy nhưng không tạo khoản hoàn tiền. Nay dùng chung hàm hoàn tiền với luồng tự hủy; từ chối thì đơn và thanh toán giữ nguyên.
- Cổng thanh toán từ chối thì khoản hoàn tiền nằm `failed` mãi. Nay worker thử lại với độ trễ tăng dần, tối đa 3 lần, mỗi lần một khóa idempotency mới; hết lượt thì dừng.
- Dispatcher lặp không nghỉ khi một lô chỉ có sự kiện không cần xử lý (đánh dấu nhưng không commit nên lấy lại mãi).
- Test "fault injection" của outbox kiểm tra một worker thứ hai không được dùng (`infra/worker/outbox_worker.py`, xử lý loại sự kiện ứng dụng không phát ra). Đã xóa; các test outbox chạy trên `OutboxDispatcher` mà `python -m shopagent.worker` dùng.
- Bước vector BGE-M3 không đổi kết quả retrieval với 10 điều khoản (đo lại có và không có: mọi chỉ số giống hệt) nhưng tốn khoảng 1,1 GB RAM và 150 ms mỗi câu hỏi; nay tắt mặc định ([ADR-007](adr/007-D7-hybrid-rag.md)).

**Lỗi tìm thấy khi thử giao diện thật và đã sửa:** nút "Gặp nhân viên" trả 500 trên PostgreSQL do deadlock (thêm test PostgreSQL: 42 request lỗi trước khi sửa, 0 sau); timeout mạng không có thông báo bị coi là lỗi vĩnh viễn; mã điều khoản hiện kèm dấu backtick; ảnh Docker không build được.

**Retrieval:** bước vector BGE-M3 tắt mặc định từ 2026-11-26 ([ADR-007](adr/007-D7-hybrid-rag.md)). Với 10 điều khoản và 10 ứng viên cho reranker, bước chọn ứng viên chọn hết cả kho, nên BGE-M3 và RRF không làm thay đổi kết quả (kết quả tương đương BM25 làm cổng rồi reranker chấm cả kho); bước vector chỉ có tác dụng khi số điều khoản vượt số ứng viên. Mã retrieval không đổi; tập holdout retrieval nhỏ (28 câu có đáp án, 20 câu ngoài phạm vi), khoảng tin cậy rộng. Số liệu chọn bản int8 và số ứng viên của reranker ở [ADR-008](adr/008-D8-embedding-selection.md) và [ADR-007](adr/007-D7-hybrid-rag.md).

**Giới hạn:**
- Mọi kịch bản do nhóm dự án viết; tập nghiệm thu nhỏ (16 kịch bản), nên 48/48 là bằng chứng tốt trong phạm vi các tình huống đó, không phải tỷ lệ trên khách hàng thật.
- Gemini là dịch vụ ngoài: quá tải (503) và hết hạn mức ngày đã xảy ra trong lúc đo. Agent trả câu an toàn khi đó, nhưng khách không được phục vụ trong lượt ấy.
- Chưa kiểm chứng: tải đồng thời với Gemini thật, thời gian phản hồi trên phần cứng khác, triển khai trên máy chủ thật (ảnh Docker đã build và chạy được cục bộ).
