# Prompt tái tạo Gemini + TTS For Free Video Tool / Rebuild Prompt

Tài liệu này được viết từ hành vi thực tế của `gemini_ttsforfree_video_tool.py`. Chỉ cần sao chép **một** trong hai prompt bên dưới và gửi cho AI lập trình. Hai bản có cùng yêu cầu; bản tiếng Việt và bản tiếng Anh đều độc lập, đầy đủ và có thể dùng riêng.

> Lưu ý: prompt yêu cầu tái tạo đúng hành vi hiện tại, bao gồm việc lưu API key vào file settings dạng văn bản thuần. Chỉ dùng trên máy cá nhân đáng tin cậy và không commit file settings/history lên Git.

## Bản tiếng Việt — sao chép prompt này

~~~text
Bạn là một Senior Python Desktop/Application Engineer. Hãy xây dựng hoàn chỉnh một công cụ desktop Windows tên “Gemini + TTS For Free Video Tool”, tái tạo đúng đặc tả bên dưới. Không làm bản demo, không dùng mock, không để TODO, không bỏ bớt tính năng. Công cụ phải thực sự gọi API, tạo ảnh, tạo giọng đọc, chạy FFmpeg và xuất video MP4.

Khi thực hiện:

1. Nếu bạn có quyền sửa workspace, hãy tạo file chính `gemini_ttsforfree_video_tool.py`. Mọi logic runtime phải nằm trong một file Python duy nhất.
2. Nếu bạn chỉ có thể trả lời trong chat, hãy trả về toàn bộ nội dung file Python trong một code block duy nhất.
3. Sau code, phải cung cấp hướng dẫn cài đặt, cấu hình API, cài FFmpeg, chạy tool, giải thích các file output và xử lý lỗi thường gặp.
4. Không hỏi lại những chi tiết đã có trong đặc tả. Tự hoàn thiện code, tự kiểm tra syntax/import và sửa lỗi trước khi bàn giao.
5. Dùng Python 3.11+, type hints, class/hàm rõ ràng, xử lý lỗi đầy đủ và bảo đảm GUI không bị treo khi tác vụ dài chạy.

## 1. Mục tiêu và pipeline bắt buộc

Đây là ứng dụng Tkinter một file chạy ưu tiên trên Windows. Nó nhận một hoặc nhiều ý tưởng/kịch bản trong một batch và chạy pipeline thật:

1. Gemini text biến ý tưởng hoặc kịch bản thành JSON nhiều scene.
2. Mỗi scene được tạo đúng một ảnh bằng provider/model ảnh được chọn: Gemini hoặc fal.ai.
3. Tạo `scenes.json` và file subtitle `script.srt`.
4. Gửi từng cue SRT tới API thật của TTS For Free:
   - `POST /api/TTS/CreateSrt`
   - `GET /api/TTS/Multi/Status/{job_id}`
5. Poll đến khi hoàn tất, lấy URL và tải audio.
6. Dùng FFmpeg tạo video từ chuỗi ảnh.
7. Ghép video + audio, burn subtitle và xuất `final_video.mp4`.
8. Lưu settings, lịch sử, usage và chi phí ước tính; hiển thị log realtime.

Gemini luôn chịu trách nhiệm lập scene, kể cả khi fal.ai là provider tạo ảnh.

## 2. Công nghệ và dependency

Chỉ dùng standard library cộng với:

- `requests`
- `google-genai`
- `cryptography`
- `Pillow` (SDK ảnh có thể cần)
- Tkinter có sẵn trong Python
- chương trình ngoài: `ffmpeg` và `ffprobe` phải có trong `PATH`

Đặt comment ở đầu file:

`pip install requests google-genai cryptography Pillow`

Khi khởi động pipeline, kiểm tra package runtime và kiểm tra cả `ffmpeg` lẫn `ffprobe`; báo lỗi rõ ràng nếu thiếu.

Vì thư mục chạy có thể chứa file cục bộ tên `copy.py`, trước khi import thư viện bên thứ ba phải có helper `_load_stdlib_copy_before_local_modules()` để tạm loại thư mục script khỏi `sys.path`, import module `copy` chuẩn của Python, rồi phục hồi `sys.path`. Điều này ngăn file `copy.py` cạnh tool che khuất standard library.

## 3. Hằng số, đường dẫn và biến môi trường

Dùng thư mục chứa file Python làm gốc:

- `outputs/`
- `temp/`
- `gemini_ttsforfree_video_tool.settings.json`
- `gemini_ttsforfree_video_tool.history.json`
- history tối đa 100 entry và có `threading.Lock` bảo vệ thao tác read-modify-write.

Mỗi lần chạy một video tạo `run_id` dạng `YYYYMMDD_HHMMSS_<8 ký tự uuid hex>`.

Cấu trúc:

- `temp/<run_id>/scenes.json`
- `temp/<run_id>/script.srt`
- `temp/<run_id>/images/scene_001.png`, `scene_002.png`, ...
- `temp/<run_id>/images_concat.txt`
- `temp/<run_id>/image_video.mp4`
- `temp/<run_id>/tts_audio.mp3`
- `outputs/<run_id>/final_video.mp4`
- `outputs/<run_id>/token_usage.json`

Dùng chính xác các default và env sau:

- `GEMINI_TEXT_MODEL`, mặc định `gemini-2.5-flash`
- `TTSFORFREE_ENDPOINT`, mặc định `https://api.ttsforfree.com`
- `TTSFORFREE_API_KEY`
- `TTSFORFREE_BEARER_TOKEN`
- `TTSFORFREE_COOKIE`
- `TTSFORFREE_EXTRA_HEADERS`: chuỗi JSON object
- `TTSFORFREE_SPEED`, mặc định `0.9`
- `TTSFORFREE_PITCH`, mặc định `0`
- `TTSFORFREE_VOLUME`, mặc định `1`
- `FAL_API_KEY`, fallback `FAL_KEY`
- `FAL_REQUEST_TIMEOUT_SECONDS`, mặc định `300`
- `IMAGE_GENERATION_MAX_WORKERS`, mặc định chuỗi `10`; nếu không parse được thì fallback `3`

Thông số:

- TTS poll mỗi 5 giây, timeout tổng 20 phút
- HTTP timeout chung 60 giây
- download theo chunk 512 KiB
- Gemini retry delay: 10, 30, 60 giây
- fal.ai retry delay: 5, 15, 30 giây
- video 1920x1080, 30 FPS

## 4. Registry provider/model ảnh

