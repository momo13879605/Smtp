import asyncio
import logging
import zipfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from io import BytesIO
from typing import List, Tuple, Dict
import time

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
BOT_TOKEN = "7413084969:AAHglr2N6eO_9VxhGCepns0iWKr9nYgmDZg"                 # ← توکن ربات از @BotFather
ADMIN_ID = 5914346958                        # ← آیدی عددی شما
TEST_RECIPIENT = "source.donii@gmail.com"       # ← ایمیل مقصد تست
TIMEOUT_SMTP = 8                            # تایم‌اوت هر تست (ثانیه)
MAX_CONCURRENT_TASKS = 10                   # تعداد تست هم‌زمان
DELAY_BETWEEN_TASKS = 0.2                   # تأخیر بین شروع تسک‌ها (کنترل نرخ ورود)
PER_DOMAIN_DELAY = 2.0                      # حداقل فاصله (ثانیه) بین تست‌های یک دامنه
# =================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# کنترل نرخ دامنه
domain_last_test: Dict[str, float] = {}
domain_lock = asyncio.Lock()

# نگهداری وظایف برای کنسل شدن
user_active_tasks: dict[int, asyncio.Event] = {}
user_task_list: dict[int, list[asyncio.Task]] = {}


# ==================== توابع کمکی ====================

def parse_smtp_list(file_content: str) -> List[Tuple[str, int, str, str]]:
    """تجزیه فایل SMTP با جداکننده‌های | : ,"""
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
                       recipient: str) -> Tuple[bool, str]:
    """
    تلاش برای لاگین و ارسال ایمیل تست با هدرهای کامل.
    برمی‌گرداند: (موفقیت, پیام خطا یا موفقیت)
    """
    try:
        # اتصال
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SMTP)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SMTP)
            server.ehlo()
            if server.has_extn('STARTTLS'):
                server.starttls()
                server.ehlo()

        # لاگین
        server.login(user, password)

        # ساخت ایمیل تست کامل (با هدرهای Date و Message-ID)
        msg = MIMEMultipart()
        msg['From'] = user
        msg['To'] = recipient
        msg['Subject'] = "SMTP Delivery Test"
        msg['Date'] = formatdate(localtime=True)        # ← این هدر حیاتی است
        msg['Message-ID'] = make_msgid(domain=host.split('.')[-2] + '.' + host.split('.')[-1] if '.' in host else "smtp.test")
        msg.attach(MIMEText(
            "This is a delivery test. If you receive this, the SMTP is healthy.",
            'plain'
        ))

        # ارسال
        server.sendmail(user, recipient, msg.as_string())
        server.quit()
        return True, "ارسال موفق"

    except smtplib.SMTPAuthenticationError as e:
        return False, f"خطای احراز هویت: {e.smtp_code} {e.smtp_error}"
    except smtplib.SMTPRecipientsRefused as e:
        return False, f"گیرنده رد شد: {e.recipients}"
    except smtplib.SMTPSenderRefused as e:
        return False, f"فرستنده رد شد: {e.smtp_code} {e.smtp_error}"
    except smtplib.SMTPDataError as e:
        return False, f"خطای داده (Data Error): {e.smtp_code} {e.smtp_error}"
    except smtplib.SMTPConnectError as e:
        return False, f"خطای اتصال: {e.smtp_code} {e.smtp_error}"
    except smtplib.SMTPHeloError as e:
        return False, f"خطای HELO/EHLO: {e.smtp_code} {e.smtp_error}"
    except smtplib.SMTPNotSupportedError as e:
        return False, f"ویژگی پشتیبانی نمی‌شود: {e}"
    except smtplib.SMTPException as e:
        return False, f"خطای عمومی SMTP: {e}"
    except socket.timeout:
        return False, "اتصال تایم‌اوت شد (پاسخی از سرور دریافت نشد)"
    except socket.gaierror as e:
        return False, f"خطای DNS/آدرس: {e}"
    except ConnectionRefusedError:
        return False, "اتصال توسط سرور رد شد (Connection Refused)"
    except Exception as e:
        return False, f"خطای ناشناخته: {e}"


def create_zip_in_memory(files: dict[str, str]) -> BytesIO:
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    zip_buffer.seek(0)
    return zip_buffer


async def wait_for_domain(host: str):
    """کنترل نرخ برای یک دامنه خاص"""
    domain = host.split('.')[-2] + '.' + host.split('.')[-1] if '.' in host else host
    async with domain_lock:
        now = time.time()
        last_time = domain_last_test.get(domain, 0)
        wait = PER_DOMAIN_DELAY - (now - last_time)
        if wait > 0:
            logger.info(f"Waiting {wait:.1f}s for domain {domain}")
            await asyncio.sleep(wait)
        domain_last_test[domain] = time.time()


