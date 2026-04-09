import io
import os
import logging
import secrets
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()  # load biến từ file .env khi chạy local
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

# Telegram giới hạn ~4096 ký tự / tin; callback_data tối đa 64 byte → dùng token + bot_data
TELEGRAM_MAX_MESSAGE_LEN = 4096

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
        "/scheduletest — Test ngay báo cáo tuyến 1 (cố định trong `.env`, vào chat hiện tại)\n"
        "/scheduletest2 — Test ngay báo cáo tuyến 2 (`SCHEDULE2_*`)\n"
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

    result = None
    try:
        result = check_traffic(origin, destination)
        msg = format_result(result, origin_display, destination, show_route_steps=False)
    except Exception as e:
        logger.error(f"Traffic check error: {e}")
        msg = "❌ Có lỗi xảy ra khi kiểm tra. Vui lòng thử lại hoặc kiểm tra lại tên địa điểm."

    has_route_detail = bool((result or {}).get("route_legs")) or bool(
        (result or {}).get("route_turn_by_turn")
    )
    reply_markup = None
    if has_route_detail and result is not None:
        token = secrets.token_hex(8)
        context.bot_data[f"rtd_{token}"] = {
            "result": _result_for_route_cache(result),
            "origin": origin_display,
            "destination": destination,
            "header": "",
        }
        reply_markup = _route_detail_markup(token, "collapsed")

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    png = (result or {}).get("route_static_map_png")
    if png:
        await update.message.reply_photo(
            photo=io.BytesIO(png),
            filename="route.png",
        )
    return ConversationHandler.END


def _result_for_route_cache(result: dict) -> dict:
    """Bỏ bytes ảnh khỏi dict lưu tạm (tránh giữ PNG trong bot_data)."""
    return {k: v for k, v in result.items() if k != "route_static_map_png"}


def _truncate_for_telegram(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + "\n\n… _Tin bị cắt do giới hạn độ dài Telegram._"


def _route_detail_markup(token: str, state: str) -> InlineKeyboardMarkup:
    """
    state: collapsed | summary | full
    summary: đã mở — chỉ tóm tắt theo đoạn; full: kèm từng bước nhỏ.
    """
    if state == "collapsed":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Xem chi tiết tuyến", callback_data=f"rtd:{token}")]]
        )
    if state == "summary":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔼 Ẩn chi tiết tuyến", callback_data=f"rtc:{token}"),
                    InlineKeyboardButton("🔍 Từng bước chi tiết", callback_data=f"rts:{token}"),
                ]
            ]
        )
    # full
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔼 Ẩn chi tiết tuyến", callback_data=f"rtc:{token}"),
                InlineKeyboardButton("📌 Chỉ tóm tắt đoạn", callback_data=f"rts:{token}"),
            ]
        ]
    )


def _route_detail_body(entry: dict, *, collapsed: bool) -> str:
    if collapsed:
        return _truncate_for_telegram(
            entry["header"]
            + format_result(
                entry["result"],
                entry["origin"],
                entry["destination"],
                show_route_steps=False,
            )
        )
    mode = entry.get("detail_steps", "summary")
    return _truncate_for_telegram(
        entry["header"]
        + format_result(
            entry["result"],
            entry["origin"],
            entry["destination"],
            show_route_steps=True,
            route_steps_mode=mode,
        )
    )


def _fmt_leg_eta_line(leg: dict) -> str:
    """Một đoạn dòng phụ: thời gian (thực tế) + quãng đường."""
    dt = int(leg.get("duration_traffic_sec", 0))
    dm = int(leg.get("distance_m", 0))
    if dt >= 60:
        t = f"{dt // 60} phút"
    else:
        t = f"{dt} giây"
    dk = dm / 1000.0
    return f"{t} · {dk:.1f} km"


def _format_route_legs_block(
    route_legs: list,
    mode: str,
    *,
    max_legs: int = 32,
    max_step_lines_total: int = 55,
) -> list[str]:
    """mode: summary | full — phân cấp leg → bước nhỏ."""
    lines: list[str] = [""]
    lines.append("📋 *Chi tiết tuyến (theo đoạn):*")
    n_legs = min(len(route_legs), max_legs)
    step_budget = max_step_lines_total

    for i in range(n_legs):
        leg = route_legs[i]
        summary = _md_escape(str(leg.get("summary") or ""))
        eta = _fmt_leg_eta_line(leg)
        lines.append(f"")
        lines.append(f"*Đoạn {i + 1}* — {summary}")
        lines.append(f"_{eta}_")
        if mode == "full":
            for st in leg.get("steps") or []:
                if step_budget <= 0:
                    lines.append("  … _còn bước khác (bị giới hạn hiển thị)_")
                    break
                lines.append(f"  • {_md_escape(str(st))}")
                step_budget -= 1

    if len(route_legs) > max_legs:
        lines.append(f"")
        lines.append(f"… _và {len(route_legs) - max_legs} đoạn nữa_")
    return lines


