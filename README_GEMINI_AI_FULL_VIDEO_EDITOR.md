# Gemini AI Full Video Editor

Ứng dụng desktop dùng Gemini để re-edit video gốc theo timeline của một file giọng đọc mới.

Khác với việc chỉ thay audio, công cụ sẽ:

- Đọc nội dung và timestamp của video gốc qua SRT.
- Đọc script gốc để hiểu toàn bộ ngữ cảnh.
- Đọc SRT của giọng mới để xác định timeline đầu ra.
- Dùng Gemini ghép nội dung tương ứng giữa hai timeline theo ngữ nghĩa.
- Tạo một `edit_plan.json` có thể xem và sửa thủ công.
- Dùng FFmpeg dựng lại hình ảnh và ghép giọng mới thành video hoàn chỉnh.

## 1. Yêu cầu

- Windows 10/11.
- Python 3.11 trở lên.
- FFmpeg và ffprobe có trong `PATH`.
- Gemini API key.
- Kết nối Internet khi chạy bước phân tích bằng Gemini.

## 2. Cài dependency Python

Mở PowerShell tại thư mục dự án:

```powershell
cd D:\work\python\VideoAI
python -m pip install -U google-genai
```

Kiểm tra Python:

```powershell
python --version
```

## 3. Cài FFmpeg

Nếu đã cài FFmpeg, kiểm tra bằng:

```powershell
ffmpeg -version
ffprobe -version
```

Nếu PowerShell báo không tìm thấy lệnh, hãy cài FFmpeg và thêm thư mục `bin` của FFmpeg vào biến môi trường `PATH`.

Sau khi chỉnh `PATH`, đóng PowerShell hiện tại, mở cửa sổ PowerShell mới rồi kiểm tra lại hai lệnh trên.

## 4. Chạy ứng dụng

Tại thư mục dự án, chạy:

```powershell
cd D:\work\python\VideoAI
python gemini_ai_full_video_editor.py
```

Cũng có thể chạy package trực tiếp:

```powershell
python -m gemini_full_video_editor
```

## 5. Chuẩn bị đầu vào

Ứng dụng cần 5 file bắt buộc:

1. `Original Video`: video MP4/MOV/MKV/WEBM gốc.
2. `Original Video SRT`: phụ đề khớp chính xác với video gốc.
3. `Original Script TXT`: bản script gốc đầy đủ, sạch và chưa chia subtitle.
4. `Target Voice MP3`: giọng đọc mới dùng làm audio chính.
5. `Target Voice SRT`: phụ đề khớp chính xác với giọng đọc mới.

Có thể chọn thêm `Assets Folder`. Thư mục này hỗ trợ:

- Ảnh: PNG, JPG, JPEG, WEBP.
- Video: MP4, MOV, WEBM.

Tên asset và metadata sẽ được gửi cho Gemini. File nhị phân không được chèn vào prompt.

## 6. Quy trình khuyên dùng

### Bước 1: Điền Gemini API Key

Nhập API key vào `API Key`.

Chọn model trong ô `Model`, hoặc nhập model ID khác. Bật `Remember API key` nếu muốn lưu key trên máy.

### Bước 2: Chọn các file đầu vào

Chọn đủ 5 file bắt buộc, thư mục assets nếu có, và thư mục output.

### Bước 3: Cấu hình dựng video

- `Resolution`: mặc định `1920x1080`.
- `FPS`: mặc định `30`.
- `Render subtitles onto video`: burn Target Voice SRT vào video. Mặc định tắt.
- `Subtitle font size`: cỡ chữ subtitle burn vào video, mặc định `14`.
- `Export sidecar SRT`: xuất `final_video.srt` cạnh MP4. Mặc định tắt. Một số
  trình phát sẽ tự động hiển thị file SRT này dù subtitle không được burn vào MP4.
- `Original audio / Mute`: tắt hoàn toàn tiếng gốc.
- `Original audio / Background`: giữ tiếng gốc nhỏ dưới giọng mới.
- `Background volume`: mặc định `0.10`.
- `Normalize final audio`: chuẩn hóa âm lượng cuối.
- `Max Speed Up`: mặc định `1.20`.
- `Max Slow Down`: mặc định `0.85`.

Trong `Additional Gemini editing instructions`, có thể nhập ví dụ:

```text
Đây là video hướng dẫn phần mềm.
Ưu tiên cảnh quay màn hình khi lời đọc mô tả thao tác.
Dùng talking-head cho các đoạn giải thích.
Giữ cách dựng sạch và chuyên nghiệp, không lạm dụng zoom.
```

### Bước 4: Tạo plan

