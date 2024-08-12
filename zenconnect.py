import os
import socket
import asyncio
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from datetime import time, timezone
import mysql.connector
from mysql.connector import Error
from aiohttp import web

# Socket-based lock
LOCK_SOCKET = None
LOCK_SOCKET_ADDRESS = ("localhost", 47200)  # Choose an arbitrary port number

def is_already_running():
    global LOCK_SOCKET
    LOCK_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        LOCK_SOCKET.bind(LOCK_SOCKET_ADDRESS)
        return False
    except socket.error:
        return True

# Set up your OpenAI client using environment variables
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Your personal chat ID (use environment variable)
YOUR_CHAT_ID = int(os.getenv("CHAT_ID"))

def get_db_connection():
    try:
        return mysql.connector.connect(
            host=os.getenv("MYSQLHOST"),
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQLPORT", 3306))
        )
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
        return None

async def generate_response(prompt, elaborate=False):
    try:
        max_tokens = 150 if elaborate else 50
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a wise Zen monk. Provide concise, insightful responses unless asked for elaboration."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating response: {type(e).__name__}: {str(e)}")
        return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

async def send_daily_quote(context: ContextTypes.DEFAULT_TYPE):
    if YOUR_CHAT_ID:
        quote = await generate_response("Give me a short Zen quote.")
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=quote)

async def zen_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    story = await generate_response("Tell me a short Zen story.")
    await update.message.reply_text(story)

async def meditate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        duration = int(context.args[0]) if context.args else 5  # Default to 5 minutes
        if duration <= 0:
            raise ValueError("Meditation duration must be a positive number.")
    except ValueError as e:
        await update.message.reply_text(f"Invalid duration: {str(e)}. Please provide a positive number of minutes.")
        return

    await update.message.reply_text(f"Start meditating for {duration} minutes. Focus on your breath.")
    
    interval = 2  # Interval in minutes
    total_intervals = duration // interval
    
    for i in range(total_intervals):
        await asyncio.sleep(interval * 60)  # Wait for the interval duration
        motivational_message = await generate_response("Give me a short Zen meditation guidance message.")
        await update.message.reply_text(motivational_message)
    
    await asyncio.sleep((duration % interval) * 60)  # Sleep for the remaining time
    zen_points = duration + (5 if duration > 15 else 0)  # 1 point per minute, +5 for sessions > 15 minutes
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("INSERT INTO meditation_log (user_id, duration, zen_points) VALUES (%s, %s, %s)", (update.effective_chat.id, duration, zen_points))
            cursor.execute("INSERT INTO users (user_id, total_minutes, zen_points) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE total_minutes = total_minutes + %s, zen_points = zen_points + %s", 
                           (update.effective_chat.id, duration, zen_points, duration, zen_points))
            db.commit()
            await update.message.reply_text(f"Your meditation session is over. You earned {zen_points} Zen points!")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I'm sorry, there was an issue logging your meditation session.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()

def create_progress_bar(points):
    total_blocks = 20  # Total length of the progress bar
    filled_blocks = int((points % 100) / 5)  # 5 points per block, reset every 100 points
    empty_blocks = total_blocks - filled_blocks
    return f"[{'█' * filled_blocks}{'░' * empty_blocks}] {points % 100}/100 Zen Points"

