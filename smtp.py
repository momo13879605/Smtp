import asyncio
import logging
import zipfile
import random
import re
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, formataddr
from io import BytesIO
from typing import List, Tuple, Dict
import time

import smtplib
import socket

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode

# ==================== تنظیمات ====================
BOT_TOKEN = "7413084969:AAHglr2N6eO_9VxhGCepns0iWKr9nYgmDZg"                 # ← توکن ربات از @BotFather
ADMIN_ID = 5914346958                        # ← آیدی عددی شما
DEFAULT_TEST_RECIPIENT = "cowacik720@ocuser.com"  # ایمیل مقصد پیش‌فرض

TIMEOUT_SMTP = 8                            # تایم‌اوت هر تست (ثانیه)
MAX_CONCURRENT_TASKS = 10                   # تعداد تست هم‌زمان
DELAY_BETWEEN_TASKS = 0.2                   # تأخیر بین شروع تسک‌ها (کنترل نرخ ورود)
PER_DOMAIN_DELAY = 2.0                      # حداقل فاصله (ثانیه) بین تست‌های یک دامنه
SMTP_DEBUG_LEVEL = 0                        # 0 = خاموش, 1 = نمایش دستورات SMTP

MAX_RETRIES = 3                             # حداکثر تلاش برای خطاهای گذرا
RETRY_BACKOFF_BASE = 2.0                    # ضریب تأخیر تصاعدی
RETRY_JITTER = 0.5                          # جیتر تصادفی
# =================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ذخیره‌سازی ایمیل مقصد برای هر کاربر (فقط ادمین)
user_target_email: Dict[int, str] = {}

# کنترل نرخ دامنه
domain_last_test: Dict[str, float] = {}
domain_lock = asyncio.Lock()

# نگهداری وظایف برای کنسل شدن
user_active_tasks: dict[int, asyncio.Event] = {}
user_task_list: dict[int, list[asyncio.Task]] = {}

# مراحل ConversationHandler برای تغییر ایمیل
WAITING_FOR_EMAIL = 1


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


def is_transient_error(error_msg: str) -> bool:
    transient_keywords = [
        'timeout', 'timed out', 'connection reset', 'connection refused',
        'too many concurrent', 'dns', 'name or service not known',
        'connection unexpectedly closed', 'response took too long',
        'no response from server', 'overall timeout',
        'the read operation timed out', 'try again later'
    ]
    return any(keyword in error_msg.lower() for keyword in transient_keywords)


def test_and_send_smtp(host: str, port: int, user: str, password: str,
                       recipient: str) -> Tuple[bool, str]:
    """
    تلاش برای لاگین و ارسال ایمیل تست با تلاش مجدد برای خطاهای گذرا.
    """
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        server = None
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SMTP)
            else:
                server = smtplib.SMTP(host, port, timeout=TIMEOUT_SMTP)
                server.ehlo()
                if server.has_extn('STARTTLS'):
                    server.starttls()
                    server.ehlo()

            if SMTP_DEBUG_LEVEL > 0:
                server.set_debuglevel(SMTP_DEBUG_LEVEL)

            server.login(user, password)

            msg = MIMEText(
                "This is a delivery test. If you receive this, the SMTP is healthy.",
                'plain', 'utf-8'
            )
            msg['Subject'] = "SMTP Delivery Test"
            msg['From'] = formataddr((None, user))
            msg['To'] = recipient
            msg['Date'] = formatdate(localtime=True)
            msg['Message-ID'] = make_msgid(domain=host.split('.')[-2] + '.' + host.split('.')[-1] if '.' in host else "smtp.test")

            server.sendmail(user, recipient, msg.as_string())
            return True, "ارسال موفق"

        except smtplib.SMTPAuthenticationError as e:
            return False, f"Authentication failed: {e.smtp_code} {e.smtp_error}"
        except smtplib.SMTPRecipientsRefused as e:
            return False, f"Recipient refused: {e.recipients}"
        except smtplib.SMTPSenderRefused as e:
            return False, f"Sender refused: {e.smtp_code} {e.smtp_error}"
        except smtplib.SMTPDataError as e:
            return False, f"Data error: {e.smtp_code} {e.smtp_error}"
        except smtplib.SMTPHeloError as e:
            return False, f"HELO/EHLO error: {e.smtp_code} {e.smtp_error}"
        except smtplib.SMTPNotSupportedError as e:
            return False, f"Feature not supported: {e}"
        except smtplib.SMTPConnectError as e:
            last_error = f"Connection error: {e.smtp_code} {e.smtp_error}"
        except smtplib.SMTPException as e:
            last_error = f"General SMTP error: {e}"
        except socket.timeout:
            last_error = "Connection timeout (no response from server)"
        except socket.gaierror as e:
            last_error = f"DNS/Address error: {e}"
        except ConnectionRefusedError:
            last_error = "Connection refused by server"
        except asyncio.TimeoutError:
            last_error = "Overall timeout (response took too long)"
        except Exception as e:
            last_error = f"Unknown error: {e}"
        finally:
            if server:
                try:
                    server.quit()
                except:
                    pass

        if attempt < MAX_RETRIES and is_transient_error(last_error):
            wait = RETRY_BACKOFF_BASE ** attempt + random.uniform(0, RETRY_JITTER)
            logger.info(f"Retry {attempt+1}/{MAX_RETRIES} for {host}:{port} after {wait:.1f}s")
            time.sleep(wait)
        else:
            break

    return False, last_error


