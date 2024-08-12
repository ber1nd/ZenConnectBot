import os
import socket
import asyncio
import fcntl
import sys
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from datetime import time, timezone, datetime, timedelta
import mysql.connector
from mysql.connector import Error
from aiohttp import web
import json
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()  # Load environment variables from .env file

# Set up your OpenAI client using environment variables
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Rate limiting
RATE_LIMIT = 5  # messages per minute
rate_limit_dict = defaultdict(list)

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

def setup_database():
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    chat_type ENUM('private', 'group') DEFAULT 'private',
                    total_minutes INT DEFAULT 0,
                    zen_points INT DEFAULT 0,
                    daily_quote TINYINT(1) DEFAULT 0
                )
                """)
                
                # Check if columns exist, if not, add them
                columns_to_check = ['first_name', 'last_name', 'chat_type', 'daily_quote']
                for column in columns_to_check:
                    cursor.execute(f"""
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = 'users'
                    AND COLUMN_NAME = '{column}'
                    """)
                    if cursor.fetchone()[0] == 0:
                        if column in ['first_name', 'last_name']:
                            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} VARCHAR(255)")
                        elif column == 'chat_type':
                            cursor.execute("ALTER TABLE users ADD COLUMN chat_type ENUM('private', 'group') DEFAULT 'private'")
                        elif column == 'daily_quote':
                            cursor.execute("ALTER TABLE users ADD COLUMN daily_quote TINYINT(1) DEFAULT 0")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    group_id BIGINT,
                    memory TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                
                # Check if group_id column exists in user_memory table
                cursor.execute("""
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'user_memory'
                AND COLUMN_NAME = 'group_id'
                """)
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE user_memory ADD COLUMN group_id BIGINT")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS meditation_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    group_id BIGINT,
                    duration INT,
                    zen_points INT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_memberships (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    group_id BIGINT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """)
            connection.commit()
            print("Database setup completed successfully.")
        except Error as e:
            print(f"Error setting up database: {e}")
        finally:
            connection.close()
    else:
        print("Failed to connect to the database for setup.")

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
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("SELECT user_id FROM users WHERE daily_quote = 1")
            users = cursor.fetchall()
            for user in users:
                quote = await generate_response("Give me a short Zen quote.")
                await context.bot.send_message(chat_id=user[0], text=quote)
        except Error as e:
            print(f"Database error: {e}")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()

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
    
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("INSERT INTO meditation_log (user_id, duration, zen_points) VALUES (%s, %s, %s)", (user_id, duration, zen_points))
            cursor.execute("""
                INSERT INTO users (user_id, total_minutes, zen_points) 
                VALUES (%s, %s, %s) 
                ON DUPLICATE KEY UPDATE 
                total_minutes = total_minutes + %s, 
                zen_points = zen_points + %s
            """, (user_id, duration, zen_points, duration, zen_points))
            db.commit()
            await update.message.reply_text(f"Your meditation session is over. You earned {zen_points} Zen points!")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I'm sorry, there was an issue logging your meditation session.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, there was an issue logging your meditation session.")

