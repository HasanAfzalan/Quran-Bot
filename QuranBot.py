import sqlite3
import logging
import asyncio
from datetime import datetime, date
from jdatetime import datetime as jdatetime

from telegram import Update
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# initial settings
TELEGRAM_BOT_TOKEN = "BOT TOKEN"  # enter bot token
TARGET_GROUP_ID = -1001111111111  # enter group id
TARGET_TOPIC_ID = 2  # enter topic id
ACCESS_PASSWORD = "YourPassWord"

# timing settings
MORNING_POST_HOUR = 6
MORNING_POST_MINUTE = 0
PERSONAL_REMINDER_HOUR = 19
PERSONAL_REMINDER_MINUTE = 0
NIGHTLY_SUMMARY_HOUR = 23
NIGHTLY_SUMMARY_MINUTE = 58

# other settings
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
DB_NAME = "quran_daily_bot.db"
QURAN_PAGES = 604
IMAGE_URL_TEMPLATE = "http://itsee.ir/Files/Pictures/Quran/Pages/{}.jpg"
AUDIO_URL_TEMPLATE = "https://everyayah.com/data/Minshawy_Murattal_128kbps/PageMp3s/Page{}.mp3"

# coversation states
AWAITING_PASSWORD, AWAITING_USERNAME = range(2)


# database functions
def setup_database():
    """create new database with new instructure"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        password_verified BOOLEAN DEFAULT 0,
        total_pages_read INTEGER DEFAULT 0
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        page_number INTEGER NOT NULL,
        read_date DATE NOT NULL,
        UNIQUE(user_id, read_date)
    )""")
    cursor.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES (?, ?)", ('current_page_number', '1'))
    cursor.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES (?, ?)", ('current_khatm_number', '1'))
    conn.commit()
    conn.close()
    logger.info("Database setup complete.")


def get_bot_state(key: str, default=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else default


def set_bot_state(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def check_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ? AND password_verified = 1", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user is not None


def add_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, username, password_verified, total_pages_read) VALUES (?, ?, ?, COALESCE((SELECT total_pages_read FROM users WHERE user_id = ?), 0))",
        (user_id, username, 1, user_id)
    )
    conn.commit()
    conn.close()


def update_user_temp_status(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, password_verified) VALUES (?, ?, ?)",
                   (user_id, f'temp_{user_id}', 0))
    conn.commit()
    conn.close()


# login functions
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if check_user(user.id):
        await update.message.reply_text(
            "سلام! شما قبلاً ثبت‌نام کرده‌اید. برای ثبت قرائت امروز از دستور /read استفاده کنید.")
        return ConversationHandler.END
    else:
        update_user_temp_status(user.id)
        await update.message.reply_text("👋 سلام! به ربات روزانه ختم قرآن خوش آمدید.\n\nلطفاً رمز عبور را وارد کنید:")
        return AWAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == ACCESS_PASSWORD:
        await update.message.reply_text("✅ رمز عبور صحیح است!\n\nلطفاً یک نام کاربری برای خودتان وارد کنید:")
        return AWAITING_USERNAME
    else:
        await update.message.reply_text("❌ رمز عبور اشتباه است. لطفاً دوباره تلاش کنید:")
        return AWAITING_PASSWORD


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    username = update.message.text
    add_user(user.id, username)
    await update.message.reply_text(f"🎉 ثبت‌نام شما با نام کاربری «{username}» با موفقیت انجام شد.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END


# main functions

def _register_reading_sync(user_id: int, current_page: int, today: date) -> str:
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM daily_readings WHERE user_id = ? AND read_date = ?", (user_id, today))
        if cursor.fetchone():
            return f"شما قبلاً قرائت صفحه {current_page} را برای امروز ثبت کرده‌اید. التماس دعا."
        cursor.execute("INSERT INTO daily_readings (user_id, page_number, read_date) VALUES (?, ?, ?)",
                       (user_id, current_page, today))
        cursor.execute("UPDATE users SET total_pages_read = total_pages_read + 1 WHERE user_id = ?", (user_id,))
        cursor.execute("SELECT value FROM bot_state WHERE key = 'first_khatm_start_date'")
        if not cursor.fetchone():
            today_jalali = jdatetime.now().strftime('%Y/%m/%d')
            cursor.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                           ('first_khatm_start_date', today_jalali))
            cursor.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                           ('current_khatm_start_date', today_jalali))
        conn.commit()
        return f"✅ قرائت صفحه {current_page} برای شما ثبت شد. قبول باشه."
    except Exception as e:
        logger.error(f"DATABASE ERROR in _register_reading_sync: {e}")
        return "خطایی در ثبت اطلاعات رخ داد. لطفاً دوباره تلاش کنید."
    finally:
        if conn:
            conn.close()


