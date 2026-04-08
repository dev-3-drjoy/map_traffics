import os
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton

load_dotenv()  # load biến từ file .env khi chạy local
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from traffic import check_traffic

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SCHEDULE_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Conversation states
WAITING_ORIGIN, WAITING_DESTINATION = range(2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot kiểm tra tắc đường.\n\n"
        "Dùng lệnh /check để bắt đầu kiểm tra tuyến đường.\n"
        "Hoặc gõ /help để xem hướng dẫn."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    await update.message.reply_text(
        f"Chat ID của bạn: `{chat.id}`\n\n"
        "Dán vào `SCHEDULE_CHAT_ID` trong `.env` nếu bật báo cáo 17h.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Hướng dẫn sử dụng:*\n\n"
        "/check — Kiểm tra tắc đường từ A đến B\n"
        "/myid — Xem Chat ID (cấu hình báo cáo định kỳ trong `.env`)\n"
        "/scheduletest — Test ngay báo cáo tuyến cố định (vào chat hiện tại)\n"
        "/cancel — Hủy thao tác hiện tại\n\n"
        "💡 *Mẹo nhập địa chỉ:*\n"
        "• Có thể nhập tên địa danh: `Hồ Hoàn Kiếm`\n"
        "• Hoặc địa chỉ đầy đủ: `123 Cầu Giấy, Hà Nội`\n"
        "• Hoặc chia sẻ vị trí GPS trực tiếp 📍",
        parse_mode="Markdown"
    )


async def check_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location_button = KeyboardButton("📍 Dùng vị trí hiện tại", request_location=True)
    keyboard = ReplyKeyboardMarkup(
        [[location_button]], resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "📍 *Điểm xuất phát (A) là đâu?*\n\n"
        "Bạn có thể:\n"
        "• Gõ tên địa điểm hoặc địa chỉ\n"
        "• Hoặc bấm nút bên dưới để dùng vị trí hiện tại",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return WAITING_ORIGIN


async def receive_origin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        loc = update.message.location
        context.user_data["origin"] = f"{loc.latitude},{loc.longitude}"
        origin_display = f"📍 GPS ({loc.latitude:.4f}, {loc.longitude:.4f})"
    else:
        context.user_data["origin"] = update.message.text
        origin_display = update.message.text

    context.user_data["origin_display"] = origin_display

    from telegram import ReplyKeyboardRemove
    await update.message.reply_text(
        f"✅ Điểm A: *{origin_display}*\n\n"
        "🏁 *Điểm đến (B) là đâu?*\n"
        "Gõ tên địa điểm hoặc địa chỉ:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_DESTINATION


async def receive_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    destination = update.message.text
    origin = context.user_data.get("origin")
    origin_display = context.user_data.get("origin_display", origin)

    await update.message.reply_text("⏳ Đang kiểm tra giao thông, chờ mình một chút...")

    try:
        result = check_traffic(origin, destination)
        msg = format_result(result, origin_display, destination)
    except Exception as e:
        logger.error(f"Traffic check error: {e}")
        msg = "❌ Có lỗi xảy ra khi kiểm tra. Vui lòng thử lại hoặc kiểm tra lại tên địa điểm."

    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    return ConversationHandler.END


def format_result(result: dict, origin: str, destination: str) -> str:
    status_icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(result["status"], "⚪")
    status_text = {
        "green": "Thông thoáng",
        "yellow": "Có chậm",
        "red": "Tắc đường"
    }.get(result["status"], "Không rõ")

    normal_min = result["duration_normal"] // 60
    traffic_min = result["duration_traffic"] // 60
    delay_min = traffic_min - normal_min
    distance_km = result["distance"] / 1000

    lines = [
        f"{status_icon} *{status_text}*",
        f"",
        f"🗺 *Tuyến đường:*",
        f"  `{origin}`",
        f"  ↓",
        f"  `{destination}`",
        f"",
        f"📏 Khoảng cách: *{distance_km:.1f} km*",
        f"⏱ Thời gian bình thường: *{normal_min} phút*",
        f"🚦 Thời gian thực tế: *{traffic_min} phút*",
    ]

    if delay_min > 0:
        lines.append(f"⚠️ Chậm hơn bình thường: *+{delay_min} phút*")

    if result.get("summary"):
        lines.append(f"")
        lines.append(f"🛣 Đi qua: _{result['summary']}_")

    # ── Danh sách đoạn tắc / chậm ─────────────────────────────────────────────
    segments = result.get("congested_segments", [])
    if segments:
        lines.append(f"")
        lines.append(f"📍 *Các đoạn cần chú ý:*")
        for i, seg in enumerate(segments, 1):
            seg_icon  = "🔴" if seg["status"] == "red" else "🟡"
            seg_label = "Tắc" if seg["status"] == "red" else "Chậm"
            delay_m   = seg["delay_sec"] // 60
            dist_km   = seg["distance_m"] / 1000
            delay_str = f"+{delay_m} phút" if delay_m >= 1 else f"+{seg['delay_sec']} giây"

            mid = seg.get("midpoint") or seg["start"]
            mlat, mlng = mid[0], mid[1]
            maps_link = f"https://maps.google.com/?q={mlat:.6f},{mlng:.6f}"

            slat, slng = seg["start"][0], seg["start"][1]
            elat, elng = seg["end"][0], seg["end"][1]
            dir_link = (
                f"https://www.google.com/maps/dir/"
                f"{slat:.6f},{slng:.6f}/{elat:.6f},{elng:.6f}"
            )

            road = (seg.get("road_name") or "").strip()
            fallback = seg.get("instruction", "").strip()
            place_line = road if road else fallback

            lines.append(
                f"{seg_icon} *Đoạn {i}* — {seg_label} · {dist_km:.1f} km · chậm {delay_str}\n"
                f"   [📌 Ghim giữa đoạn]({maps_link}) · [🧭 Chỉ đường đoạn]({dir_link})"
            )
            if place_line:
                lines.append(f"   🛣 _{place_line}_")
    else:
        if result["status"] == "green":
            lines.append(f"")
            lines.append(f"✅ Không có đoạn tắc nào trên tuyến này")

    lines.append(f"")
    lines.append(f"_Cập nhật lúc: {result['timestamp']}_")
    lines.append("Powered by Google")

    return "\n".join(lines)


async def _deliver_fixed_route_report(
    application: Application,
    chat_id: int,
    header: str,
) -> None:
    """Gọi check_traffic theo SCHEDULE_ORIGIN/DESTINATION và gửi tin."""
    origin = os.getenv("SCHEDULE_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE_DESTINATION", "").strip()
    if not origin or not destination:
        raise ValueError("Thiếu SCHEDULE_ORIGIN hoặc SCHEDULE_DESTINATION")

    result = check_traffic(origin, destination)
    msg = header + format_result(result, origin, destination)
    await application.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def _send_scheduled_report(application: Application) -> None:
    chat_raw = os.getenv("SCHEDULE_CHAT_ID", "").strip()
    if not chat_raw:
        return
    try:
        chat_id = int(chat_raw)
    except ValueError:
        logger.error("SCHEDULE_CHAT_ID phải là số nguyên")
        return

    header = "⏰ *Báo cáo tự động (giờ Việt Nam)*\n\n"
    try:
        await _deliver_fixed_route_report(application, chat_id, header)
    except Exception as e:
        logger.exception("Lỗi báo cáo định kỳ: %s", e)
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Lỗi báo cáo định kỳ: {e}",
            )
        except Exception:
            pass


async def scheduletest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạm thời: chạy cùng logic job định kỳ, gửi vào chat hiện tại."""
    chat = update.effective_chat
    if not chat or not update.message:
        return
    origin = os.getenv("SCHEDULE_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE_DESTINATION", "").strip()
    if not origin or not destination:
        await update.message.reply_text(
            "Cần `SCHEDULE_ORIGIN` và `SCHEDULE_DESTINATION` trong `.env`.",
            parse_mode="Markdown",
        )
        return
    header = "🧪 *Test báo cáo định kỳ*\n\n"
    try:
        await _deliver_fixed_route_report(context.application, chat.id, header)
    except Exception as e:
        logger.exception("scheduletest: %s", e)
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def _post_init_schedule(application: Application) -> None:
    origin = os.getenv("SCHEDULE_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE_DESTINATION", "").strip()
    chat_raw = os.getenv("SCHEDULE_CHAT_ID", "").strip()
    if not origin or not destination or not chat_raw:
        logger.info(
            "Báo cáo định kỳ tắt (thiếu SCHEDULE_ORIGIN / SCHEDULE_DESTINATION / SCHEDULE_CHAT_ID)."
        )
        return

    try:
        hour = int(os.getenv("SCHEDULE_HOUR", "17"))
        minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    except ValueError:
        hour, minute = 17, 0

    scheduler = AsyncIOScheduler(timezone=SCHEDULE_TZ)
    scheduler.add_job(
        _send_scheduled_report,
        CronTrigger(hour=hour, minute=minute, timezone=SCHEDULE_TZ),
        args=[application],
        id="daily_traffic_report",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Đã bật báo cáo định kỳ lúc %02d:%02d (%s)",
        hour,
        minute,
        SCHEDULE_TZ.key,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardRemove
    await update.message.reply_text(
        "❌ Đã hủy. Gõ /check để bắt đầu lại.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mình không hiểu lệnh này. Gõ /help để xem hướng dẫn."
    )


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("Thiếu TELEGRAM_TOKEN trong biến môi trường!")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init_schedule)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("check", check_start)],
        states={
            WAITING_ORIGIN: [
                MessageHandler(filters.LOCATION, receive_origin),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_origin),
            ],
            WAITING_DESTINATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_destination),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("scheduletest", scheduletest))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot đang chạy...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()