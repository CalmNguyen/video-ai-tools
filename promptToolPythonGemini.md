Bạn hãy đóng vai Senior Python Developer.

Hãy viết cho tôi một tool Python **1 file duy nhất** có giao diện desktop đơn giản để tạo video AI tự động từ kịch bản.

Yêu cầu công nghệ:

* Python 3.11+
* Giao diện dùng Tkinter hoặc CustomTkinter
* Chỉ cần 1 file `.py`
* Có log realtime trên giao diện
* Code rõ ràng, có try/catch, có comment
* Có thể chạy trên Windows

Chức năng giao diện:

1. Ô nhập Gemini/Google API Key
2. Ô nhập API endpoint của web TTS For Free
3. Ô nhập voice dạng string, ví dụ: `vi-VN-HoaiMyNeural`
4. Ô nhập kịch bản hoặc ý tưởng video
5. Chọn model tạo ảnh:

   * Imagen 4 Fast Generate
   * Imagen 4 Generate
   * Imagen 4 Ultra Generate
6. Nút Generate Video
7. Khung log trạng thái
8. Ô hiển thị file output cuối cùng

Luồng xử lý:

Bước 1: Nhận kịch bản/ý tưởng từ user.

Bước 2: Gọi Gemini text model để biến ý tưởng thành một video script dạng JSON gồm nhiều scene.

Mỗi scene cần có format:

```json
{
  "sceneIndex": 1,
  "narrationText": "Câu thoại ngắn, dễ đọc, không quá dài",
  "imagePrompt": "Prompt tạo ảnh phù hợp với câu thoại",
  "durationSeconds": 5
}
```

Yêu cầu chia scene:

* Mỗi scene chỉ nên có 1–2 câu ngắn.
* Không để một ảnh phải nói quá nhiều.
* Nếu text dài thì tự chia thành nhiều scene.
* durationSeconds ước lượng theo độ dài narrationText.
* Tổng video nên mượt, mỗi ảnh khớp nội dung đang nói.

Bước 3: Với mỗi scene, gọi Imagen 4 để tạo ảnh tương ứng từ `imagePrompt`.

Yêu cầu tạo ảnh:

* 16:9
* phong cách cinematic / YouTube educational / clean professional
* lưu ảnh vào folder temp
* nếu gặp lỗi rate limit / too many requests / 429 thì delay rồi retry
* retry tối đa 3 lần
* delay tăng dần: 10s, 30s, 60s

Bước 4: Từ danh sách `narrationText`, tự tạo file SRT chuẩn.

Yêu cầu SRT:

* format chuẩn:

```srt
1
00:00:00,000 --> 00:00:05,000
Nội dung thoại scene 1

2
00:00:05,000 --> 00:00:10,000
Nội dung thoại scene 2
```

* thời gian dựa trên `durationSeconds`
* tự cộng dồn timestamp
* không overlap
* không sai format
* lưu file `script.srt`

Bước 5: Gọi API SRT-to-Speech hiện tại của web TTS For Free.

Input gửi lên API:

* file SRT
* voice string user nhập
* các config cần thiết nếu có
* API endpoint user nhập trên giao diện

Sau khi submit job:

* lấy `jobId`
* polling status mỗi 5 giây
* nếu status `completed` thì lấy `audioUrl`
* nếu status `failed` thì báo lỗi rõ
* download audio file về local

Do chưa biết chính xác API schema của tôi, hãy viết code theo kiểu dễ sửa:

* tạo class `TtsForFreeClient`
* có method `submit_srt_job(srt_path, voice)`
* có method `poll_until_completed(job_id)`
* có method `download_audio(audio_url)`
* để endpoint/path/config thành biến dễ chỉnh ở đầu file

Bước 6: Tạo video từ ảnh.

Yêu cầu:

* Dùng FFmpeg
* Mỗi ảnh hiển thị đúng durationSeconds của scene
* Scale/crop về 1920x1080
* tạo video tạm `image_video.mp4`
* có thể dùng concat demuxer hoặc filter_complex
* mỗi ảnh nên có hiệu ứng nhẹ như zoom/pan nếu làm được, không bắt buộc

Bước 7: Merge video ảnh + audio + subtitle.

Yêu cầu:

* Dùng FFmpeg
* ghép `image_video.mp4` với audio tải về
* burn subtitle từ file `script.srt` lên video
* output cuối cùng `final_video.mp4`
* dùng codec:

  * video: libx264
  * audio: aac
  * preset: veryfast
  * crf: 23
* nếu audio dài hơn video thì kéo video theo audio hoặc dùng duration tổng từ SRT sao cho không bị cắt tiếng
* nếu video dài hơn audio thì cắt theo audio

Bước 8: Sau khi hoàn thành:

* hiển thị path final video trên GUI
* log đầy đủ từng bước

Yêu cầu code:

* Không hardcode API key
* Có kiểm tra FFmpeg đã cài chưa
* Có tạo folder `outputs` và `temp`
* Có hàm helper:

  * `format_srt_timestamp(seconds)`
  * `estimate_duration(text)`
  * `write_srt(scenes, path)`
  * `run_ffmpeg(args)`
  * `download_file(url, path)`
  * `retry_with_backoff(fn)`
* Code phải chạy được sau khi cài package cần thiết.
* In ra danh sách package cần cài ở comment đầu file.

Lưu ý quan trọng:

* Gemini text model chỉ dùng để tạo/chia script.
* Imagen 4 mới là model tạo ảnh.
* Không dùng Gemini Flash Lite để tạo ảnh.
* Free quota Imagen chỉ khoảng 25 RPD mỗi model, nên phải có retry/delay khi rate limit.
* Tool này phục vụ tạo video ngắn từ script, ảnh, voice TTS và subtitle.

Hãy trả về toàn bộ code Python hoàn chỉnh trong một file.
