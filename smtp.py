import asyncio
import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Tuple, Optional

import smtplib
import socket

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# =================== تنظیمات ===================
BOT_TOKEN = "7413084969:AAHglr2N6eO_9VxhGCepns0iWKr9nYgmDZg"      # ← توکن ربات از @BotFather
ADMIN_ID = 5914346958                             # ← آیدی عددی خودت را جایگزین کن
TIMEOUT_SMTP = 10                                # ثانیه، تایم‌اوت برای هر تست
DELAY_BETWEEN_TESTS = 1.5                        # ثانیه، فاصله‌ی بین تست‌ها
# =================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# نگهداری وضعیت هر کاربر برای امکان کنسل کردن
user_active_tasks: dict[int, asyncio.Event] = {}  # chat_id -> cancel_event


# ===== ابزارهای کمکی =====

def parse_smtp_list(file_content: str) -> List[Tuple[str, int, str, str]]:
    """
    محتوای فایل را خوانده و لیست SMTP ها را استخراج می‌کند.
    جداکننده‌ها: '|', ':', ','  
    هر رکورد با فاصله، تب یا خط جدید جدا می‌شود.
    """
    tokens = file_content.split()
    entries = []
    for token in tokens:
        for delim in ['|', ':', ',']:
            parts = token.split(delim)
            if len(parts) == 4:
                host, port_str, user, pwd = parts
                try:
                    port = int(port_str)
                    entries.append((host, port, user, pwd))
                    break
                except ValueError:
                    continue
    return entries


def test_smtp_login(host: str, port: int, user: str, password: str) -> bool:
    """تلاش برای لاگین به SMTP (بلاک کننده است)"""
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SMTP)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SMTP)
            server.ehlo()
            if server.has_extn('STARTTLS'):
                server.starttls()
                server.ehlo()
        server.login(user, password)
        server.quit()
        return True
    except Exception:
        return False


def create_zip_in_memory(files: dict[str, str]) -> BytesIO:
    """فایل‌های داده‌شده را به یک زیپ درون‌حافظه تبدیل می‌کند"""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    zip_buffer.seek(0)
    return zip_buffer


async def cancel_active_test(chat_id: int):
    """تسک جاری کاربر را کنسل می‌کند"""
    if chat_id in user_active_tasks:
        user_active_tasks[chat_id].set()
        del user_active_tasks[chat_id]


