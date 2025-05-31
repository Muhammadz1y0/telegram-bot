import asyncio
import asyncpg
import os
from telegram import ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from datetime import datetime
from pytz import timezone
import requests
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise ValueError("TELEGRAM_TOKEN and DATABASE_URL must be set in the .env file")

def get_prayer_times(region):
    url = f"https://api.aladhan.com/v1/timingsByCity?city={region}&country=Uzbekistan&method=2"
    response = requests.get(url)
    if response.status_code == 200:
        timings = response.json()['data']['timings']
        translated_timings = {
            "🕋 Bomdod": timings["Fajr"],
            "🌄 Quyosh chiqishi": timings["Sunrise"],
            "☀️️ Peshin": timings["Dhuhr"],
            "🌤️ Asr": timings["Asr"],
            "🌇 Shom": timings["Maghrib"],
            "🌌 Hufton": timings["Isha"],
        }
        return translated_timings
    return None

async def init_db():
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                region VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS regions (
                region_id SERIAL PRIMARY KEY,
                region_name VARCHAR(100) UNIQUE NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id SERIAL PRIMARY KEY,
                chat_id BIGINT REFERENCES users(chat_id),
                region_id INT REFERENCES regions(region_id),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        regions = [
            "Toshkent", "Samarqand", "Buxoro", "Xiva",
            "Andijon", "Namangan", "Farg'ona", "Navoiy",
            "Qashqadaryo", "Surxondaryo", "Sirdaryo",
            "Jizzax", "Qoraqalpog'iston"
        ]
        for region in regions:
            await conn.execute('''
                INSERT INTO regions (region_name)
                VALUES ($1)
                ON CONFLICT (region_name) DO NOTHING
            ''', region)
    return pool

async def add_user(pool, chat_id, username, region=None):
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (chat_id, username, region)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO NOTHING
        ''', chat_id, username, region)

async def update_user_region(pool, chat_id, region):
    async with pool.acquire() as conn:
        await conn.execute('''
            UPDATE users SET region = $1 WHERE chat_id = $2
        ''', region, chat_id)

async def get_user(pool, chat_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow('''
            SELECT * FROM users WHERE chat_id = $1
        ''', chat_id)

async def delete_user(pool, chat_id):
    async with pool.acquire() as conn:
        await conn.execute('''
            DELETE FROM users WHERE chat_id = $1
        ''', chat_id)

async def add_reminder(pool, chat_id, region_id):
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO reminders (chat_id, region_id)
            VALUES ($1, $2)
        ''', chat_id, region_id)

async def get_regions(pool):
    async with pool.acquire() as conn:
        return await conn.fetch('''
            SELECT region_name FROM regions
        ''')

async def get_region_id(pool, region_name):
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT region_id FROM regions WHERE region_name = $1
        ''', region_name)
        return row['region_id'] if row else None

async def get_user_reminders(pool, chat_id):
    async with pool.acquire() as conn:
        return await conn.fetch('''
            SELECT r.reminder_id, r.is_active, reg.region_name
            FROM reminders r
            JOIN regions reg ON r.region_id = reg.region_id
            WHERE r.chat_id = $1
        ''', chat_id)

async def get_region_stats(pool):
    async with pool.acquire() as conn:
        return await conn.fetch('''
            SELECT reg.region_name, COUNT(u.chat_id) as user_count
            FROM users u
            JOIN regions reg ON u.region = reg.region_name
            GROUP BY reg.region_name
            HAVING COUNT(u.chat_id) > 0
        ''')

async def format_regions_keyboard(pool):
    regions = await get_regions(pool)
    regions_list = [row['region_name'] for row in regions]
    keyboard = [regions_list[i:i + 3] for i in range(0, len(regions_list), 3)]
    return keyboard

async def start(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data['db_pool']
    reply_markup = ReplyKeyboardMarkup(await format_regions_keyboard(pool), resize_keyboard=True)
    await update.message.reply_text("Assalomu alaykum! Viloyatingizni tanlang:", reply_markup=reply_markup)

async def region_selection(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data['db_pool']
    region = update.message.text
    regions = [row['region_name'] for row in await get_regions(pool)]

    if region in regions:
        chat_id = update.message.chat_id
        username = update.message.from_user.username or "Unknown"
        await add_user(pool, chat_id, username, region)
        context.user_data['region'] = region
        prayer_times = get_prayer_times(region)
        if prayer_times:
            await update.message.reply_text(
                f"{region} uchun namoz vaqtlari:\n" +
                "\n".join([f"{key}: {value}" for key, value in prayer_times.items()])
            )
        else:
            await update.message.reply_text("Kechirasiz, namoz vaqtlarini olishda xatolik yuz berdi.")
    else:
        await update.message.reply_text("Iltimos, viloyatni tugmalardan tanlang.")

async def add_reminders(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data['db_pool']
    if 'region' in context.user_data:
        region = context.user_data['region']
        chat_id = update.message.chat_id
        region_id = await get_region_id(pool, region)
        if region_id:
            await add_reminder(pool, chat_id, region_id)
            context.job_queue.run_repeating(
                send_prayer_reminders,
                interval=60,
                first=0,
                data={'chat_id': chat_id, 'region': region}
            )
            await update.message.reply_text(f"✅ {region} uchun namoz vaqtlari eslatmalari yoqildi.")
        else:
            await update.message.reply_text("⚠️ Viloyat topilmadi.")
    else:
        await update.message.reply_text("⚠️ Iltimos, avval viloyatingizni tanlang.")

async def send_prayer_reminders(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    prayer_times = get_prayer_times(job.data['region'])
    if prayer_times:
        now = datetime.now(timezone('Asia/Tashkent')).strftime("%H:%M")
        for prayer, time in prayer_times.items():
            if now == time:
                await context.bot.send_message(
                    chat_id=job.data['chat_id'],
                    text=f"⚠️ {prayer} vaqti bo'ldi! Namozingizni o'tkazib yubormang. 🙏"
                )

async def stats(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data['db_pool']
    stats = await get_region_stats(pool)
    if stats:
        response = "Viloyatlar bo'yicha foydalanuvchilar:\n"
        for row in stats:
            response += f"{row['region_name']}: {row['user_count']} foydalanuvchi\n"
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("Hozircha statistika mavjud emas.")

async def main():
    pool = await init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data['db_pool'] = pool

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, region_selection))
    app.add_handler(CommandHandler("reminders", add_reminders))
    app.add_handler(CommandHandler("stats", stats))

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    except Exception as e:
        print(f"Error running bot: {e}")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await pool.close()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(main())
        else:
            loop.run_until_complete(main())
    except RuntimeError as e:
        print(f"Event loop error: {e}")