def create_zip_in_memory(files: dict[str, str]) -> BytesIO:
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    zip_buffer.seek(0)
    return zip_buffer


async def wait_for_domain(host: str):
    domain = host.split('.')[-2] + '.' + host.split('.')[-1] if '.' in host else host
    async with domain_lock:
        now = time.time()
        last_time = domain_last_test.get(domain, 0)
        wait = PER_DOMAIN_DELAY - (now - last_time)
        if wait > 0:
            logger.info(f"Waiting {wait:.1f}s for domain {domain}")
            await asyncio.sleep(wait)
        domain_last_test[domain] = time.time()


def get_target_email(user_id: int) -> str:
    """دریافت ایمیل مقصد برای یک کاربر خاص"""
    return user_target_email.get(user_id, DEFAULT_TEST_RECIPIENT)


# ==================== کیبورد دکمه‌ای ====================

CHANGE_EMAIL_BUTTON = "📧 تغییر ایمیل مقصد"

admin_keyboard = ReplyKeyboardMarkup(
    [[CHANGE_EMAIL_BUTTON]], resize_keyboard=True, one_time_keyboard=False
)


# ==================== هندلرهای ربات ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return

    target = get_target_email(ADMIN_ID)
    await update.message.reply_text(
        f"🤖 **ربات تست SMTP**\n\n"
        f"📧 ایمیل مقصد فعلی: `{target}`\n"
        f"برای تغییر آن از دکمه‌ی زیر استفاده کنید.\n\n"
        f"لطفاً فایل txt حاوی SMTP ها را ارسال کنید.\n"
        f"تنظیمات: هم‌زمان={MAX_CONCURRENT_TASKS}, تایم‌اوت={TIMEOUT_SMTP}s\n"
        f"🔄 حداکثر تلاش مجدد برای خطاهای گذرا: {MAX_RETRIES}\n"
        f"/cancel برای توقف تست",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard
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
        await update.message.reply_text("🛑 فرایند تست کنسل شد.")
    else:
        await update.message.reply_text("ℹ️ هیچ تستی در حال اجرا نیست.")


# ---------- تغییر ایمیل مقصد (دو مرحله) ----------

async def change_email_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فعال شدن دکمه تغییر ایمیل"""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    await update.message.reply_text(
        "📧 لطفاً ایمیل جدید را وارد کنید:",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_FOR_EMAIL


async def change_email_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت ایمیل جدید و ذخیره آن"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    new_email = update.message.text.strip()
    # اعتبارسنجی ساده ایمیل
    if not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
        await update.message.reply_text(
            "❌ فرمت ایمیل نادرست است. لطفاً دوباره تلاش کنید.",
            reply_markup=admin_keyboard
        )
        return ConversationHandler.END

    user_target_email[user_id] = new_email
    await update.message.reply_text(
        f"✅ ایمیل مقصد با موفقیت به `{new_email}` تغییر یافت.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard
    )
    return ConversationHandler.END


async def change_email_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """انصراف از تغییر ایمیل"""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    await update.message.reply_text(
        "❌ عملیات تغییر ایمیل لغو شد.",
        reply_markup=admin_keyboard
    )
    return ConversationHandler.END


change_email_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex(f"^{CHANGE_EMAIL_BUTTON}$") & filters.Chat(ADMIN_ID), change_email_start)],
    states={
        WAITING_FOR_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_email_received)]
    },
    fallbacks=[CommandHandler("cancel", change_email_cancel)],
)


# ---------- دریافت فایل و شروع تست ----------

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

    target_email = get_target_email(user_id)

    status_msg = await update.message.reply_text(
        f"📥 فایل دریافت شد.\n"
        f"🔢 تعداد SMTP: {total}\n"
        f"📧 ایمیل مقصد: `{target_email}`\n"
        f"⚙️ هم‌زمان={MAX_CONCURRENT_TASKS}, تایم‌اوت={TIMEOUT_SMTP}s\n"
        f"🔄 تلاش مجدد: {MAX_RETRIES}\n"
        f"⏳ تست‌ها شروع می‌شوند...\n"
        f"/cancel برای توقف",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard
    )

    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=status_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Pin failed: {e}")

    cancel_event = asyncio.Event()
    user_active_tasks[chat_id] = cancel_event
    user_task_list[chat_id] = []

    valid_smtps: List[str] = []
    failed_log_lines: List[str] = []
    error_occurred = False
    error_message = ""
    completed = 0

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    async def worker(host: str, port: int, user: str, pwd: str):
        nonlocal completed, valid_smtps, failed_log_lines
        if cancel_event.is_set():
            return

        await wait_for_domain(host)

        async with semaphore:
            if cancel_event.is_set():
                return

            loop = asyncio.get_running_loop()
            try:
                success, msg = await asyncio.wait_for(
                    loop.run_in_executor(None, test_and_send_smtp, host, port, user, pwd, target_email),
                    timeout=TIMEOUT_SMTP * 2 + 10
                )
            except asyncio.TimeoutError:
                success, msg = False, "Overall timeout after retries"
            except Exception as e:
                success, msg = False, f"System error: {e}"

        if success:
            valid_smtps.append(f"{host}:{port}:{user}:{pwd}")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"✅ SMTP سالم پیدا شد:\n"
                        f"هاست: {host}\n"
                        f"پورت: {port}\n"
                        f"کاربر: {user}\n"
                        f"رمز: {pwd}"
                    )
                )
            except Exception as e:
                logger.error(f"ارسال پیام سالم ناموفق: {e}")
        else:
            fail_line = f"{host}:{port}:{user}:{pwd} | Reason: {msg}"
            failed_log_lines.append(fail_line)

        completed += 1

        if completed % 30 == 0 or completed == total:
            try:
                await status_msg.edit_text(
                    f"⏳ پیشرفت: {completed}/{total}\n"
                    f"✅ سالم: {len(valid_smtps)}\n"
                    f"❌ ناموفق: {len(failed_log_lines)}\n"
                    f"/cancel برای توقف",
                    parse_mode=ParseMode.MARKDOWN
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

        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=status_msg.message_id)
        except:
            pass

        files_to_send = []

        if valid_smtps:
            valid_content = "\n".join(valid_smtps)
            valid_file = BytesIO(valid_content.encode("utf-8"))
            valid_file.name = "valid_smtps.txt"
            files_to_send.append((valid_file, f"📁 {len(valid_smtps)} SMTP سالم (ارسال موفق)"))

        if failed_log_lines:
            failed_content = "\n".join(failed_log_lines)
            failed_file = BytesIO(failed_content.encode("utf-8"))
            failed_file.name = "failed_smtps_detailed.log"
            files_to_send.append((failed_file, f"📁 {len(failed_log_lines)} SMTP ناموفق با دلایل دقیق"))

        if error_occurred:
            zip_files = {}
            if valid_smtps:
                zip_files["valid_smtps_partial.txt"] = "\n".join(valid_smtps)
            if failed_log_lines:
                zip_files["failed_details.log"] = "\n".join(failed_log_lines)
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
            f"❌ ناموفق: {len(failed_log_lines)}"
        )
        await status_msg.edit_text(final_text)
        # بازگرداندن کیبورد
        await context.bot.send_message(chat_id=chat_id, text="می‌توانید فایل دیگری ارسال کنید.", reply_markup=admin_keyboard)


# ==================== اجرای ربات ====================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(change_email_conv)  # ConversationHandler تغییر ایمیل
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("ربات شروع به کار کرد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()