# ===== مدیریت ربات =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /start"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    await update.message.reply_text(
        "🤖 *ربات تست SMTP*\n\n"
        "لطفاً فایل txt حاوی لیست SMTP ها را ارسال کنید.\n"
        "فرمت قابل قبول: `host:port:user:pass` یا `host|port|user|pass`\n"
        "(رکوردها می‌توانند با فاصله، تب یا خط جدید جدا شده باشند)",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /cancel برای توقف تست جاری"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    if update.effective_chat.id in user_active_tasks:
        await cancel_active_test(update.effective_chat.id)
        await update.message.reply_text("🛑 فرایند تست کنسل شد.")
    else:
        await update.message.reply_text("ℹ️ هیچ تستی در حال اجرا نیست.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت فایل و شروع تست SMTP ها"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    # اگر تستی در حال اجراست، لغو آن درخواست و رد شود
    if chat_id in user_active_tasks:
        await update.message.reply_text("⏳ یک تست در حال اجراست. لطفاً صبر کنید یا از /cancel استفاده کنید.")
        return

    document = update.message.document
    if not document:
        await update.message.reply_text("❌ لطفاً یک فایل ارسال کنید.")
        return

    # دانلود فایل
    file = await context.bot.get_file(document.file_id)
    file_bytes = await file.download_as_bytearray()
    try:
        file_content = file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در خواندن فایل: {e}")
        return

    # تجزیه‌ی SMTP ها
    smtp_list = parse_smtp_list(file_content)
    total = len(smtp_list)
    if total == 0:
        await update.message.reply_text("❌ هیچ رکورد SMTP معتبری در فایل پیدا نشد.")
        return

    # اعلام شروع
    status_msg = await update.message.reply_text(
        f"📥 فایل دریافت شد.\n"
        f"🔢 تعداد SMTP های یافت‌شده: {total}\n"
        f"⏱️ تایم‌اوت هر تست: {TIMEOUT_SMTP} ثانیه\n"
        f"⏳ تأخیر بین تست‌ها: {DELAY_BETWEEN_TESTS} ثانیه\n\n"
        f"🔄 تست‌ها شروع می‌شوند...\n"
        f"برای توقف از /cancel استفاده کنید."
    )

    # ایجاد رویداد کنسل شدن
    cancel_event = asyncio.Event()
    user_active_tasks[chat_id] = cancel_event

    valid_smtps: List[str] = []  # ذخیره‌سازی سالم‌ها به صورت رشته‌ی host:port:user:pass
    error_occurred = False
    error_message = ""

    try:
        for idx, (host, port, user, pwd) in enumerate(smtp_list, start=1):
            if cancel_event.is_set():
                await update.message.reply_text("🛑 فرایند با دستور کاربر متوقف شد.")
                break

            # بروزرسانی پیام پیشرفت
            if idx % 10 == 0 or idx == total:
                try:
                    await status_msg.edit_text(
                        f"⏳ در حال تست {idx}/{total}...\n"
                        f"✅ سالم: {len(valid_smtps)}\n\n"
                        f"برای توقف /cancel"
                    )
                except Exception:
                    pass

            # تست با تایم‌اوت
            try:
                is_valid = await asyncio.wait_for(
                    asyncio.to_thread(test_smtp_login, host, port, user, pwd),
                    timeout=TIMEOUT_SMTP + 2  # کمی بیشتر از تایم‌اوت اصلی
                )
            except asyncio.TimeoutError:
                is_valid = False  # زمان بیش از حد طول کشید
            except Exception:
                is_valid = False

            if is_valid:
                valid_smtps.append(f"{host}:{port}:{user}:{pwd}")
                # ارسال SMTP سالم به ادمین
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"✅ SMTP سالم پیدا شد:\n"
                            f"هاست: `{host}`\n"
                            f"پورت: `{port}`\n"
                            f"کاربر: `{user}`\n"
                            f"رمز: `{pwd}`"
                        ),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"خطا در ارسال SMTP سالم: {e}")

            # تأخیر بین تست‌ها
            await asyncio.sleep(DELAY_BETWEEN_TESTS)

    except Exception as e:
        logger.exception("خطا در فرایند تست")
        error_occurred = True
        error_message = str(e)

    finally:
        # پاک‌سازی رویداد
        if chat_id in user_active_tasks:
            del user_active_tasks[chat_id]

    # تولید فایل نهایی سالم‌ها
    if valid_smtps:
        final_content = "\n".join(valid_smtps)
        valid_file = BytesIO(final_content.encode("utf-8"))
        valid_file.name = "valid_smtps.txt"
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=valid_file,
                caption=f"📁 تست پایان یافت. {len(valid_smtps)} SMTP سالم از {total} مورد."
            )
        except Exception as e:
            logger.error(f"ارسال فایل نهایی ناموفق: {e}")
    else:
        if not cancel_event.is_set():
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ هیچ SMTP سالمی پیدا نشد."
            )

    # در صورت بروز خطای غیرمنتظره، فایل زیپ شامل سالم‌های تا آن لحظه و خطا ارسال شود
    if error_occurred:
        files_to_zip = {}
        if valid_smtps:
            files_to_zip["valid_smtps_partial.txt"] = "\n".join(valid_smtps)
        files_to_zip["error.txt"] = f"Error during testing:\n{error_message}"
        zip_file = create_zip_in_memory(files_to_zip)
        zip_file.name = "smtp_error_report.zip"
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=zip_file,
                caption="⚠️ خطایی در فرایند رخ داد. فایل‌های تا این لحظه پیوست شدند."
            )
        except Exception as e:
            logger.error(f"ارسال فایل زیپ خطا: {e}")

    # اطلاع‌رسانی پایان
    await status_msg.edit_text(
        f"✅ تست‌ها به پایان رسید.\n"
        f"🔢 کل: {total}\n"
        f"💚 سالم: {len(valid_smtps)}\n"
        f"فایل نهایی ارسال شد."
    )


# ===== اجرای ربات =====

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("ربات آماده‌ی اجرا...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()