async def check_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT total_minutes, zen_points FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            if result:
                total_minutes = result['total_minutes']
                zen_points = result['zen_points']
                message = f"Your Zen journey:\nTotal meditation time: {total_minutes} minutes\nZen points: {zen_points}"
                if chat_type == 'private':
                    mini_app_url = "https://zenconnectbot-production.up.railway.app/"
                    keyboard = [[InlineKeyboardButton("Open Zen Stats", web_app=WebAppInfo(url=mini_app_url))]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(message, reply_markup=reply_markup)
                else:
                    await update.message.reply_text(message)
            else:
                await update.message.reply_text("You haven't started your Zen journey yet. Try meditating to earn some points!")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I apologize, I'm having trouble accessing your stats right now. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def zen_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = await generate_response("Give me a Zen quote.")
    await update.message.reply_text(quote)

async def zen_advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    advice = await generate_response("Give me practical Zen advice for daily life.")
    await update.message.reply_text(advice)

async def random_wisdom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wisdom = await generate_response("Share a random piece of Zen wisdom.")
    await update.message.reply_text(wisdom)

def check_rate_limit(user_id):
    now = datetime.now()
    user_messages = rate_limit_dict[user_id]
    user_messages = [time for time in user_messages if now - time < timedelta(minutes=1)]
    rate_limit_dict[user_id] = user_messages
    return len(user_messages) < RATE_LIMIT

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    chat_type = update.message.chat.type
    group_id = update.message.chat.id if chat_type == 'group' else None

    # Apply rate limiting
    if not check_rate_limit(user_id):
        await update.message.reply_text("Please wait a moment before sending another message. Zen teaches us the value of patience.")
        return

    rate_limit_dict[user_id].append(datetime.now())

    db = get_db_connection()
    if not db:
        await update.message.reply_text("I'm having trouble accessing my memory right now. Please try again later.")
        return

    try:
        cursor = db.cursor()
        
        # Ensure chat_type is either 'private' or 'group'
        chat_type_db = 'private' if chat_type == 'private' else 'group'
        
        # Update or insert user information
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, chat_type)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            username = VALUES(username),
            first_name = VALUES(first_name),
            last_name = VALUES(last_name),
            chat_type = VALUES(chat_type)
        """, (user_id, update.effective_user.username, update.effective_user.first_name, 
              update.effective_user.last_name, chat_type_db))

        # If it's a group chat, update group membership
        if group_id:
            cursor.execute("""
                INSERT IGNORE INTO group_memberships (user_id, group_id)
                VALUES (%s, %s)
            """, (user_id, group_id))

        # Fetch user memory
        if group_id:
            cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s AND group_id = %s ORDER BY timestamp DESC LIMIT 5", (user_id, group_id))
        else:
            cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s AND group_id IS NULL ORDER BY timestamp DESC LIMIT 5", (user_id,))
        
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
        cursor.execute("INSERT INTO user_memory (user_id, group_id, memory) VALUES (%s, %s, %s)", (user_id, group_id, new_memory))
        db.commit()

        await update.message.reply_text(response)

    except Error as e:
        print(f"Database error in handle_message: {e}")
        await update.message.reply_text("I'm having trouble processing your message. Please try again later.")

    finally:
        if db.is_connected():
            cursor.close()
            db.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Greetings, seeker of wisdom. I am a Zen monk here to guide you on your path to enlightenment. How may I assist you today?')

async def togglequote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    if chat_type != 'private':
        await update.message.reply_text("This command can only be used in private chats. Please message me directly to toggle your daily quote subscription.")
        return

    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
cursor.execute("SELECT daily_quote FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            if result is None:
                new_status = 1
                cursor.execute("INSERT INTO users (user_id, daily_quote) VALUES (%s, %s)", (user_id, new_status))
            else:
                new_status = 1 if result[0] == 0 else 0
                cursor.execute("UPDATE users SET daily_quote = %s WHERE user_id = %s", (new_status, user_id))
            db.commit()
            if new_status == 1:
                await update.message.reply_text("You have chosen to receive daily nuggets of Zen wisdom. May they light your path.")
            else:
                await update.message.reply_text("You have chosen to pause the daily Zen quotes. Remember, wisdom is all around us, even in silence.")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I apologize, I'm having trouble updating your preferences. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def getchatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your unique identifier in this realm is: {update.effective_chat.id}")

async def delete_user_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("DELETE FROM user_memory WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM meditation_log WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM group_memberships WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            db.commit()
            await update.message.reply_text("Your data has been deleted from my memory. Your journey continues anew.")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I apologize, I'm having trouble deleting your data. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    Available commands:
    /start - Start interacting with the Zen Monk bot
    /togglequote - Subscribe/Unsubscribe to daily Zen quotes (private chat only)
    /zenstory - Hear a Zen story
    /meditate [minutes] - Start a meditation timer (default is 5 minutes)
    /zenquote - Receive a Zen quote
    /zenadvice - Get practical Zen advice
    /randomwisdom - Get a random piece of Zen wisdom
    /checkpoints - Check your meditation minutes and Zen points progress
    /getchatid - Get your unique Chat ID
    /deletedata - Delete all your data from the bot
    /help - Display this help message
    """
    await update.message.reply_text(help_text)

async def serve_mini_app(request):
    return web.FileResponse('./zen_stats.html')

async def get_user_stats(request):
    user_id = request.query.get('user_id')
    print(f"Fetching stats for user_id: {user_id}")  # Debug log
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            query = """
                SELECT total_minutes, zen_points, 
                       COALESCE(username, '') as username, 
                       COALESCE(first_name, '') as first_name, 
                       COALESCE(last_name, '') as last_name
                FROM users
                WHERE user_id = %s
            """
            print(f"Executing query: {query}")  # Debug log
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            print(f"Query result: {result}")  # Debug log
            if result:
                return web.json_response(result)
            else:
                print(f"User not found: {user_id}")  # Debug log
                return web.json_response({"error": "User not found", "user_id": user_id}, status=404)
        except Error as e:
            print(f"Database error: {e}")
            return web.json_response({"error": "Database error", "details": str(e)}, status=500)
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        print("Failed to connect to database")  # Debug log
        return web.json_response({"error": "Database connection failed"}, status=500)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Exception while handling an update: {context.error}")
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An error occurred while processing your request. Please try again later.")

def main():
    # File-based lock
    lock_file = '/tmp/zenconnect_bot.lock'
    
    try:
        lock_file_fd = open(lock_file, 'w')
        fcntl.lockf(lock_file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance of this bot is already running. Exiting.")
        sys.exit(1)

    setup_database()

    token = os.getenv("BOT_TOKEN")  # Use environment variable for the Telegram bot token
    application = Application.builder().token(token).build()
    
    # Command handlers
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
    application.add_handler(CommandHandler("deletedata", delete_user_data))
    
    # Message handler with custom filters
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (
            filters.ChatType.PRIVATE |
            (filters.ChatType.GROUPS & (
                filters.Regex(r'(?i)\bzen\b') |
                filters.Regex(r'@\w+')  # This will match any mention, we'll check for the bot's username in handle_message
            ))
        ),
        handle_message
    ))
    
    application.add_error_handler(error_handler)
    
    # Schedule the daily quote at a specific time (e.g., 8:00 AM UTC)
    if application.job_queue:
        application.job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    else:
        print("Warning: JobQueue is not available. Daily quotes will not be scheduled.")
    
    # Set up web app
    app = web.Application()
    app.router.add_get('/', serve_mini_app)
    app.router.add_get('/api/stats', get_user_stats)

    # Start bot and web server
    web_runner = web.AppRunner(app)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(web_runner.setup())
    site = web.TCPSite(web_runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    loop.run_until_complete(site.start())
    
    print("Zen Monk Bot has awakened. Press Ctrl+C to return to silence.")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()