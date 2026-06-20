import asyncio
import logging
import smtplib
from io import BytesIO, StringIO
from typing import List, Dict, Optional, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ===================== تنظیمات =====================
BOT_TOKEN = "7413084969:AAHglr2N6eO_9VxhGCepns0iWKr9nYgmDZg"         # ← توکن ربات از @BotFather
ADMIN_ID = 5914346958                 # ← آیدی عددی خودت
TIMEOUT_SMTP = 10                    # ثانیه تایم‌اوت اتصال
DELAY_BETWEEN_TESTS = 1.0            # تأخیر بین تست‌ها
DELAY_BETWEEN_VALID_MSGS = 1.2       # تأخیر بین ارسال پیام‌های سالم (Flood Control)
MAX_VALID_PER_FILE = 50              # ارسال فایل بعد از این تعداد سالم
PROGRESS_UPDATE_EVERY = 15           # به‌روزرسانی پیام پیشرفت هر n تست
# =====================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

user_active_tasks: dict[int, asyncio.Event] = {}  # chat_id -> cancel event


def test_smtp_with_info(host: str, port: int, user: str, password: str) -> Tuple[bool, Optional[Dict[str, str]]]:
    """
    تلاش برای لاگین در SMTP.
    برگشت: (وضعیت سالم بودن, دیکشنری اطلاعات سرور شامل بنر و غیره + max_message_size در صورت وجود)
    """
    info = {}
    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SMTP)
            info['banner'] = 'SSL Connection'
            code, ehlo_msg = server.ehlo()
            info['ehlo_response'] = ehlo_msg.decode() if isinstance(ehlo_msg, bytes) else ehlo_msg
        else:
            server = smtplib.SMTP()
            code, banner_msg = server.connect(host, port, timeout=TIMEOUT_SMTP)
            info['banner'] = banner_msg.decode() if isinstance(banner_msg, bytes) else banner_msg
            code, ehlo_msg = server.ehlo()
            info['ehlo_response'] = ehlo_msg.decode() if isinstance(ehlo_msg, bytes) else ehlo_msg
            if server.has_extn('STARTTLS'):
                server.starttls()
                code, ehlo_msg2 = server.ehlo()
                info['ehlo_response_after_tls'] = ehlo_msg2.decode() if isinstance(ehlo_msg2, bytes) else ehlo_msg2

        # متدهای احراز
        info['auth_methods'] = server.esmtp_features.get('auth', 'ندارد')

        # ⚡ حداکثر حجم مجاز هر ایمیل (SIZE) از SMTP
        size_bytes = server.esmtp_features.get('size')
        if size_bytes:
            try:
                size_mb = int(size_bytes) / (1024 * 1024)
                # اگر کمتر از ۱ مگ بود، به کیلوبایت نمایش بده
                if size_mb < 1:
                    size_kb = int(size_bytes) / 1024
                    info['max_message_size'] = f"{size_kb:.1f} کیلوبایت"
                else:
                    info['max_message_size'] = f"{size_mb:.1f} مگابایت"
            except ValueError:
                info['max_message_size'] = f"{size_bytes} بایت"

        server.login(user, password)
        server.quit()
        return True, info
    except Exception:
        return False, None
    finally:
        try:
            if server:
                server.close()
        except:
            pass


def parse_smtp_line(line: str) -> Optional[Tuple[str, int, str, str]]:
    """یک خط را بررسی کرده و tuple معتبر (host, port, user, pwd) برمی‌گرداند"""
    line = line.strip()
    if not line:
        return None
    for delim in ['|', ':', ',']:
        parts = line.split(delim)
        if len(parts) == 4:
            host, port_str, user, pwd = parts
            try:
                port = int(port_str)
                return (host, port, user, pwd)
            except ValueError:
                return None
    # حالت چند SMTP با فاصله در یک خط
    sub_tokens = line.split()
    if len(sub_tokens) > 1:
        for token in sub_tokens:
            res = parse_smtp_line(token)
            if res:
                return res
    return None


async def send_valid_smtp_message(context, host: str, port: int, user: str, pwd: str, info: dict):
    """ارسال پیام تلگرامی با اطلاعات لاگین + (اختیاری) حداکثر حجم ایمیل"""
    msg = (
        f"✅ *SMTP سالم پیدا شد:*\n\n"
        f"هاست: `{host}`\n"
        f"پورت: `{port}`\n"
        f"کاربر: `{user}`\n"
        f"رمز: `{pwd}`"
    )

    # اگر اطلاعات حجم در دسترس بود، جداکننده و مقدار را اضافه کن
    if 'max_message_size' in info:
        msg += (
            f"\n\n➖️➖️➖️➖️➖️➖️➖️➖️➖️➖️➖️\n"
            f"حداکثر حجم مجاز هر ایمیل: `{info['max_message_size']}`"
        )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"خطا در ارسال پیام سالم: {e}")