async def check_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mini_app_url = "https://zenconnectbot-production.up.railway.app"
    keyboard = [[InlineKeyboardButton("Open Zen Stats", web_app=WebAppInfo(url=mini_app_url))]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Click the button below to view your Zen stats, or use the menu button for quick access anytime:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "upgrade":
        await query.answer()
        await query.message.reply_text("To unlock higher levels and additional features, please subscribe.")
        # Here you would integrate with Telegram's payment system to handle the subscription process

async def zen_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = await generate_response("Give me a Zen quote.")
    await update.message.reply_text(quote)

async def zen_advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    advice = await generate_response("Give me practical Zen advice for daily life.")
    await update.message.reply_text(advice)

async def random_wisdom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wisdom = await generate_response("Share a random piece of Zen wisdom.")
    await update.message.reply_text(wisdom)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else

    user_id = update.effective_chat.id
    user_message = update.message.text

    db = get_db_connection()
    if not db:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")
        return

    try:
        cursor = db.cursor()
        
        cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 5", (user_id,))
        results = cursor.fetchall()

        memory = "\n".join([result[0] for result in results[::-1]]) if results else ""
        
        elaborate = any(word in user_message.lower() for word in ['why', 'how', 'explain', 'elaborate', 'tell me more'])
        
        prompt = f"""You are a wise Zen monk having a conversation with a student. 
        Here's the recent conversation history:

        {memory}

        Student: {user_message}
        Zen Monk: """

        response = await generate_response(prompt, elaborate)

        new_memory = f"Student: {user_message}\nZen Monk: {response}"
        cursor.execute("INSERT INTO user_memory (user_id, memory) VALUES (%s, %s)", (user_id, new_memory))
        db.commit()

        await update.message.reply_text(response)

    except Error as e:
        print(f"Database error: {e}")
        await update.message.reply_text("I apologize, I'm having trouble remembering our conversation. Let's continue anyway.")

    finally:
        if db.is_connected():
            cursor.close()
            db.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else
    await update.message.reply_text('Greetings, seeker of wisdom. I am a Zen monk here to guide you on your path to enlightenment. How may I assist you today?')

async def togglequote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else

    if 'daily_quote_active' not in context.bot_data:
        context.bot_data['daily_quote_active'] = True
        await update.message.reply_text("You have chosen to receive daily nuggets of Zen wisdom. May they light your path.")
    else:
        del context.bot_data['daily_quote_active']
        await update.message.reply_text("You have chosen to pause the daily Zen quotes. Remember, wisdom is all around us, even in silence.")

async def getchatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your unique identifier in this realm is: {update.effective_chat.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    Available commands:
    /start - Start interacting with the Zen Monk bot
    /togglequote - Subscribe/Unsubscribe to daily Zen quotes
        /zenstory - Hear a Zen story
    /meditate [minutes] - Start a meditation timer (default is 5 minutes)
    /zenquote - Receive a Zen quote
    /zenadvice - Get practical Zen advice
    /randomwisdom - Get a random piece of Zen wisdom
    /checkpoints - Check your meditation minutes and Zen points progress
    /getchatid - Get your unique Chat ID
    /help - Display this help message
    """
    await update.message.reply_text(help_text)

async def serve_mini_app(request):
    return web.FileResponse('zen_stats.html')

async def get_user_stats(request):
    user_id = request.query.get('user_id')
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT total_minutes, zen_points FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            if result:
                return web.json_response(result)
            else:
                return web.json_response({"total_minutes": 0, "zen_points": 0})
        except Error as e:
            print(f"Database error: {e}")
            return web.json_response({"error": "Database error"}, status=500)
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    return web.json_response({"error": "Database connection failed"}, status=500)

async def setup_bot(application: Application):
    await application.bot.set_my_commands([
        ("start", "Start the bot"),
        ("help", "Show available commands"),
        ("meditate", "Start a meditation session"),
        ("checkpoints", "View your Zen stats"),
    ])

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("togglequote", togglequote))
    application.add_handler(CommandHandler("getchatid", getchatid))
    application.add_handler(CommandHandler("zenstory", zen_story))
    application.add_handler(CommandHandler("meditate", meditate))
    application.add_handler(CommandHandler("zenquote", zen_quote))
    application.add_handler(CommandHandler("zenadvice", zen_advice))
    application.add_handler(CommandHandler("randomwisdom", random_wisdom))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    if application.job_queue:
        application.job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    else:
        print("Warning: JobQueue is not available. Daily quotes will not be scheduled.")

async def main():
    if is_already_running():
        print("Another instance of this bot is already running. Exiting.")
        return

    # Create tables if not exist
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    memory TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS meditation_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    duration INT,
                    zen_points INT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    total_minutes INT DEFAULT 0,
                    zen_points INT DEFAULT 0
                )
                """)
            connection.commit()
        except Error as e:
            print(f"Error creating tables: {e}")
        finally:
            connection.close()

    token = os.getenv("BOT_TOKEN")
    application = Application.builder().token(token).build()
    
    await setup_bot(application)
    
    # Set up web app
    app = web.Application()
    app.router.add_get('/', serve_mini_app)
    app.router.add_get('/api/stats', get_user_stats)

    # Start bot and web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    
    print("Zen Monk Bot has awakened. Press Ctrl+C to return to silence.")
    await application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if str(e) == "This event loop is already running":
            print("Detected a running event loop, reusing it.")
            loop = asyncio.get_running_loop()
            loop.run_until_complete(main())
        else:
            raise