Nhấn:

```text
Analyze & Create Plan
```

Ứng dụng sẽ:

1. Parse và sửa encoding/mojibake trong SRT/script.
2. Đọc metadata video và audio bằng ffprobe.
3. Quét assets.
4. Gọi Gemini để semantic alignment.
5. Gọi Gemini để tạo edit plan.
6. Kiểm tra cue ID, timestamp, asset, gap, overlap và tốc độ.

Sau khi hoàn tất, ứng dụng tự điền đường dẫn `edit_plan.json` vào ô `Edit Plan JSON`.

### Bước 5: Kiểm tra hoặc sửa plan

Mở file:

```text
edit_plan.json
```

Ví dụ có thể sửa:

```json
"sourceStart": 52.1
```

thành:

```json
"sourceStart": 53.4
```

Lưu lại file. Không cần gọi Gemini lại.

### Bước 6: Render

Nhấn:

```text
Render Video
```

Ứng dụng sẽ đọc lại `edit_plan.json`, validate lại plan rồi render bằng FFmpeg.

Nếu không cần kiểm tra plan trước, có thể nhấn:

```text
Analyze + Render
```

## 7. Kết quả đầu ra

Mỗi lần Analyze sẽ tạo một thư mục dạng:

```text
full_video_outputs/
    edit_YYYYMMDD_HHMMSS/
        semantic_alignment.json
        edit_plan.json
        validated_edit_plan.json
        validation_report.json
        asset_manifest.json
        script_context.txt
        final_video.mp4
        final_video.srt
        logs.txt
        temp/
```

Các file quan trọng:

- `semantic_alignment.json`: nội dung tương ứng giữa SRT gốc và SRT mới.
- `edit_plan.json`: plan gốc từ Gemini, có thể sửa bằng tay.
- `validated_edit_plan.json`: plan sau khi được kiểm tra và sửa lỗi an toàn.
- `validation_report.json`: báo cáo coverage, speed và fallback.
- `final_video.mp4`: video hoàn chỉnh.
- `logs.txt`: log đầy đủ để kiểm tra khi có lỗi.

Nếu render thất bại, plan và thư mục `temp` vẫn được giữ lại để debug.

## 8. Render lại mà không gọi Gemini

Để render lại một plan cũ:

1. Mở ứng dụng.
2. Chọn lại đúng video, SRT, script, target voice và assets đã dùng để tạo plan.
3. Chọn file `edit_plan.json` trong ô `Edit Plan JSON`.
4. Nhấn `Render Video`.

Bước này không cần Gemini API key và không gọi Gemini.

## 9. Dừng tác vụ

Nhấn `Stop` để yêu cầu dừng.

- Gemini retry sẽ dừng tại điểm an toàn gần nhất.
- Tiến trình FFmpeg hiện tại sẽ được terminate.
- Các plan và file tạm đã tạo vẫn được giữ lại.

## 10. Lỗi thường gặp

### `Missing google-genai`

Chạy:

```powershell
python -m pip install -U google-genai
```

### `Could not find ffmpeg` hoặc `ffprobe`

FFmpeg chưa được cài hoặc thư mục `bin` chưa có trong `PATH`.

Kiểm tra:

```powershell
ffmpeg -version
ffprobe -version
```

### Gemini báo 429, quota hoặc resource exhausted

Ứng dụng tự retry với thời gian chờ tăng dần. Nếu vẫn lỗi:

- Kiểm tra quota của API key.
- Đợi một lúc rồi thử lại.
- Chọn model khác.

### Gemini trả JSON lỗi

Ứng dụng sẽ retry và không âm thầm nhận plan JSON sai. Xem chi tiết trong `logs.txt`.

### Subtitle bị lỗi tiếng Việt

Ứng dụng thử đọc UTF-8, UTF-16, CP1258 và CP1252, sau đó sửa mojibake phổ biến. Bản SRT đã chuẩn hóa được dùng khi burn và export video cuối.

### Render Video báo thiếu file

Plan không chứa file media bên trong. Khi render lại, cần chọn đúng các file nguồn và assets tương ứng trong giao diện.

### Video cuối ngắn hơn giọng đọc

Mở `logs.txt` và kiểm tra bước `Final verification`. Target Voice MP3 luôn là timeline chính; ứng dụng sẽ không chủ động dùng visual ngắn để cắt mất giọng đọc.

## 11. File chạy chính

- Launcher: `gemini_ai_full_video_editor.py`
- Package ứng dụng: `gemini_full_video_editor/`
- Công cụ short cũ: `gemini_video_short_cutter.py` — không bị thay đổi.