def _md_escape(s: str) -> str:
    """Tránh vỡ Markdown Telegram trong nội dung tự do."""
    return (
        s.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
    )


def format_result(
    result: dict,
    origin: str,
    destination: str,
    *,
    show_route_steps: bool = True,
    route_steps_mode: str = "summary",
) -> str:
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

    steps = result.get("route_turn_by_turn") or []
    route_legs = result.get("route_legs") or []
    if show_route_steps:
        if route_legs:
            mode = route_steps_mode if route_steps_mode in ("summary", "full") else "summary"
            lines.extend(_format_route_legs_block(route_legs, mode))
        elif steps:
            lines.append(f"")
            lines.append("📋 *Chi tiết tuyến (Routes API):*")
            for line in steps[:40]:
                lines.append(f"  {_md_escape(line)}")
            if len(steps) > 40:
                lines.append(f"  … _và {len(steps) - 40} bước nữa_")
    elif steps or route_legs:
        lines.append(f"")
        lines.append("📋 *Chi tiết tuyến:* _bấm nút bên dưới để xem._")

    if result.get("route_static_map_png"):
        lines.append(f"")
        lines.append("🗺 *Sơ đồ tuyến:* xem ảnh bên dưới.")
        if result.get("congested_segments"):
            lines.append(
                "_Vạch đỏ/vàng trên tuyến: đoạn tắc/chậm. "
                "(Ảnh tĩnh không có lớp xe như trên app Maps.)_"
            )

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
    *,
    origin: str | None = None,
    destination: str | None = None,
) -> None:
    """Gọi check_traffic và gửi tin. Mặc định đọc SCHEDULE_ORIGIN / SCHEDULE_DESTINATION."""
    o = (origin if origin is not None else os.getenv("SCHEDULE_ORIGIN", "")).strip()
    d = (destination if destination is not None else os.getenv("SCHEDULE_DESTINATION", "")).strip()
    if not o or not d:
        raise ValueError("Thiếu điểm xuất phát hoặc điểm đến cho báo cáo")

    origin, destination = o, d

    result = check_traffic(origin, destination)
    msg = header + format_result(result, origin, destination, show_route_steps=False)
    has_route_detail = bool(result.get("route_legs")) or bool(result.get("route_turn_by_turn"))
    reply_markup = None
    if has_route_detail:
        token = secrets.token_hex(8)
        application.bot_data[f"rtd_{token}"] = {
            "result": _result_for_route_cache(result),
            "origin": origin,
            "destination": destination,
            "header": header,
        }
        reply_markup = _route_detail_markup(token, "collapsed")

    await application.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    png = result.get("route_static_map_png")
    if png:
        await application.bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(png),
            filename="route.png",
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


async def _send_scheduled_report_route2(application: Application) -> None:
    chat_raw = os.getenv("SCHEDULE2_CHAT_ID", "").strip() or os.getenv("SCHEDULE_CHAT_ID", "").strip()
    if not chat_raw:
        return
    try:
        chat_id = int(chat_raw)
    except ValueError:
        logger.error("SCHEDULE2_CHAT_ID / SCHEDULE_CHAT_ID phải là số nguyên")
        return

    origin = os.getenv("SCHEDULE2_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE2_DESTINATION", "").strip()
    if not origin or not destination:
        return

    header = "⏰ *Báo cáo tự động — tuyến 2 (giờ Việt Nam)*\n\n"
    try:
        await _deliver_fixed_route_report(
            application, chat_id, header, origin=origin, destination=destination
        )
    except Exception as e:
        logger.exception("Lỗi báo cáo định kỳ tuyến 2: %s", e)
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Lỗi báo cáo định kỳ (tuyến 2): {e}",
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