Thiết kế provider mở rộng được bằng abstract base class `ImageGenerationProvider`, hai implementation `GeminiImageProvider`, `FalImageProvider`, registry `IMAGE_PROVIDER_CLASSES` và factory `create_image_provider(...)`. Dữ liệu provider/model nằm ngoài UI.

Registry phải có đúng:

1. Provider id `gemini`, label `Gemini`
   - label `Gemini 3.1 Flash Lite Image`
     - id `gemini-3.1-flash-lite-image`
     - family `gemini_image`
     - hỗ trợ reference image
     - giá input/output mỗi một triệu token: 0.25 / 30.00
   - label `Gemini 3.1 Flash Image`
     - id `gemini-3.1-flash-image`
     - family `gemini_image`
     - hỗ trợ reference image
     - giá: 0.50 / 60.00
   - label `Gemini 3 Pro Image`
     - id `gemini-3-pro-image`
     - family `gemini_image`
     - hỗ trợ reference image
     - giá: 2.00 / 120.00

2. Provider id `fal`, label `fal.ai`
   - label `FLUX.1 Dev`
     - id và endpoint `fal-ai/flux/dev`
     - family `flux`
     - không hỗ trợ reference image
     - request default: `image_size=landscape_16_9`, `num_images=1`, `output_format=png`
     - giá mặc định 0.025 USD/ảnh
   - label `FLUX.1 Kontext`
     - id `fal-ai/flux-pro/kontext`
     - endpoint text-to-image `fal-ai/flux-pro/kontext/text-to-image`
     - endpoint có reference `fal-ai/flux-pro/kontext/multi`
     - reference field `image_urls`
     - family `flux_kontext`
     - hỗ trợ reference image
     - request default: `aspect_ratio=16:9`, `num_images=1`, `output_format=png`
     - giá mặc định 0.04 USD/ảnh

Cho phép override từng giá bằng env dễ hiểu tương ứng:

- `GEMINI_TEXT_INPUT_COST_PER_MILLION` mặc định 0.30
- `GEMINI_TEXT_OUTPUT_COST_PER_MILLION` mặc định 2.50
- `GEMINI_FLASH_LITE_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_FLASH_LITE_IMAGE_OUTPUT_COST_PER_MILLION`
- `GEMINI_FLASH_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_FLASH_IMAGE_OUTPUT_COST_PER_MILLION`
- `GEMINI_PRO_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_PRO_IMAGE_OUTPUT_COST_PER_MILLION`
- `FAL_FLUX_DEV_COST_PER_IMAGE`
- `FAL_FLUX_KONTEXT_COST_PER_IMAGE`

Hỗ trợ migrate ba model label cũ:

- `Gemini 3.1 Flash-Lite Image | Fastest, lowest cost`
- `Gemini 3.1 Flash Image | Balanced, supports higher resolution`
- `Gemini 3 Pro Image | Highest quality, complex detail`

sang ba label Gemini mới tương ứng.

Không tự thay model id bằng model khác. Nếu API không còn model, báo lỗi rõ và để người dùng sửa registry/env.

## 5. Data model và helper bắt buộc

Có các class:

- `Scene(sceneIndex, narrationText, imagePrompt, durationSeconds)` và `to_dict()`
- `ReferenceImage(path, mime_type, data)`
- `TokenUsageTracker`
- `VideoJobRequest`
- `VideoJobCard`
- ba exception: `RetryableError`, `ImageGenerationNoOutputError`, `TtsJobFailedError`

Có các helper tương đương:

- parse số nguyên dương/default, số không âm và float env
- `format_srt_timestamp`
- `estimate_duration`
- `write_srt`
- parse/load cue SRT
- `run_ffmpeg`
- `probe_media_duration_seconds`
- `download_file`
- `retry_with_backoff`
- normalize/load/summarize reference image
- parse/repair JSON Gemini
- đọc/mở path và quản lý history

`estimate_duration(text)`:

- normalize whitespace
- rỗng thì 3 giây
- số giây cơ bản = số từ / 2.6 + số dấu `[,.!?;:]` * 0.12
- ceil và tối thiểu 3 giây

`write_srt` dùng duration integer tối thiểu 1, cộng dồn timestamp, không overlap, UTF-8.

`normalize_scenes` chấp nhận trực tiếp một array hoặc object có key `scenes`; bỏ item không phải object hoặc thiếu `narrationText`/`imagePrompt`; đánh lại `sceneIndex` tuần tự từ 1; duration lỗi thì dùng `estimate_duration`; nếu không còn scene hợp lệ thì raise error.

## 6. Gemini lập kịch bản scene

Class `GeminiVideoClient` dùng `genai.Client(api_key=...)`.

GUI cho mỗi video có mode `Short` hoặc `Long`.

Prompt lập scene phải bắt Gemini trả về JSON only, mỗi scene có đúng bốn key:

- `sceneIndex`
- `narrationText`
- `imagePrompt`
- `durationSeconds`

Quy tắc chung:

- narration tự nhiên cho AI TTS
- imagePrompt mô tả chính xác những gì nhìn thấy
- mỗi scene khác scene trước về hình ảnh
- câu chuyện mượt, cảm xúc, coherent
- shared prompt áp dụng cho tất cả video trong batch
- video prompt chỉ áp dụng video đó
- prompt bổ sung không được làm thay đổi JSON schema bắt buộc

Short mode:

- vai trò “senior short-form video script planner”
- thường 6–12 scene
- mỗi scene chỉ một khoảnh khắc/hành động chính
- narration thường một câu ngắn
- 8–14 từ, tuyệt đối không quá 16 từ
- câu dài phải tách scene
- đọc khoảng 2–4 giây
- duration thường 2–4 giây
- tránh mô tả thừa và cấu trúc câu phức tạp
- input được xem là idea/script
- `max_output_tokens=None`

Long mode:

- vai trò “senior long-form video script and scene planner”
- xem input là kịch bản narration dài đã hoàn thành
- giữ toàn bộ ý nghĩa, thứ tự, facts, examples và chi tiết quan trọng
- không tóm tắt, rút ngắn mạnh hoặc bỏ đoạn
- giữ wording gốc khi có thể
- tổng `narrationText` phải phủ toàn bộ script từ đầu đến cuối; chỉ cleanup dấu câu nhẹ
- tạo đủ scene, thường 20–120 nhưng không giới hạn cứng
- mỗi scene là một visual beat coherent
- narration 1–3 câu, thường 15–40 từ, không quá 60 từ
- tách paragraph dài qua scene liên tiếp mà không mất nội dung
- duration thường 5–15 giây
- giữ continuity người, nơi, vật và style
- `max_output_tokens=65536`

Gọi `generate_content` với text model, temperature 0.4, `response_mime_type="application/json"`.