async def read_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_user(user_id):
        await update.message.reply_text("شما هنوز ثبت‌نام نکرده‌اید. لطفاً ابتدا دستور /start را ارسال کنید.")
        return
    current_page = int(get_bot_state('current_page_number'))
    today = date.today()
    response_message = await asyncio.to_thread(_register_reading_sync, user_id, current_page, today)
    await update.message.reply_text(response_message)


async def record_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_user(user_id):
        await update.message.reply_text("برای مشاهده گزارش ابتدا باید با دستور /start ثبت‌نام کنید.")
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    khatm_num = get_bot_state('current_khatm_number', '1')
    first_start = get_bot_state('first_khatm_start_date', 'هنوز شروع نشده')
    current_start = get_bot_state('current_khatm_start_date', 'هنوز شروع نشده')
    cursor.execute("SELECT COUNT(DISTINCT read_date) FROM daily_readings")
    total_days = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(total_pages_read) FROM users")
    total_pages_all_users = cursor.fetchone()[0] or 0
    cursor.execute("SELECT MAX(total_pages_read) FROM users")
    max_pages_result = cursor.fetchone()
    top_readers_str = "هنوز قرائتی ثبت نشده"
    if max_pages_result and max_pages_result[0] is not None and max_pages_result[0] > 0:
        max_pages = max_pages_result[0]
        cursor.execute("SELECT username FROM users WHERE total_pages_read = ?", (max_pages,))
        top_readers = [row[0] for row in cursor.fetchall()]
        top_readers_str = f'{"، ".join(top_readers)} ({max_pages} صفحه)'
    conn.close()
    message = (
        "📊 **گزارش پویش ختم قرآن**\n\n"
        f"💠 **دور ختم فعلی:** {khatm_num}\n"
        f"🗓 **تاریخ شروع پویش:** {first_start}\n"
        f"📅 **تاریخ شروع دور فعلی:** {current_start}\n"
        f"⏳ **مجموع روزهای سپری شده:** {total_days} روز\n"
        f"📖 **مجموع کل صفحات قرائت شده:** {total_pages_all_users} صفحه\n"
        f"🏆 **بیشترین مشارکت:** {top_readers_str}"
    )
    await update.message.reply_text(message, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "راهنمای ربات روزانه ختم قرآن:\n\n"
        "/start - شروع کار و ثبت‌نام اولیه (فقط یکبار)\n"
        "/read - ثبت قرائت صفحه امروز (در چت خصوصی)\n"
        "/record - نمایش گزارش کلی از وضعیت پویش\n"
        "/help - نمایش همین پیام راهنما"
    )
    await update.message.reply_text(message)


# scheduled functions
async def send_daily_page(context: ContextTypes.DEFAULT_TYPE):
    """
    send daily message for group and all users
    """
    current_page = int(get_bot_state('current_page_number'))
    page_str_padded = str(current_page).zfill(3)

    image_url = IMAGE_URL_TEMPLATE.format(page_str_padded)
    audio_url = AUDIO_URL_TEMPLATE.format(page_str_padded)

    bot = context.bot
    bot_user = await bot.get_me()
    bot_username = bot_user.username

    message_text = (
        f"☀️ **قرار روزانه ختم قرآن** ☀️\n\n"
        f"📖 **صفحه امروز: {current_page}**\n\n"
        "پس از قرائت، لطفاً برای ثبت به ربات مراجعه کرده و دستور /read را ارسال کنید:\n"
        f"➡️ @{bot_username}"
    )

    # 1: send messages to group
    try:
        await bot.send_message(
            chat_id=TARGET_GROUP_ID,
            message_thread_id=TARGET_TOPIC_ID,
            text=message_text,
            parse_mode='Markdown'
        )
        await bot.send_photo(chat_id=TARGET_GROUP_ID, message_thread_id=TARGET_TOPIC_ID, photo=image_url,
                             caption=f"تصویر صفحه {current_page}")
        await bot.send_audio(chat_id=TARGET_GROUP_ID, message_thread_id=TARGET_TOPIC_ID, audio=audio_url,
                             title=f"تلاوت صفحه {current_page}", caption=f"صوت تلاوت صفحه {current_page}")
        logger.info(f"Daily content for page {current_page} sent to the group successfully.")
    except Exception as e:
        logger.error(f"Failed to send daily content to the group. Error: {e}")

    # 2: send messages for users
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE password_verified = 1")
    all_user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    logger.info(f"Starting to send personal daily messages to {len(all_user_ids)} users.")

    for user_id in all_user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=message_text, parse_mode='Markdown')
            await bot.send_photo(chat_id=user_id, photo=image_url, caption=f"تصویر صفحه {current_page}")
            await bot.send_audio(chat_id=user_id, audio=audio_url, title=f"تلاوت صفحه {current_page}",
                                 caption=f"صوت تلاوت صفحه {current_page}")
            await asyncio.sleep(0.1)  # فاصله کوتاه برای جلوگیری از اسپم شدن
        except Forbidden:
            logger.warning(f"User {user_id} has blocked the bot. Skipping.")
        except BadRequest:
            logger.warning(f"Chat with user {user_id} not found. Skipping.")
        except Exception as e:
            logger.error(f"Failed to send personal daily content to user {user_id}. Error: {e}")

    logger.info("Finished sending personal daily messages.")


