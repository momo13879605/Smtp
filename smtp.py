import asyncio
import logging
import zipfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from typing import List, Tuple
import concurrent.futures

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

# ==================== تنظیمات ====================
BOT_TOKEN = "7413084969:AAHglr2N6eO_9VxhGCepns0iWKr9nYgmDZg"                 # ← توکن از @BotFather
ADMIN_ID = 5914346958                        # ← آیدی عددی خودت را جایگزین کن
TEST_RECIPIENT = "mwmw07291@gmail.com"   # ← ایمیلی که تست به آن ارسال می‌شود
TIMEOUT_SMTP = 10                          # ثانیه تایم‌اوت هر تست
DELAY_BETWEEN_TESTS = 0.3                  # ثانیه تأخیر بین شروع هر تست (جهت کنترل نرخ)
MAX_WORKERS = 15                           # تعداد نخ‌های هم‌زمان (بسته به قدرت VPS تنظیم کن)
# =================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

user_active_tasks: dict[int, asyncio.Event] = {}


# ==================== توابع کمکی ====================

def parse_smtp_list(file_content: str) -> List[Tuple[str, int, str, str]]:
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


def test_and_send_smtp(host: str, port: int, user: str, password: str,
                       recipient: str) -> bool:
    """بلاک‌کننده: تست لاگین + ارسال ایمیل امن"""
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

        # ساخت ایمیل تست بدون لو دادن اطلاعات
        msg = MIMEMultipart()
        msg['From'] = user
        msg['To'] = recipient
        msg['Subject'] = "SMTP Connection Test"
        msg.attach(MIMEText(
            "This is a test email sent by the SMTP checker bot.\n"
            "No sensitive data is included.",
            'plain'
        ))
        server.sendmail(user, recipient, msg.as_string())
        server.quit()
        return True
    except:
        return False


def create_zip_in_memory(files: dict[str, str]) -> BytesIO:
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    zip_buffer.seek(0)
    return zip_buffer


async def cancel_active_test(chat_id: int):
    if chat_id in user_active_tasks:
        user_active_tasks[chat_id].set()
        del user_active_tasks[chat_id]


# ==================== هندلرهای ربات ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return
    await update.message.reply_text(
        "🤖 **ربات تست SMTP (پرسرعت)**\n\n"
        "لطفاً فایل txt حاوی SMTP ها را ارسال کنید.\n"
        f"تنظیمات فعلی: {MAX_WORKERS} نخ هم‌زمان, تأخیر {DELAY_BETWEEN_TESTS}s, تایم‌اوت {TIMEOUT_SMTP}s\n"
        f"ایمیل تست: {TEST_RECIPIENT}\n"
        "/cancel برای توقف",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return
    if update.effective_chat.id in user_active_tasks:
        await cancel_active_test(update.effective_chat.id)
        await update.message.reply_text("🛑 فرایند تست کنسل شد.")
    else:
        await update.message.reply_text("ℹ️ هیچ تستی در حال اجرا نیست.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    if chat_id in user_active_tasks:
        await update.message.reply_text(
            "⏳ یک تست در حال اجراست. لطفاً صبر کنید یا از /cancel استفاده کنید."
        )
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

    smtp_list = parse_smtp_list(file_content)
    total = len(smtp_list)
    if total == 0:
        await update.message.reply_text("❌ هیچ رکورد SMTP معتبری در فایل پیدا نشد.")
        return

    status_msg = await update.message.reply_text(
        f"📥 فایل دریافت شد.\n"
        f"🔢 تعداد SMTP: {total}\n"
        f"⚙️ نخ‌های هم‌زمان: {MAX_WORKERS}\n"
        f"⏱️ تایم‌اوت هر تست: {TIMEOUT_SMTP}s\n"
        f"⏳ تأخیر بین تست‌ها: {DELAY_BETWEEN_TESTS}s\n\n"
        f"🔄 تست‌ها شروع می‌شوند...\n"
        f"/cancel برای توقف"
    )

    cancel_event = asyncio.Event()
    user_active_tasks[chat_id] = cancel_event

    valid_smtps: List[str] = []
    error_occurred = False
    error_message = ""
    completed = 0

    # Semaphore برای محدود کردن هم‌زمانی
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async def worker(host: str, port: int, user: str, pwd: str):
        nonlocal completed, valid_smtps
        if cancel_event.is_set():
            return

        async with semaphore:
            # اجرای تابع بلاک‌کننده در یک thread
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(
                None,  # استفاده از ThreadPoolExecutor پیش‌فرض
                test_and_send_smtp, host, port, user, pwd, TEST_RECIPIENT
            )

        if success:
            valid_smtps.append(f"{host}:{port}:{user}:{pwd}")
            # ارسال فوری به ادمین
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"✅ SMTP سالم (ارسال موفق):\n"
                        f"هاست: `{host}`\n"
                        f"پورت: `{port}`\n"
                        f"کاربر: `{user}`\n"
                        f"رمز: `{pwd}`"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"خطا در ارسال SMTP سالم: {e}")

        completed += 1
        # به‌روزرسانی پیام پیشرفت هر 50 تست یا در پایان
        if completed % 50 == 0 or completed == total:
            try:
                await status_msg.edit_text(
                    f"⏳ پیشرفت: {completed}/{total}\n"
                    f"✅ سالم: {len(valid_smtps)}\n"
                    f"/cancel برای توقف"
                )
            except:
                pass

    try:
        # ایجاد taskها و کنترل تأخیر بین شروع آن‌ها
        tasks = []
        for (host, port, user, pwd) in smtp_list:
            if cancel_event.is_set():
                break
            tasks.append(asyncio.create_task(worker(host, port, user, pwd)))
            await asyncio.sleep(DELAY_BETWEEN_TESTS)  # تأخیر بین شروع تسک‌ها

        # صبر برای تمام تسک‌ها
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        logger.exception("خطای پیش‌بینی‌نشده در فرایند تست")
        error_occurred = True
        error_message = str(e)

    finally:
        if chat_id in user_active_tasks:
            del user_active_tasks[chat_id]

    # ارسال فایل نهایی
    if valid_smtps:
        final_content = "\n".join(valid_smtps)
        valid_file = BytesIO(final_content.encode("utf-8"))
        valid_file.name = "valid_smtps.txt"
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=valid_file,
                caption=f"📁 تست پایان یافت. {len(valid_smtps)} از {total} SMTP سالم."
            )
        except Exception as e:
            logger.error(f"ارسال فایل نهایی ناموفق: {e}")
    else:
        if not cancel_event.is_set():
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ هیچ SMTP سالمی پیدا نشد."
            )

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
                caption="⚠️ خطایی رخ داد. فایل‌ها پیوست شد."
            )
        except Exception as e:
            logger.error(f"ارسال فایل زیپ خطا: {e}")

    await status_msg.edit_text(
        f"✅ تست‌ها به پایان رسید.\n"
        f"🔢 کل: {total}\n"
        f"💚 سالم: {len(valid_smtps)}"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("ربات آماده‌ی اجرا...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()