# ==================== هندلرهای ربات ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return
    await update.message.reply_text(
        "🤖 **ربات تست SMTP (ارسال واقعی با هدرهای استاندارد)**\n\n"
        "لطفاً فایل txt حاوی SMTP ها را ارسال کنید.\n"
        f"تنظیمات: هم‌زمان={MAX_CONCURRENT_TASKS}, تایم‌اوت={TIMEOUT_SMTP}s\n"
        f"ایمیل تست: `{TEST_RECIPIENT}`\n"
        "پس از پایان، فایل سالم‌ها + فایل خطاهای دقیق ارسال می‌شود.\n"
        "/cancel برای توقف",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    if chat_id in user_active_tasks:
        user_active_tasks[chat_id].set()
        for task in user_task_list.get(chat_id, []):
            task.cancel()
        user_task_list.pop(chat_id, None)
        await update.message.reply_text("🛑 فرایند تست کنسل شد. همه وظایف متوقف می‌شوند.")
    else:
        await update.message.reply_text("ℹ️ هیچ تستی در حال اجرا نیست.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

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

    smtp_list = parse_smtp_list(file_content)
    total = len(smtp_list)
    if total == 0:
        await update.message.reply_text("❌ هیچ رکورد SMTP معتبری در فایل پیدا نشد.")
        return

    status_msg = await update.message.reply_text(
        f"📥 فایل دریافت شد.\n"
        f"🔢 تعداد SMTP: {total}\n"
        f"⚙️ هم‌زمان={MAX_CONCURRENT_TASKS}, تایم‌اوت={TIMEOUT_SMTP}s\n"
        f"⏳ تست‌ها شروع می‌شوند...\n"
        f"/cancel برای توقف"
    )

    # پین پیام (بدون اعلان)
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=status_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Pin failed: {e}")

    cancel_event = asyncio.Event()
    user_active_tasks[chat_id] = cancel_event
    user_task_list[chat_id] = []

    valid_smtps: List[str] = []          # host:port:user:pass
    failed_details: List[str] = []       # خطاهای دقیق
    error_occurred = False
    error_message = ""
    completed = 0

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    async def worker(host: str, port: int, user: str, pwd: str):
        nonlocal completed, valid_smtps, failed_details
        if cancel_event.is_set():
            return

        await wait_for_domain(host)

        async with semaphore:
            if cancel_event.is_set():
                return

            loop = asyncio.get_running_loop()
            try:
                success, msg = await asyncio.wait_for(
                    loop.run_in_executor(None, test_and_send_smtp, host, port, user, pwd, TEST_RECIPIENT),
                    timeout=TIMEOUT_SMTP + 2
                )
            except asyncio.TimeoutError:
                success, msg = False, "تایم‌اوت کلی (پاسخ دیرتر از حد انتظار)"
            except Exception as e:
                success, msg = False, f"خطای سیستمی: {e}"

        if success:
            valid_smtps.append(f"{host}:{port}:{user}:{pwd}")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"✅ SMTP سالم: `{host}:{port}`\n"
                        f"کاربر: `{user}`\n"
                        f"رمز: `{pwd}`"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"ارسال پیام سالم ناموفق: {e}")
        else:
            fail_line = f"{host}:{port}:{user}:{pwd} | {msg}"
            failed_details.append(fail_line)

        completed += 1

        if completed % 30 == 0 or completed == total:
            try:
                await status_msg.edit_text(
                    f"⏳ پیشرفت: {completed}/{total}\n"
                    f"✅ سالم: {len(valid_smtps)}\n"
                    f"❌ ناموفق: {len(failed_details)}\n"
                    f"/cancel برای توقف"
                )
            except:
                pass

    tasks = []
    try:
        for host, port, user, pwd in smtp_list:
            if cancel_event.is_set():
                break
            task = asyncio.create_task(worker(host, port, user, pwd))
            tasks.append(task)
            user_task_list[chat_id].append(task)
            await asyncio.sleep(DELAY_BETWEEN_TASKS)

        if tasks and not cancel_event.is_set():
            await asyncio.gather(*tasks, return_exceptions=True)

    except asyncio.CancelledError:
        logger.info("Main task cancelled.")
    except Exception as e:
        logger.exception("خطای غیرمنتظره در تست")
        error_occurred = True
        error_message = str(e)
    finally:
        if chat_id in user_active_tasks:
            del user_active_tasks[chat_id]
        if chat_id in user_task_list:
            del user_task_list[chat_id]

        # برداشتن پین
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=status_msg.message_id)
        except:
            pass

        # ساخت فایل‌های نهایی
        files_to_send = []

        if valid_smtps:
            valid_content = "\n".join(valid_smtps)
            valid_file = BytesIO(valid_content.encode("utf-8"))
            valid_file.name = "valid_smtps.txt"
            files_to_send.append((valid_file, f"📁 {len(valid_smtps)} SMTP سالم (ارسال موفق)"))

        if failed_details:
            failed_content = "\n".join(failed_details)
            failed_file = BytesIO(failed_content.encode("utf-8"))
            failed_file.name = "failed_smtps_with_reasons.txt"
            files_to_send.append((failed_file, f"📁 {len(failed_details)} SMTP ناموفق با دلایل دقیق"))

        if error_occurred:
            zip_files = {}
            if valid_smtps:
                zip_files["valid_smtps_partial.txt"] = "\n".join(valid_smtps)
            if failed_details:
                zip_files["failed_details.txt"] = "\n".join(failed_details)
            zip_files["error.txt"] = f"Error during testing:\n{error_message}"
            zip_file = create_zip_in_memory(zip_files)
            zip_file.name = "smtp_error_report.zip"
            files_to_send.append((zip_file, "⚠️ خطا در فرایند. گزارش کامل پیوست شد."))

        for file_obj, caption in files_to_send:
            try:
                await context.bot.send_document(chat_id=ADMIN_ID, document=file_obj, caption=caption)
            except Exception as e:
                logger.error(f"ارسال فایل {file_obj.name} ناموفق: {e}")

        final_text = (
            f"✅ تست‌ها به پایان رسید.\n"
            f"🔢 کل: {total}\n"
            f"💚 سالم: {len(valid_smtps)}\n"
            f"❌ ناموفق: {len(failed_details)}"
        )
        await status_msg.edit_text(final_text)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("ربات شروع به کار کرد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()