async def send_valid_file(context, smtp_entries: List[str], final=False):
    """فایل valid_smtps.txt را (فقط host:port:user:pass) ارسال می‌کند"""
    filename = f"valid_smtps{'_final' if final else ''}.txt"
    content = "\n".join(smtp_entries)
    file = BytesIO(content.encode("utf-8"))
    file.name = filename
    try:
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=file,
            caption=f"📁 {'فایل نهایی' if final else 'بخش'} سالم‌ها ({len(smtp_entries)} عدد)"
        )
    except Exception as e:
        logger.error(f"ارسال فایل ناموفق: {e}")


async def process_file(update, context, file_content: str):
    """فایل دریافتی را خط‌به‌خط پردازش می‌کند"""
    chat_id = update.effective_chat.id
    cancel_event = asyncio.Event()
    user_active_tasks[chat_id] = cancel_event

    smtp_lines = StringIO(file_content)
    total_tested = 0
    valid_entries = []
    status_msg = await update.message.reply_text("⏳ شروع تست SMTP ها...")

    try:
        for line in smtp_lines:
            if cancel_event.is_set():
                await update.message.reply_text("🛑 فرایند با دستور کنسل شد.")
                break

            smtp_data = parse_smtp_line(line)
            if smtp_data is None:
                continue

            host, port, user, pwd = smtp_data
            total_tested += 1

            # تست SMTP با تایم‌اوت
            try:
                valid, info = await asyncio.wait_for(
                    asyncio.to_thread(test_smtp_with_info, host, port, user, pwd),
                    timeout=TIMEOUT_SMTP + 3
                )
            except asyncio.TimeoutError:
                valid, info = False, None
            except Exception:
                valid, info = False, None

            if valid and info:
                # ثبت فقط اطلاعات لاگین برای فایل
                valid_entries.append(f"{host}:{port}:{user}:{pwd}")

                # ارسال پیام با اطلاعات سرور + حجم (در صورت وجود)
                await send_valid_smtp_message(context, host, port, user, pwd, info)
                await asyncio.sleep(DELAY_BETWEEN_VALID_MSGS)

                # اگر تعداد سالم‌ها به حد نصاب رسید، فایل را بفرست و لیست را خالی کن
                if len(valid_entries) >= MAX_VALID_PER_FILE:
                    await send_valid_file(context, valid_entries, final=False)
                    valid_entries.clear()

            # به‌روزرسانی پیام پیشرفت
            if total_tested % PROGRESS_UPDATE_EVERY == 0:
                try:
                    await status_msg.edit_text(
                        f"🔄 در حال تست...\n"
                        f"📊 تست‌های انجام‌شده: {total_tested}\n"
                        f"💚 سالم تا اینجا: {len(valid_entries)}"
                    )
                except:
                    pass

            await asyncio.sleep(DELAY_BETWEEN_TESTS)

    except Exception as e:
        logger.exception("خطا در پردازش")
        if valid_entries:
            await send_valid_file(context, valid_entries, final=False)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ خطا: {str(e)}")
        return
    finally:
        if chat_id in user_active_tasks:
            del user_active_tasks[chat_id]

    # ارسال فایل نهایی
    if valid_entries:
        await send_valid_file(context, valid_entries, final=True)
    else:
        await context.bot.send_message(chat_id=ADMIN_ID, text="❌ هیچ SMTP سالمی پیدا نشد.")

    await status_msg.edit_text(f"✅ پایان تست. {total_tested} تست انجام شد.")


# -------- هندلرهای ربات ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ دسترسی محدود.")
        return
    await update.message.reply_text(
        "📩 فایل SMTP خود را ارسال کنید.\n"
        "فرمت قابل قبول: `host:port:user:pass` (جداکننده‌ها: `:`, `|`, `,`)\n"
        "برای توقف از /cancel استفاده کنید.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    chat_id = update.effective_chat.id
    if chat_id in user_active_tasks:
        user_active_tasks[chat_id].set()
        await update.message.reply_text("🛑 درخواست توقف ثبت شد.")
    else:
        await update.message.reply_text("ℹ️ تستی در جریان نیست.")


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id != ADMIN_ID:
        return
    if chat_id in user_active_tasks:
        await update.message.reply_text("⏳ یک تست در حال انجام است. /cancel برای توقف.")
        return

    doc = update.message.document
    if not doc:
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        file_content = file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در دریافت فایل: {e}")
        return

    # اجرای غیرهمزمان برای آزاد شدن سریع هندلر
    asyncio.create_task(process_file(update, context, file_content))


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()