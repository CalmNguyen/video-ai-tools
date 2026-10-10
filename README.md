# VideoAI

## Mở tool

Chạy một file duy nhất rồi chọn tool trong cửa sổ launcher:

```powershell
python main.py
```

Launcher có: Cắt Short từ video dài, Full Video Editor, Tự lên kịch bản + TTS +
dựng video, Sửa SRT bằng Gemini, Thêm giọng từ SRT vào video, Cắt khoảng lặng MP3 + MP4, và Tải video. Mỗi tool mở trong một cửa sổ riêng.

## Thêm giọng từ SRT vào video (TTS For Free)

Dành cho video gốc đã có âm thanh nhưng thiếu nhiều chỗ nói. Mở từ launcher hoặc chạy
`python gemini_srt_voiceover.py`. Cần `pip install -U requests cryptography` và FFmpeg.

1. Nhập TTS For Free API key và giọng (voice). Nếu đã nhập trong tool
   "Tự lên kịch bản + TTS + dựng video" thì key, endpoint và voice được dùng chung tự động.
2. `Thêm video...` chọn nhiều video một lần (file `.srt` cùng tên cạnh video sẽ tự nạp).
3. Chọn từng video trong danh sách rồi dán SRT vào ô bên dưới (hoặc `Mở file .srt...`).
4. Chỉnh âm gốc / giọng mới / `Hạ âm gốc khi giọng mới nói` nếu cần.
5. Bấm `Tạo giọng + ghép vào video`. Mỗi video gọi API SRT-to-Speech
   (`POST /api/TTS/CreateSrt`, poll `GET /api/TTS/Multi/Status/{id}`), giọng được đặt đúng
   timestamp trong SRT, tự khớp tốc độ trong khoảng min–max, rồi ghép với âm thanh gốc.

Kết quả: `<tên video>_voiced.mp4` (hình giữ nguyên, không encode lại nếu codec cho phép).
File SRT và audio giọng được lưu ở thư mục `voiceover_work/`. Chạy lại cùng SRT/giọng thì dùng
lại audio đã tạo, không tốn thêm credit (có thể tắt bằng checkbox).

## Cắt khoảng lặng MP3 + MP4

Dành cho video (thường không có tiếng) dựng theo timeline của một file MP3 giọng đọc, khi
giữa các câu ngừng quá lâu (nói 1-3s rồi 5s sau mới nói tiếp). Mở từ launcher hoặc chạy
`python silence_trimmer.py`. Chỉ cần FFmpeg, không cần API key.

1. Chọn MP3 giọng đọc và MP4 (nếu có MP4 cùng tên cạnh MP3 thì tự điền).
2. Chỉnh nếu cần:
   - `Giữ lại giữa 2 câu`: khoảng lặng còn lại sau khi cắt (mặc định 200ms, 150ms là gọn hơn).
   - `Chỉ cắt khi lặng dài hơn`: ngừng ngắn hơn mức này giữ nguyên (mặc định 400ms).
   - `Ngưỡng im lặng`: mặc định -40dB. Tiếng ồn nền bị coi là nói thì đổi -35/-30,
     bị lẹm cuối chữ thì đổi -45/-50.
   - `Cắt cả khoảng lặng đầu/cuối file`: mặc định tắt (giữ nguyên intro/outro của video).
3. `Phân tích khoảng lặng` để xem danh sách đoạn sẽ cắt và độ dài mới.
4. `Cắt + xuất`: MP3 và MP4 bị cắt đúng cùng các đoạn (cắt ở ranh giới frame nên không bị
   lệch tiếng/hình), mỗi chỗ ngừng chừa một nửa khoảng giữ lại sau câu trước và một nửa trước câu sau.

Kết quả: `<tên video>_trimmed.mp4` (hình đã cắt + tiếng MP3 đã cắt) và nếu bật thì thêm
`<tên mp3>_trimmed.mp3`. Video được encode lại (H.264, CRF 18) vì phải cắt chính xác từng frame.

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