Đọc text từ `response.text`; nếu không có thì duyệt `candidates[].content.parts[].text`.

Parser JSON phải chịu lỗi:

- bỏ Markdown code fence
- tìm object/array JSON trong text
- sửa smart quotes
- quote các key scene phổ biến bị thiếu quote
- bỏ trailing comma và comment JS phổ biến
- fallback `ast.literal_eval` cho output kiểu Python dùng single quote
- nếu vẫn là `JSONDecodeError`, gọi Gemini đúng một lần để repair JSON, temperature 0, JSON MIME; yêu cầu không dịch/tóm tắt/thêm Markdown

## 7. Tạo ảnh

Prompt Gemini ảnh = scene prompt cộng ý nghĩa chính xác:

“Create exactly one finished image for this scene. Cinematic, YouTube educational, clean professional visual style, landscape 16:9 composition, highly detailed, natural lighting, no text overlay, no watermark.”

Với Gemini image model:

- mỗi worker thread dùng `threading.local()` để có `genai.Client` riêng
- gọi `models.generate_content`
- `response_modalities=["IMAGE"]`
- `types.ImageConfig(aspect_ratio="16:9")`
- lấy ảnh đầu tiên từ part qua `as_image()` hoặc `inline_data.as_image()`
- lưu được cả object có `.save()` lẫn bytes `.data`
- nếu chỉ có text hoặc không có ảnh, raise `ImageGenerationNoOutputError`
- retry khi 429/rate limit/quota/503/504/timeout/internal server error hoặc exception retryable
- vẫn cài sẵn nhánh `generate_images` cho model Imagen tương lai: một ảnh, 16:9, PNG

Khi Gemini có reference images, contents gồm text prompt và từng ảnh bằng `types.Part.from_bytes`, kèm tên file. Chỉ dùng ảnh như visual guidance về product UI, layout language, colors, components và identity; không sao chép private data hoặc sensitive text.

Prompt fal.ai cơ bản phải bổ sung:

- Ultra realistic
- Cinematic
- Highly detailed
- Natural lighting
- Professional YouTube style
- 16:9
- No text
- No watermark

fal.ai gọi trực tiếp:

- `POST https://fal.run/{endpoint}`
- header `Authorization: Key <FAL_API_KEY>`
- JSON body từ request defaults cộng `prompt`
- lấy `payload.images[0].url`, rồi download ảnh
- retry network timeout/connection, HTTP 429 và 5xx

Với FLUX.1 Kontext và reference images:

- dùng endpoint `/multi`
- encode mỗi ảnh thành data URI `data:<mime>;base64,...`
- gửi vào `image_urls`
- prompt thêm yêu cầu giữ nguyên character identity, face, hairstyle, clothing; chỉ thay environment theo scene

Reference images hỗ trợ `.png`, `.jpg`, `.jpeg`, `.webp`; resolve path, bỏ file không tồn tại/sai extension/trùng lặp. Nếu model không hỗ trợ thì log rằng ảnh bị bỏ qua.

Mỗi scene image prompt thực tế gồm:

1. `scene.imagePrompt`
2. `Shared visual instructions: <shared prompt>` nếu có
3. `Video-specific visual instructions: <video prompt>` nếu có

Tạo ảnh scene song song bằng `ThreadPoolExecutor`, số worker = min(image threads, số scene). Thứ tự file/output vẫn phải theo scene.

Nếu một số ảnh lỗi nhưng còn ít nhất một ảnh thành công, copy ảnh thành công gần nhất làm fallback cho scene lỗi; khi khoảng cách bằng nhau ưu tiên scene trước rồi scene sau. Nếu tất cả ảnh lỗi, dừng video và báo lỗi đầu tiên.

## 8. Theo dõi token và chi phí

`TokenUsageTracker` phải thread-safe và theo dõi theo từng video:

- input tokens
- output/candidate tokens
- reported total
- thoughts tokens
- cached content tokens
- số API requests
- requests có metadata
- requests thiếu token counts
- breakdown theo operation, ví dụ `scene_script_short`, `scene_script_long`, `scene_json_repair`, `gemini_image`, `imagen_image`

Đọc được usage metadata dạng object hoặc dict, cả snake_case và camelCase. API attempt lỗi trước khi có response vẫn được đếm là unreported request.

Với Gemini, chi phí = token text dùng giá Gemini text + token operation `gemini_image` dùng giá model ảnh. Với fal.ai, chi phí = số ảnh tạo thành công * giá mỗi ảnh. Ghi metadata đầy đủ vào `token_usage.json` và hiển thị trên từng video card:

- Gemini: `Input Tokens: ... | Output Tokens: ... | Estimated Cost ($): ...`
- fal.ai: `Images Generated: ... | Estimated Cost ($): ...`

## 9. TTS For Free thật

Tạo class `TtsForFreeClient` có:

- `submit_srt_job(srt_path, voice)`
- `poll_until_completed(job_id)`
- `download_audio(audio_url, output_path=None)`

Mỗi client có `requests.Session()` và `connection_id = uuid.uuid4()` riêng.

Chuẩn hóa endpoint:

- trim `/`
- nếu kết thúc bằng `/docs`, `/swagger`, `/swagger/index.html` hoặc `/swagger/` thì bỏ suffix
- nếu người dùng đã nhập full `/api/TTS/CreateSrt`, dùng nó
- nếu chưa thì nối `/api/TTS/CreateSrt`
- base URL dùng cho status

Voice token:

- rỗng thì lỗi
- nếu bắt đầu bằng `v1:` thì gửi nguyên
- nếu là voice plain string thì mã hóa đúng thuật toán backend:
  - key Base64 chính xác: `K1hXK1B4K0pMaHBDaHh6WXh1R2J4S3ZBbkJoQ2l4eWw=`
  - decode key
  - random nonce 12 byte
  - AES-GCM, không có associated data
  - kết quả thư viện là `cipher || tag_16`; tách tag
  - pack theo thứ tự `nonce || tag || cipher`
  - URL-safe Base64, bỏ dấu `=`
  - thêm prefix `v1:`

Parse SRT thành cue có `start_ms`, `end_ms`, `text`; chấp nhận block có hoặc không có dòng index.

Body `POST /api/TTS/CreateSrt`:

{
  "connectionId": "<uuid>",
  "blocks": [
    {
      "texts": "<cue text>",
      "voice": "<encrypted voice>",
      "speed": 0.9,
      "pitch": 0,
      "volume": 1,
      "startMs": 0,
      "endMs": 3000
    }
  ]
}

Headers mặc định:

- `Accept: application/json`
- `Content-Type: application/json`
- `User-Agent: GeminiTtsForFreeTool/1.0`
- nếu GUI có API key: `X-API-Key`
- nếu env bearer có giá trị: `Authorization: Bearer ...`
- nếu env cookie có giá trị: `Cookie: ...`
- merge object từ `TTSFORFREE_EXTRA_HEADERS`

Response key phải đọc case-insensitive. Nếu `id == -1` hoặc status `error/failed`, raise với `message`. Job id lấy từ `jobId` hoặc `id`.

Poll status:

- URL `/api/TTS/Multi/Status/{job_id}`
- mỗi 5 giây, tối đa 20 phút
- thành công khi `success` hoặc `completed`
- thất bại khi `error` hoặc `failed`
- audio URL có thể ở `audioUrl`, `data`, `url` hoặc `expectUrl`
- completed mà không có URL thì lỗi
- status khác thì log pending

Download bằng session và cùng headers, streaming. File mặc định `.mp3`, nhưng đoán extension từ URL nếu có.

## 10. FFmpeg

Class `VideoAssembler`.

Tạo video ảnh:

- concat demuxer manifest: mỗi ảnh có dòng `file` và `duration`; lặp lại ảnh cuối ở cuối manifest
- escape path an toàn, kể cả Windows
- filter: `fps=30,scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p`
- codec `libx264`, pixel format `yuv420p`

Ghép cuối:

- dùng ffprobe lấy duration image video và audio
- độ dài đích chính xác theo audio
- nếu audio dài hơn video trên 0.01 giây, dùng `tpad=stop_mode=clone:stop_duration=<phần thiếu>` để giữ frame cuối
- burn SRT bằng filter `subtitles='<escaped absolute path>'`
- sau đó `format=yuv420p`
- video `libx264`, `preset veryfast`, `crf 23`
- audio `aac`, bitrate `192k`
- dùng `-t <audio_duration>` để không cắt tiếng và không để video dài hơn audio
- subprocess capture stdout/stderr UTF-8, nếu exit code khác 0 thì raise lỗi chi tiết

## 11. GUI phải tái tạo

Dùng Tkinter/ttk, window:

- title `Gemini + TTS For Free Video Tool`
- geometry `1180x940`
- minsize `980x760`
- thử theme `vista`

UI có các khu vực:

### API configuration

- Gemini / Google API Key, password field
- fal.ai API Key (FAL_API_KEY), password field
- TTS For Free Endpoint
- TTS API Key (X-API-Key), password field

### Batch configuration

- Image Provider combobox readonly
- Image Model combobox readonly và refresh theo provider
- Image threads / video, Spinbox 1–12
- Run mode: `Sequential` hoặc `Parallel`
- Video threads, Spinbox 1–12
- cảnh báo concurrency tối đa có thể bằng Video threads × Image threads

### Shared prompt and shared reference images

- ScrolledText Shared Prompt
- Add Images, Clear
- label tóm tắt số ảnh/tên ảnh

### Videos in this batch

- vùng canvas cuộn dọc chứa nhiều card
- nút `Add Blank Video`
- luôn phải còn ít nhất một video

Mỗi card `Video N` có:

- Voice combobox cho phép gõ tay
- default voice option chính xác:
  `v1:ZX7_aRS0FzGeqAgWsfVbUP_St5VDMhy9r58iPvgQ-YKffu7Y83VDYXS0`
- Mode readonly: `Short` / `Long`
- `Clone`, `Remove`
- ScrolledText `Video Prompt`
- ScrolledText `Idea / Script`
- `Add Images...`, `Clear`
- checkbox `Use shared images (unchecked = use own images)`
- summary ảnh riêng
- usage/cost label và status riêng

### Actions/output

- `Generate Batch`
- `History`
- status batch
- readonly `Completed Outputs`, nhiều path nối bằng ` ; `
- `Realtime Log` ScrolledText readonly

Log từ worker phải đi qua `queue.Queue`, GUI flush mỗi khoảng 150 ms. Không update widget trực tiếp từ worker; dùng `root.after(...)`. Nút Generate disabled khi chạy và enabled trong `finally`. Worker chính là daemon thread.

Validation trước khi chạy:

- Gemini key luôn bắt buộc
- fal key bắt buộc khi chọn fal.ai
- endpoint TTS bắt buộc
- TTS auth hợp lệ nếu có GUI X-API-Key hoặc env bearer/cookie
- model/provider/mode hợp lệ
- image/video thread là số nguyên dương
- mỗi card có voice, Short/Long mode và idea/script

Batch sequential dùng một video worker. Batch parallel dùng `min(video_threads, số video)`. Bên trong mỗi video, ảnh tiếp tục chạy song song theo image threads. Lỗi một video không làm dừng các video khác. Card chuyển `Queued` → `Running` → `Completed` hoặc `Failed: ...`; cuối batch hiện số thành công/thất bại và dialog phù hợp.

## 12. Settings và history

Settings autosave debounce khoảng 500 ms khi field thay đổi và save khi đóng app. Load lại toàn bộ batch khi mở app. Payload gồm ít nhất:

- `gemini_api_key`
- `fal_api_key`
- `tts_endpoint`
- `tts_api_key`
- legacy `voice`
- `image_provider`
- `image_model_label`
- `parallel_image_requests`
- `batch_mode`
- `max_parallel_videos`
- `shared_prompt`
- `shared_reference_image_paths`
- legacy `reference_image_paths`
- `video_jobs`
- `saved_at`

Mỗi video job lưu voice, mode, video prompt, idea, cờ dùng shared reference và list ảnh riêng.

Lịch sử:

- chỉ append sau khi video hoàn tất
- lưu newest first, dedupe bằng `run_id`, tối đa 100
- lock read-modify-write vì nhiều video có thể hoàn tất đồng thời
- khi load, merge history JSON với các `outputs/*/final_video.mp4` được tự discover
- có thể phục hồi script từ `scenes.json`, fallback đọc SRT

Window `Generation History`:

- list video bên trái, detail/copyable script bên phải
- hiển thị run id, thời gian, paths, voice, mode, provider/model, cost, prompts, reference, usage, plain script và SRT
- buttons `Open Video`, `Open Folder`, `Copy Text`, `Refresh`
- đánh dấu video bị missing

## 13. Trình tự `_run_single_video`

Phải theo thứ tự:

