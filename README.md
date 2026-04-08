# 🚦 Traffic Bot Telegram

Bot Telegram kiểm tra tắc đường từ điểm A đến B, dùng Google Maps Directions API.

---

## 🛠 Cài đặt

### Bước 1 — Tạo bot Telegram

1. Mở Telegram, tìm **@BotFather**
2. Gõ `/newbot` → đặt tên bot → đặt username (phải kết thúc bằng `bot`)
3. Copy **token** nhận được (dạng `123456789:ABCdef...`)

### Bước 2 — Lấy Google Maps API Key

1. Vào [console.cloud.google.com](https://console.cloud.google.com)
2. Tạo project mới (hoặc dùng project có sẵn)
3. Vào **APIs & Services → Library** → tìm và bật **Directions API**
4. Vào **APIs & Services → Credentials → Create Credentials → API Key**
5. Copy API key

> 💡 Google cho $200 credit/tháng miễn phí. Directions API tốn ~$5/1000 request.
> Dùng cá nhân gần như không bao giờ hết free tier.

### Bước 3 — Cài đặt và chạy

```bash
# Clone hoặc copy folder traffic_bot về máy

# Tạo môi trường ảo (khuyến nghị)
python -m venv venv
source venv/bin/activate      # macOS/Linux
# hoặc: venv\Scripts\activate  # Windows

# Cài thư viện
pip install -r requirements.txt

# Tạo file .env
cp .env.example .env
# Mở .env và điền token vào

# Chạy bot
python bot.py
```

---

## 📁 Cấu trúc file

```
traffic_bot/
├── bot.py          # Logic bot Telegram (conversation flow)
├── traffic.py      # Gọi Google Maps API, tính mức độ tắc
├── requirements.txt
├── .env.example    # Mẫu biến môi trường
└── .env            # File thật (KHÔNG commit lên git)
```

---

## 🤖 Cách dùng bot

| Lệnh | Chức năng |
|------|-----------|
| `/start` | Chào mừng |
| `/check` | Bắt đầu kiểm tra tuyến đường |
| `/help` | Xem hướng dẫn |
| `/cancel` | Hủy thao tác |

**Luồng kiểm tra:**
1. Gõ `/check`
2. Bot hỏi điểm A → nhập tên hoặc địa chỉ (hoặc chia sẻ GPS)
3. Bot hỏi điểm B → nhập tên hoặc địa chỉ
4. Bot trả kết quả: 🟢 Thông thoáng / 🟡 Chậm / 🔴 Tắc đường

---

## 🚀 Deploy lên Railway (free, chạy 24/7)

1. Tạo tài khoản tại [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo (hoặc upload thẳng)
3. Vào **Variables** → thêm `TELEGRAM_TOKEN` và `GOOGLE_MAPS_KEY`
4. Railway tự động chạy `python bot.py`

> Ngoài ra có thể dùng Render.com hoặc Fly.io — cũng có free tier.

---

## Báo cáo tự động mỗi ngày (17h giờ Việt Nam)

1. Trong bot, gõ `/myid` và copy **Chat ID**.
2. Trong `.env` (hoặc biến môi trường trên server) thêm:

   - `SCHEDULE_CHAT_ID` — Chat ID vừa copy  
   - `SCHEDULE_ORIGIN` — điểm A (địa chỉ hoặc `lat,lng`)  
   - `SCHEDULE_DESTINATION` — điểm B  

3. Tuỳ chọn: `SCHEDULE_HOUR` (mặc định `17`), `SCHEDULE_MINUTE` (mặc định `0`). Múi giờ cố định `Asia/Ho_Chi_Minh`.

Nếu thiếu một trong ba biến đầu, bot vẫn chạy bình thường nhưng **không** gửi lịch.

---

## ⚙️ Tuỳ chỉnh ngưỡng tắc đường

Trong `traffic.py`, hàm `classify_traffic()`:

```python
if ratio < 1.2:    # < 120% → thông thoáng (có thể chỉnh lên 1.3)
    return "green"
elif ratio < 1.6:  # 120–160% → chậm
    return "yellow"
else:              # > 160% → tắc
    return "red"
```

Chỉnh các ngưỡng này tuỳ theo cảm nhận thực tế của bạn.