# more scheduled functions
async def send_personal_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    current_page = get_bot_state('current_page_number')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE password_verified = 1")
    all_users = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT user_id FROM daily_readings WHERE read_date = ?", (today,))
    read_users = {row[0] for row in cursor.fetchall()}
    conn.close()
    users_to_remind = all_users - read_users
    logger.info(f"Found {len(users_to_remind)} users to remind.")
    reminder_message = f"سلام، یادآوری قرائت صفحه {current_page} 📖\nفرصت رو از دست ندید. التماس دعا."
    for user_id in users_to_remind:
        try:
            await context.bot.send_message(chat_id=user_id, text=reminder_message)
        except Exception as e:
            logger.warning(f"Could not send reminder to user {user_id}. Error: {e}")


async def send_daily_summary_and_advance_day(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    current_page = int(get_bot_state('current_page_number'))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.username FROM daily_readings dr
        JOIN users u ON dr.user_id = u.user_id
        WHERE dr.read_date = ?
    """, (today,))
    participants = [row[0] for row in cursor.fetchall()]
    conn.close()
    if participants:
        participants_list = "\n- ".join(participants)
        summary_message = (
            f"🌙 **گزارش پایانی قرائت صفحه {current_page}**\n\n"
            "از عزیزانی که امروز در این پویش نورانی شرکت کردند صمیمانه سپاسگزاریم:\n\n"
            f"- {participants_list}\n\n"
            "طاعاتتون قبول. التماس دعا 🙏"
        )
    else:
        summary_message = (
            f"🌙 گزارش پایانی قرائت صفحه {current_page}\n\n"
            "امروز مشارکتی ثبت نشد. ان‌شاءالله فردا با انرژی بیشتر ادامه می‌دهیم."
        )
    try:
        await context.bot.send_message(
            chat_id=TARGET_GROUP_ID,
            message_thread_id=TARGET_TOPIC_ID,
            text=summary_message
        )
        logger.info("Daily summary sent.")
    except Exception as e:
        logger.error(f"Failed to send daily summary. Error: {e}")

    next_page = current_page + 1
    if next_page > QURAN_PAGES:
        next_page = 1
        current_khatm = int(get_bot_state('current_khatm_number', '1'))
        next_khatm = current_khatm + 1
        set_bot_state('current_khatm_number', str(next_khatm))
        set_bot_state('current_khatm_start_date', jdatetime.now().strftime('%Y/%m/%d'))
        khatm_completion_message = (
            f"🎉 **تبریک! دور {current_khatm} ام ختم قرآن با موفقیت به پایان رسید.** 🎉\n\n"
            "از مشارکت همه شما عزیزان سپاسگزاریم. ان‌شاءالله از فردا دور جدید را با هم شروع می‌کنیم."
        )
        try:
            await context.bot.send_message(
                chat_id=TARGET_GROUP_ID, message_thread_id=TARGET_TOPIC_ID, text=khatm_completion_message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send khatm completion message. Error: {e}")
    set_bot_state('current_page_number', str(next_page))
    logger.info(f"Advanced to the next day. New page is {next_page}.")


async def post_init(application: Application):
    """start timing after runnig bot"""
    scheduler = AsyncIOScheduler(timezone="Asia/Tehran")
    scheduler.add_job(send_daily_page, 'cron', hour=MORNING_POST_HOUR, minute=MORNING_POST_MINUTE, args=[application])
    scheduler.add_job(send_personal_reminders, 'cron', hour=PERSONAL_REMINDER_HOUR, minute=PERSONAL_REMINDER_MINUTE,
                      args=[application])
    scheduler.add_job(send_daily_summary_and_advance_day, 'cron', hour=NIGHTLY_SUMMARY_HOUR,
                      minute=NIGHTLY_SUMMARY_MINUTE, args=[application])
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started.")


def main() -> None:
    """run bot"""
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("!!! لطفا توکن ربات خود را وارد کنید !!!")
        return

    setup_database()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    registration_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            AWAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(registration_conv)
    application.add_handler(CommandHandler("read", read_command))
    application.add_handler(CommandHandler("record", record_command))
    application.add_handler(CommandHandler("help", help_command))

    print("Daily bot with personal messaging is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