1. Tạo paths/run id.
2. Gemini tạo scenes; nếu lỗi vẫn ghi usage hiện có.
3. Ghi `scenes.json`.
4. Load reference images và chọn shared hoặc video-specific.
5. Tạo ảnh song song.
6. Ghi usage/cost; fallback ảnh lỗi.
7. Ghi `script.srt`.
8. Submit TTS, poll, download audio.
9. Tạo `image_video.mp4`.
10. Merge và burn subtitle thành `final_video.mp4`.
11. Append history.
12. Trả final path và metrics.

Không tự xóa `temp` vì history cần `scenes.json`, SRT và audio để tra cứu.

## 14. Chất lượng, an toàn và tiêu chí nghiệm thu

- Code chạy được, không pseudo-code, không placeholder endpoint schema.
- Dùng `pathlib`, UTF-8 và `ensure_ascii=False` để giữ tiếng Việt.
- Không log API key/token.
- Giữ mọi network/subprocess dài ngoài Tk main thread.
- Mỗi video có client/token tracker riêng; Gemini image client theo từng image worker thread.
- API key được settings hiện tại lưu dạng plain text để tái tạo đúng hành vi; phải cảnh báo người dùng không commit file settings/history.
- Xử lý path Windows chứa space, dấu nháy và ký tự Unicode.
- Partial image failure có fallback; partial video failure không hủy batch.
- Settings/history JSON hỏng không được làm app crash lúc mở; dùng default và log warning.
- Đảm bảo `if __name__ == "__main__": main()`.

Trước khi bàn giao, hãy:

1. chạy compile/syntax check;
2. rà soát import và tên method của `google-genai`;
3. xác nhận GUI vẫn responsive;
4. xác nhận mọi file/folder đúng tên;
5. xác nhận TTS payload và AES-GCM packing đúng thứ tự;
6. xác nhận FFmpeg lấy duration cuối theo audio;
7. xác nhận cả hai provider và cả Short/Long mode đều có code path thật.

Cuối câu trả lời, viết hướng dẫn sử dụng gồm:

- lệnh tạo virtual environment và pip install trên Windows PowerShell;
- cách cài FFmpeg và kiểm tra `ffmpeg -version`, `ffprobe -version`;
- cách lấy/nhập Gemini key, fal key và TTS auth;
- danh sách env tùy chọn;
- cách chạy `python gemini_ttsforfree_video_tool.py`;
- quy trình thao tác trên GUI;
- giải thích `outputs`, `temp`, settings, history và token usage;
- troubleshooting cho thiếu FFmpeg, 401/403 TTS, 429/quota, model không tồn tại, fal timeout, subtitle filter lỗi và voice token lỗi.
~~~

## English version — copy this prompt

~~~text
You are a Senior Python Desktop/Application Engineer. Build a complete Windows desktop application named “Gemini + TTS For Free Video Tool” that reproduces the specification below. Do not create a demo, use mocks, leave TODOs, or omit features. The application must make real API calls, generate images and speech, invoke FFmpeg, and produce an MP4 video.

While implementing:

1. If you can edit a workspace, create the main file `gemini_ttsforfree_video_tool.py`. All runtime logic must live in this one Python file.
2. If you can only answer in chat, return the entire Python file in one code block.
3. After the code, provide setup and usage instructions covering APIs, FFmpeg, outputs, and common failures.
4. Do not ask questions whose answers are already in this specification. Complete the implementation, run a syntax/import check, and fix issues before handing it over.
5. Use Python 3.11+, type hints, clear classes/functions, robust error handling, and keep the GUI responsive during long-running work.

## 1. Required goal and pipeline

This is a single-file Tkinter application designed primarily for Windows. It accepts one or more ideas/scripts in a batch and performs this real pipeline:

1. Gemini text converts an idea or script into multi-scene JSON.
2. Generate exactly one image for each scene using the selected Gemini or fal.ai image model.
3. Write `scenes.json` and `script.srt`.
4. Send SRT cues through the real TTS For Free flow:
   - `POST /api/TTS/CreateSrt`
   - `GET /api/TTS/Multi/Status/{job_id}`
5. Poll the job, obtain the audio URL, and download the audio.
6. Use FFmpeg to create a video from the images.
7. merge video and audio, burn subtitles, and produce `final_video.mp4`.
8. Save settings, history, token/cost usage, and show realtime logs.

Gemini always plans scenes, even when fal.ai is selected for image generation.

## 2. Technology and dependencies

Use only the standard library plus:

- `requests`
- `google-genai`
- `cryptography`
- `Pillow` (may be needed by the image SDK)
- the Tkinter bundled with Python
- external executables `ffmpeg` and `ffprobe` available on `PATH`

Put this installation note at the top of the file:

`pip install requests google-genai cryptography Pillow`

Before running a generation pipeline, validate runtime packages and both `ffmpeg` and `ffprobe`, with actionable errors.

The working directory may contain a neighboring local file named `copy.py`. Before importing third-party packages, implement `_load_stdlib_copy_before_local_modules()`: temporarily remove the script directory from `sys.path`, import Python’s standard-library `copy` module, and restore `sys.path`. This prevents the local file from shadowing the standard library.

## 3. Constants, paths, and environment variables

Use the directory containing the Python script as the root for:

- `outputs/`
- `temp/`
- `gemini_ttsforfree_video_tool.settings.json`
- `gemini_ttsforfree_video_tool.history.json`
- keep at most 100 history entries and protect history read-modify-write with a `threading.Lock`

Each video run gets an id formatted as `YYYYMMDD_HHMMSS_<8 uuid hex characters>`.

File layout:

- `temp/<run_id>/scenes.json`
- `temp/<run_id>/script.srt`
- `temp/<run_id>/images/scene_001.png`, `scene_002.png`, ...
- `temp/<run_id>/images_concat.txt`
- `temp/<run_id>/image_video.mp4`
- `temp/<run_id>/tts_audio.mp3`
- `outputs/<run_id>/final_video.mp4`
- `outputs/<run_id>/token_usage.json`

Use these exact defaults and environment variables:

- `GEMINI_TEXT_MODEL`, default `gemini-2.5-flash`
- `TTSFORFREE_ENDPOINT`, default `https://api.ttsforfree.com`
- `TTSFORFREE_API_KEY`
- `TTSFORFREE_BEARER_TOKEN`
- `TTSFORFREE_COOKIE`
- `TTSFORFREE_EXTRA_HEADERS`: a JSON-object string
- `TTSFORFREE_SPEED`, default `0.9`
- `TTSFORFREE_PITCH`, default `0`
- `TTSFORFREE_VOLUME`, default `1`
- `FAL_API_KEY`, falling back to `FAL_KEY`
- `FAL_REQUEST_TIMEOUT_SECONDS`, default `300`
- `IMAGE_GENERATION_MAX_WORKERS`, default string `10`; if parsing fails, fall back to `3`

