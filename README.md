# VideoAI

## Chạy Gemini AI Full Video Editor

Yêu cầu: Python 3.11+, Gemini API key, FFmpeg và ffprobe trong `PATH`.

Mở PowerShell tại thư mục dự án rồi chạy:

```powershell
python -m pip install -U google-genai
ffmpeg -version
ffprobe -version
python gemini_ai_full_video_editor.py
```

Quy trình sử dụng:

1. Chọn video gốc, SRT gốc và script gốc.
2. Chọn MP3 giọng mới và SRT khớp với giọng mới.
3. Nhập Gemini API key.
4. Nhấn `Analyze & Create Plan`.
5. Kiểm tra hoặc sửa `edit_plan.json` nếu cần.
6. Nhấn `Render Video`.

Có thể dùng `Analyze + Render` để chạy toàn bộ quy trình một lần.

Phụ đề mặc định đang tắt. Bật `Render subtitles onto video` nếu muốn burn
phụ đề vào MP4. Có thể chỉnh `Subtitle font size` (mặc định `14`).

Hướng dẫn cài đặt, sử dụng, output và xử lý lỗi đầy đủ nằm tại
[README_GEMINI_AI_FULL_VIDEO_EDITOR.md](README_GEMINI_AI_FULL_VIDEO_EDITOR.md).