async def scheduletest2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chạy cùng logic job định kỳ tuyến 2, gửi vào chat hiện tại."""
    chat = update.effective_chat
    if not chat or not update.message:
        return
    origin = os.getenv("SCHEDULE2_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE2_DESTINATION", "").strip()
    if not origin or not destination:
        await update.message.reply_text(
            "Cần `SCHEDULE2_ORIGIN` và `SCHEDULE2_DESTINATION` trong `.env`.",
            parse_mode="Markdown",
        )
        return
    header = "🧪 *Test báo cáo định kỳ — tuyến 2*\n\n"
    try:
        await _deliver_fixed_route_report(
            context.application,
            chat.id,
            header,
            origin=origin,
            destination=destination,
        )
    except Exception as e:
        logger.exception("scheduletest2: %s", e)
        await update.message.reply_text(f"❌ Lỗi: {e}")


async def _post_init_schedule(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=SCHEDULE_TZ)
    jobs_info: list[tuple[int, int, str]] = []

    origin = os.getenv("SCHEDULE_ORIGIN", "").strip()
    destination = os.getenv("SCHEDULE_DESTINATION", "").strip()
    chat_raw = os.getenv("SCHEDULE_CHAT_ID", "").strip()
    if origin and destination and chat_raw:
        try:
            hour = int(os.getenv("SCHEDULE_HOUR", "17"))
            minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
        except ValueError:
            hour, minute = 17, 0
        scheduler.add_job(
            _send_scheduled_report,
            CronTrigger(hour=hour, minute=minute, timezone=SCHEDULE_TZ),
            args=[application],
            id="daily_traffic_report",
            replace_existing=True,
        )
        jobs_info.append((hour, minute, "tuyến 1"))

    o2 = os.getenv("SCHEDULE2_ORIGIN", "").strip()
    d2 = os.getenv("SCHEDULE2_DESTINATION", "").strip()
    chat2_raw = os.getenv("SCHEDULE2_CHAT_ID", "").strip() or chat_raw
    if o2 and d2 and chat2_raw:
        try:
            int(chat2_raw)
        except ValueError:
            logger.error("SCHEDULE2_CHAT_ID / SCHEDULE_CHAT_ID phải là số nguyên")
        else:
            try:
                h2 = int(os.getenv("SCHEDULE2_HOUR", "17"))
                m2 = int(os.getenv("SCHEDULE2_MINUTE", "30"))
            except ValueError:
                h2, m2 = 17, 30
            scheduler.add_job(
                _send_scheduled_report_route2,
                CronTrigger(hour=h2, minute=m2, timezone=SCHEDULE_TZ),
                args=[application],
                id="daily_traffic_report_route2",
                replace_existing=True,
            )
            jobs_info.append((h2, m2, "tuyến 2"))

    if not jobs_info:
        logger.info(
            "Báo cáo định kỳ tắt (thiếu cấu hình SCHEDULE_* và/hoặc SCHEDULE2_*)."
        )
        return

    scheduler.start()
    for hour, minute, label in jobs_info:
        logger.info(
            "Đã bật báo cáo định kỳ (%s) lúc %02d:%02d (%s)",
            label,
            hour,
            minute,
            SCHEDULE_TZ.key,
        )


async def route_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rtd: mở tóm tắt theo đoạn | rts: đổi summary/full | rtc: ẩn."""
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.answer()
        return
    prefix, token = parts
    if prefix not in ("rtd", "rtc", "rts"):
        await query.answer()
        return

    entry = context.bot_data.get(f"rtd_{token}")
    if not entry:
        await query.answer(
            "Không tìm thấy dữ liệu (có thể đã hết hạn). Chạy lại /check.",
            show_alert=True,
        )
        return

    await query.answer()

    if prefix == "rtc":
        entry.pop("detail_steps", None)
        text = _route_detail_body(entry, collapsed=True)
        markup = _route_detail_markup(token, "collapsed")
    elif prefix == "rtd":
        entry["detail_steps"] = "summary"
        text = _route_detail_body(entry, collapsed=False)
        markup = _route_detail_markup(token, "summary")
    else:  # rts
        cur = entry.get("detail_steps", "summary")
        entry["detail_steps"] = "full" if cur == "summary" else "summary"
        st = entry["detail_steps"]
        text = _route_detail_body(entry, collapsed=False)
        markup = _route_detail_markup(token, st)

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except Exception as e:
        logger.exception("route_detail_callback edit: %s", e)
        await query.message.reply_text(
            "Không sửa được tin (có thể quá dài). Thử lại /check.",
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
    app.add_handler(CommandHandler("scheduletest2", scheduletest2))
    app.add_handler(
        CallbackQueryHandler(route_detail_callback, pattern=r"^rt[dcs]:[a-f0-9]{16}$")
    )
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot đang chạy...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()