Operational constants:

- poll TTS every 5 seconds for up to 20 minutes
- general HTTP timeout 60 seconds
- stream downloads in 512 KiB chunks
- Gemini retry delays: 10, 30, 60 seconds
- fal.ai retry delays: 5, 15, 30 seconds
- output video 1920x1080 at 30 FPS

## 4. Image provider/model registry

Create an extensible provider design using abstract base class `ImageGenerationProvider`, implementations `GeminiImageProvider` and `FalImageProvider`, `IMAGE_PROVIDER_CLASSES`, and `create_image_provider(...)`. Keep provider/model data outside the GUI.

The registry must contain exactly:

1. Provider id `gemini`, label `Gemini`
   - label `Gemini 3.1 Flash Lite Image`
     - id `gemini-3.1-flash-lite-image`
     - family `gemini_image`
     - supports reference images
     - input/output price per one million tokens: 0.25 / 30.00
   - label `Gemini 3.1 Flash Image`
     - id `gemini-3.1-flash-image`
     - family `gemini_image`
     - supports reference images
     - price: 0.50 / 60.00
   - label `Gemini 3 Pro Image`
     - id `gemini-3-pro-image`
     - family `gemini_image`
     - supports reference images
     - price: 2.00 / 120.00

2. Provider id `fal`, label `fal.ai`
   - label `FLUX.1 Dev`
     - id and endpoint `fal-ai/flux/dev`
     - family `flux`
     - no reference-image support
     - request defaults: `image_size=landscape_16_9`, `num_images=1`, `output_format=png`
     - default price 0.025 USD/image
   - label `FLUX.1 Kontext`
     - id `fal-ai/flux-pro/kontext`
     - text-to-image endpoint `fal-ai/flux-pro/kontext/text-to-image`
     - reference endpoint `fal-ai/flux-pro/kontext/multi`
     - reference field `image_urls`
     - family `flux_kontext`
     - supports reference images
     - request defaults: `aspect_ratio=16:9`, `num_images=1`, `output_format=png`
     - default price 0.04 USD/image

Allow price overrides through matching environment variables:

- `GEMINI_TEXT_INPUT_COST_PER_MILLION`, default 0.30
- `GEMINI_TEXT_OUTPUT_COST_PER_MILLION`, default 2.50
- `GEMINI_FLASH_LITE_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_FLASH_LITE_IMAGE_OUTPUT_COST_PER_MILLION`
- `GEMINI_FLASH_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_FLASH_IMAGE_OUTPUT_COST_PER_MILLION`
- `GEMINI_PRO_IMAGE_INPUT_COST_PER_MILLION`
- `GEMINI_PRO_IMAGE_OUTPUT_COST_PER_MILLION`
- `FAL_FLUX_DEV_COST_PER_IMAGE`
- `FAL_FLUX_KONTEXT_COST_PER_IMAGE`

Migrate these three legacy labels:

- `Gemini 3.1 Flash-Lite Image | Fastest, lowest cost`
- `Gemini 3.1 Flash Image | Balanced, supports higher resolution`
- `Gemini 3 Pro Image | Highest quality, complex detail`

to their corresponding current Gemini labels.

Do not silently replace model ids. If a model is unavailable, give a clear error and leave the registry/env configurable.

## 5. Required data models and helpers

Implement:

- `Scene(sceneIndex, narrationText, imagePrompt, durationSeconds)` with `to_dict()`
- `ReferenceImage(path, mime_type, data)`
- `TokenUsageTracker`
- `VideoJobRequest`
- `VideoJobCard`
- exceptions `RetryableError`, `ImageGenerationNoOutputError`, and `TtsJobFailedError`

Provide equivalent helpers for:

- positive/default/nonnegative number parsing and float env parsing
- `format_srt_timestamp`
- `estimate_duration`
- `write_srt`
- parsing/loading SRT cues
- `run_ffmpeg`
- `probe_media_duration_seconds`
- `download_file`
- `retry_with_backoff`
- normalizing/loading/summarizing reference images
- parsing/repairing Gemini JSON
- reading/opening paths and managing history

`estimate_duration(text)` must:

- normalize whitespace
- return 3 for empty text
- calculate base seconds as word count / 2.6 + count of `[,.!?;:]` * 0.12
- round up and enforce a minimum of 3 seconds

`write_srt` uses an integer duration of at least 1, cumulative non-overlapping timestamps, and UTF-8.

`normalize_scenes` accepts either an array or an object with a `scenes` array; skips non-object items and entries missing `narrationText`/`imagePrompt`; renumbers scenes sequentially from 1; uses `estimate_duration` for invalid durations; and raises if no valid scene remains.

## 6. Gemini scene planning

`GeminiVideoClient` uses `genai.Client(api_key=...)`.

Each video has mode `Short` or `Long`.

The planning prompt must demand JSON only, with exactly four keys per scene:

- `sceneIndex`
- `narrationText`
- `imagePrompt`
- `durationSeconds`

Shared rules:

- natural conversational narration suitable for AI TTS
- imagePrompt describes exactly what is visible
- each scene is visually different from its predecessor
- smooth, emotional, coherent progression
- the shared prompt applies to every video in the batch
- the per-video prompt applies only to that video
- supplemental prompts must never alter the required JSON schema

Short mode:

- role: “senior short-form video script planner”
- normally 6–12 scenes
- one key moment/action per scene
- preferably one short narration sentence
- normally 8–14 words, never over 16 words
- split long sentences into more scenes
- roughly 2–4 seconds of speech
- duration generally 2–4 seconds
- avoid unnecessary detail and complex sentence structures
- treat input as an idea/script
- `max_output_tokens=None`

Long mode:

- role: “senior long-form video script and scene planner”
- treat input as a completed long-form narration script
- preserve all meaning, order, facts, examples, and important details
- never summarize, aggressively shorten, or omit sections
- retain original narration wording whenever possible
- combined `narrationText` values cover the whole script from start to finish; light punctuation cleanup is allowed
- use as many scenes as necessary, normally 20–120 but no hard limit
- each scene is one coherent visual beat
- narration has 1–3 sentences, usually 15–40 words and never above 60
- split long paragraphs across consecutive scenes without content loss
- duration usually 5–15 seconds
- preserve continuity for recurring people, places, objects, and style
- `max_output_tokens=65536`

Call `generate_content` using the text model, temperature 0.4, and `response_mime_type="application/json"`.

Read text from `response.text`, with fallback to `candidates[].content.parts[].text`.

Make JSON parsing resilient:

