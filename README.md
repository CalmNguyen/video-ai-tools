# VideoAI

## Mở tool

Chạy một file duy nhất rồi chọn tool trong cửa sổ launcher:

```powershell
python main.py
```

Launcher có: Cắt Short từ video dài, Full Video Editor, Tự lên kịch bản + TTS +
dựng video, Sửa SRT bằng Gemini, và Tải video. Mỗi tool mở trong một cửa sổ riêng.

## Sửa SRT bằng Gemini

Mở từ launcher, chạy `python gemini_srt_fixer.py`, hoặc bấm `Fix with Gemini...` trong
cửa sổ `View / Copy` của Full Video Editor.

1. Dán SRT (hoặc `Mở file SRT...`) vào ô bên trái.
2. Dán văn bản mẫu/ngữ cảnh đúng vào ô bên phải: script gốc, tên riêng, thuật ngữ,
   ghi chú. Nếu dán chung văn bản mẫu vào ô SRT thì phần không phải SRT cũng tự
   được dùng làm ngữ cảnh.
3. Chọn chế độ:
   - Mặc định: chỉ sửa chữ nhận sai, giữ nguyên số block và timestamp.
   - `Làm mượt câu`: ngắt lại block theo câu/cụm tự nhiên, thêm dấu câu; timestamp
     được tính lại từ thời gian của từng từ gốc. Không bao giờ dời chữ qua chỗ im
     lặng ≥ 0.8s, nên các đoạn cắt/demo vẫn đúng thời gian. Nhóm nào Gemini làm
     mất hoặc đổi quá nhiều chữ thì tự giữ nguyên bản gốc.
   - `Bỏ từ đệm / nói lặp`: cho phép bỏ "ha", "ờ", "thì ok thì"...
4. Bấm `Sửa bằng Gemini`, xem danh sách thay đổi bên phải (double-click để nhảy tới,
   `Bỏ sửa các dòng đã chọn` để hoàn tác), rồi `Copy all` hoặc lưu.

## Chạy Gemini AI Full Video Editor

Yêu cầu: Python 3.11+, Gemini API key, FFmpeg và ffprobe trong `PATH`.

Mở PowerShell tại thư mục dự án rồi chạy:

```powershell
python -m pip install -U google-genai
ffmpeg -version
ffprobe -version
python main.py   # rồi chọn Full Video Editor
```

Quy trình sử dụng:

1. Nhập Gemini API key.
2. Chọn video gốc và MP3 giọng mới.
3. Chọn SRT có sẵn, hoặc bấm `Gemini SRT` để Gemini tự tạo (xem/copy toàn bộ
   bằng `View / Copy`).
4. Gõ/dán script gốc vào ô text (không bắt buộc).
5. Nhấn `Analyze & Create Plan`. Nếu bật `Keep original demo audio`, Gemini sẽ
   tìm các đoạn demo và giữ âm thanh gốc ở đó (giọng mới tạm dừng).
6. Kiểm tra hoặc sửa `edit_plan.json` nếu cần.
7. Nhấn `Render Video`.

Có thể dùng `Analyze + Render` để chạy toàn bộ quy trình một lần.

Phụ đề mặc định đang tắt. Bật `Render subtitles onto video` nếu muốn burn
phụ đề vào MP4. Có thể chỉnh `Subtitle font size` (mặc định `14`).

Hướng dẫn cài đặt, sử dụng, output và xử lý lỗi đầy đủ nằm tại
[README_GEMINI_AI_FULL_VIDEO_EDITOR.md](README_GEMINI_AI_FULL_VIDEO_EDITOR.md).