- strip Markdown code fences
- extract a JSON object/array embedded in surrounding text
- repair smart quotes
- quote common unquoted scene keys
- remove trailing commas and common JS comments
- use `ast.literal_eval` as a final local fallback for Python-like single-quoted output
- if it still raises `JSONDecodeError`, make exactly one Gemini JSON repair request at temperature 0 and JSON MIME, explicitly forbidding translation, summarization, or Markdown

## 7. Image generation

The Gemini image prompt is the scene prompt plus the exact intent:

“Create exactly one finished image for this scene. Cinematic, YouTube educational, clean professional visual style, landscape 16:9 composition, highly detailed, natural lighting, no text overlay, no watermark.”

For Gemini image models:

- use `threading.local()` so every image worker has its own `genai.Client`
- call `models.generate_content`
- set `response_modalities=["IMAGE"]`
- set `types.ImageConfig(aspect_ratio="16:9")`
- extract the first image using part `as_image()` or `inline_data.as_image()`
- save both image objects exposing `.save()` and raw `.data` bytes
- if the model returns only text or no image, raise `ImageGenerationNoOutputError`
- retry 429/rate-limit/quota/503/504/timeout/internal-server errors and retryable exceptions
- retain a future Imagen `generate_images` branch configured for one 16:9 PNG image

With Gemini reference images, contents include the text prompt and every image via `types.Part.from_bytes`, along with each filename. Use references only as guidance for product UI, layout language, colors, components, and identity; never copy private data or exact sensitive text.

The base fal.ai prompt adds:

- Ultra realistic
- Cinematic
- Highly detailed
- Natural lighting
- Professional YouTube style
- 16:9
- No text
- No watermark

Call fal.ai directly:

- `POST https://fal.run/{endpoint}`
- header `Authorization: Key <FAL_API_KEY>`
- JSON request defaults plus `prompt`
- read `payload.images[0].url` and download it
- retry network timeout/connection failures, HTTP 429, and 5xx

For FLUX.1 Kontext with references:

- use the `/multi` endpoint
- encode each image as `data:<mime>;base64,...`
- send them in `image_urls`
- add instructions to preserve character identity, face, hairstyle, and clothing, changing only the environment to match the scene

Reference images support `.png`, `.jpg`, `.jpeg`, and `.webp`. Resolve paths and remove nonexistent, unsupported, and duplicate files. If a selected model does not support references, log that they are ignored.

The effective prompt for each scene contains:

1. `scene.imagePrompt`
2. `Shared visual instructions: <shared prompt>` when present
3. `Video-specific visual instructions: <video prompt>` when present

Generate scene images concurrently with `ThreadPoolExecutor`, using min(image threads, scene count). Preserve scene order in files and output.

If some images fail but at least one succeeds, copy the nearest successful image as the failed scene’s fallback; at equal distance prefer the preceding scene, then the following scene. If all image requests fail, abort that video and report the first error.

## 8. Token and cost tracking

`TokenUsageTracker` is thread-safe and tracks per video:

- input tokens
- output/candidate tokens
- reported total tokens
- thoughts tokens
- cached-content tokens
- API request count
- requests with metadata
- requests missing token counts
- operation-level breakdown, such as `scene_script_short`, `scene_script_long`, `scene_json_repair`, `gemini_image`, and `imagen_image`

Read usage metadata from objects or dictionaries in snake_case or camelCase. An API attempt that throws before returning a response still counts as an unreported request.

For Gemini, calculate text operations at Gemini text pricing and `gemini_image` operations at the selected image model pricing. For fal.ai, cost is successful image count multiplied by cost per image. Write full metadata to `token_usage.json` and show per-card summaries:

- Gemini: `Input Tokens: ... | Output Tokens: ... | Estimated Cost ($): ...`
- fal.ai: `Images Generated: ... | Estimated Cost ($): ...`

## 9. Real TTS For Free integration

Implement `TtsForFreeClient` with:

- `submit_srt_job(srt_path, voice)`
- `poll_until_completed(job_id)`
- `download_audio(audio_url, output_path=None)`

Each instance owns a `requests.Session()` and a unique `connection_id = uuid.uuid4()`.

Normalize the endpoint:

- trim trailing `/`
- remove terminal `/docs`, `/swagger`, `/swagger/index.html`, or `/swagger/`
- if the user supplied the full `/api/TTS/CreateSrt` path, use it
- otherwise append `/api/TTS/CreateSrt`
- retain the base URL for polling

Voice tokens:

- reject empty values
- pass through values beginning with `v1:`
- encrypt a plain voice string using the backend-compatible algorithm:
  - exact Base64 key: `K1hXK1B4K0pMaHBDaHh6WXh1R2J4S3ZBbkJoQ2l4eWw=`
  - Base64-decode the key
  - generate a random 12-byte nonce
  - AES-GCM with no associated data
  - the library returns `cipher || tag_16`; separate the tag
  - pack `nonce || tag || cipher`
  - URL-safe Base64-encode and strip `=`
  - prefix with `v1:`

Parse SRT into `start_ms`, `end_ms`, and `text`, supporting blocks with or without numeric index lines.

The `POST /api/TTS/CreateSrt` body is:

{
  "connectionId": "<uuid>",
  "blocks": [
    {
      "texts": "<cue text>",
      "voice": "<encrypted voice>",
      "speed": 0.9,
      "pitch": 0,
      "volume": 1,
      "startMs": 0,
      "endMs": 3000
    }
  ]
}

Default headers:

- `Accept: application/json`
- `Content-Type: application/json`
- `User-Agent: GeminiTtsForFreeTool/1.0`
- when the GUI API key is present: `X-API-Key`
- when bearer env is present: `Authorization: Bearer ...`
- when cookie env is present: `Cookie: ...`
- merge the object from `TTSFORFREE_EXTRA_HEADERS`

Read response keys case-insensitively. If `id == -1` or status is `error/failed`, raise with the server `message`. Read the job id from `jobId` or `id`.

Polling:

- URL `/api/TTS/Multi/Status/{job_id}`
- every 5 seconds, for no more than 20 minutes
- success statuses: `success`, `completed`
- failure statuses: `error`, `failed`
- the audio URL can be in `audioUrl`, `data`, `url`, or `expectUrl`
- completed without an URL is an error
- log all other statuses as pending

Download using the same session and headers with streaming. Default to `.mp3`, while inferring an extension from the URL when possible.

## 10. FFmpeg

Implement `VideoAssembler`.

Image video:

- create a concat-demuxer manifest with `file` and `duration` for every image; repeat the last image at the end
- safely escape paths, including Windows paths
- filter: `fps=30,scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p`
- codec `libx264`, pixel format `yuv420p`

Final merge:

- use ffprobe to obtain image-video and audio durations
- target duration is exactly the audio duration
- when audio exceeds video by over 0.01 seconds, use `tpad=stop_mode=clone:stop_duration=<difference>` to hold the last frame
- burn SRT with `subtitles='<escaped absolute path>'`
- follow with `format=yuv420p`
- video `libx264`, `preset veryfast`, `crf 23`
- audio `aac`, bitrate `192k`
- use `-t <audio_duration>` so speech is never cut and video does not outlast audio
- capture subprocess stdout/stderr as UTF-8 and raise the detailed error for nonzero exit status

## 11. GUI reproduction

Use Tkinter/ttk:

- title `Gemini + TTS For Free Video Tool`
- geometry `1180x940`
- minimum size `980x760`
- attempt to use the `vista` theme

Required sections:

### API configuration

- password field: Gemini / Google API Key
- password field: fal.ai API Key (FAL_API_KEY)
- TTS For Free Endpoint
- password field: TTS API Key (X-API-Key)

### Batch configuration

- readonly Image Provider combobox
- readonly Image Model combobox that refreshes for the provider
- Image threads / video, Spinbox 1–12
- Run mode: `Sequential` or `Parallel`
- Video threads, Spinbox 1–12
- explain that maximum concurrency may equal Video threads × Image threads

### Shared prompt and shared reference images

- Shared Prompt ScrolledText
- Add Images and Clear buttons
- selected-image summary

### Videos in this batch

- vertically scrollable Canvas containing multiple cards
- `Add Blank Video`
- at least one card must always remain

Each `Video N` card contains:

- editable Voice combobox
- exact default voice option:
  `v1:ZX7_aRS0FzGeqAgWsfVbUP_St5VDMhy9r58iPvgQ-YKffu7Y83VDYXS0`
- readonly Mode: `Short` / `Long`
- `Clone` and `Remove`
- `Video Prompt` ScrolledText
- `Idea / Script` ScrolledText
- `Add Images...` and `Clear`
- checkbox `Use shared images (unchecked = use own images)`
- per-video reference summary
- per-video usage/cost label and status

### Actions/output

- `Generate Batch`
- `History`
- batch status
- readonly `Completed Outputs`, joining multiple paths with ` ; `
- readonly `Realtime Log` ScrolledText

Worker logs go through `queue.Queue`; flush the GUI about every 150 ms. Never update widgets directly from workers; schedule with `root.after(...)`. Disable Generate while running and re-enable it in `finally`. The main worker is a daemon thread.

Validate before running:

- Gemini key is always required
- fal key is required when fal.ai is selected
- TTS endpoint is required
- TTS auth is valid when a GUI X-API-Key or bearer/cookie env exists
- provider/model/mode selections are valid
- image/video threads are positive integers
- every card has voice, a valid Short/Long mode, and idea/script

Sequential mode uses one video worker. Parallel mode uses min(video threads, video count). Each video also generates images concurrently according to image threads. One failed video must not cancel other videos. Card statuses transition `Queued` → `Running` → `Completed` or `Failed: ...`; final UI/dialog reports successful and failed counts.

## 12. Settings and history

Autosave settings with about a 500 ms debounce and save on close. Restore the entire batch at startup. Store at least:

- `gemini_api_key`
- `fal_api_key`
- `tts_endpoint`
- `tts_api_key`
- legacy `voice`
- `image_provider`
- `image_model_label`
- `parallel_image_requests`
- `batch_mode`
- `max_parallel_videos`
- `shared_prompt`
- `shared_reference_image_paths`
- legacy `reference_image_paths`
- `video_jobs`
- `saved_at`

Each job stores voice, mode, video prompt, idea, whether it uses shared references, and its own image list.

History behavior:

- append only after successful video completion
- newest first, deduplicate by `run_id`, maximum 100
- lock read-modify-write because parallel videos may finish together
- merge history JSON with auto-discovered `outputs/*/final_video.mp4`
- recover script from `scenes.json`, falling back to SRT

The `Generation History` window has:

- video list on the left and details/copyable script on the right
- run id, time, paths, voice, mode, provider/model, cost, prompts, references, usage, plain script, and SRT
- `Open Video`, `Open Folder`, `Copy Text`, `Refresh`
- mark missing videos

## 13. Required `_run_single_video` order

Use this exact high-level sequence:

1. Create run id and paths.
2. Generate scenes with Gemini; save any current usage even if this fails.
3. Write `scenes.json`.
4. Load shared or video-specific reference images.
5. Generate images concurrently.
6. Save usage/cost and fill failed images from nearest successful images.
7. Write `script.srt`.
8. Submit/poll TTS and download audio.
9. Create `image_video.mp4`.
10. Merge and burn subtitles into `final_video.mp4`.
11. Append history.
12. Return final path and metrics.

Do not automatically delete `temp`; history relies on `scenes.json`, SRT, and audio for later inspection.

## 14. Quality, safety, and acceptance criteria

- Deliver executable code, not pseudocode or placeholder API schemas.
- Use `pathlib`, UTF-8, and `ensure_ascii=False` to preserve multilingual text.
- Never log API keys or tokens.
- Keep every long network/subprocess operation outside the Tk main thread.
- Use a separate client/token tracker per video and a thread-local Gemini image client per image worker.
- To match existing behavior, settings store API keys as plain text; clearly warn users never to commit settings/history and to use the app only on a trusted machine.
- Handle Windows paths containing spaces, quotes, and Unicode.
- Partial image failure uses fallback; partial video failure does not cancel the batch.
- Corrupt settings/history JSON must not crash startup; use defaults and log a warning.
- Include `if __name__ == "__main__": main()`.

Before delivery:

1. run a compile/syntax check;
2. review imports and current `google-genai` method names;
3. confirm the GUI remains responsive;
4. confirm every file/folder name;
5. confirm the TTS payload and AES-GCM packing order;
6. confirm FFmpeg output duration follows the audio;
7. confirm both providers and both Short/Long modes have real code paths.

End with a usage guide covering:

- Windows PowerShell virtual-environment and pip-install commands;
- FFmpeg installation and `ffmpeg -version` / `ffprobe -version` checks;
- obtaining/entering Gemini, fal.ai, and TTS credentials;
- optional environment variables;
- running `python gemini_ttsforfree_video_tool.py`;
- the GUI workflow;
- `outputs`, `temp`, settings, history, and token-usage files;
- troubleshooting missing FFmpeg, TTS 401/403, 429/quota, missing model, fal timeout, subtitle-filter errors, and voice-token errors.
~~~

