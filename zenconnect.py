import os
import socket
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from datetime import time, timezone
import mysql.connector
from mysql.connector import Error

# Set up your OpenAI client using environment variables
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Your personal chat ID (use environment variable)
YOUR_CHAT_ID = int(os.getenv("CHAT_ID"))

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

async def generate_response(prompt):
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,  # Increased for more detailed responses
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating response: {e}")
        return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

async def send_daily_quote(context: CallbackContext):
    if YOUR_CHAT_ID:
        quote = await generate_response("Give me a Zen quote.")
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=quote)

async def handle_message(update: Update, context: CallbackContext):
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
        
        # Retrieve memory from the database (last 10 interactions)
        cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 10", (user_id,))
        results = cursor.fetchall()

        memory = "\n".join([result[0] for result in results[::-1]]) if results else ""
        
        prompt = f"""You are a wise Zen monk having a conversation with a student. 
        Here's the recent conversation history:

        {memory}

        Student: {user_message}
        Zen Monk: """

        response = await generate_response(prompt)

        # Store the new memory in the database
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

async def start(update: Update, context: CallbackContext):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else
    await update.message.reply_text('Greetings, seeker of wisdom. I am a Zen monk here to guide you on your path to enlightenment. How may I assist you today?')

async def toggle_daily_quote(update: Update, context: CallbackContext):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else

    if 'daily_quote_active' not in context.bot_data:
        context.bot_data['daily_quote_active'] = True
        await update.message.reply_text("You have chosen to receive daily nuggets of Zen wisdom. May they light your path.")
    else:
        del context.bot_data['daily_quote_active']
        await update.message.reply_text("You have chosen to pause the daily Zen quotes. Remember, wisdom is all around us, even in silence.")

async def get_chat_id(update: Update, context: CallbackContext):
    await update.message.reply_text(f"Your unique identifier in this realm is: {update.effective_chat.id}")

def main():
    if is_already_running():
        print("Another instance of this bot is already running. Exiting.")
        return

    # Create table if not exists
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
            connection.commit()
        except Error as e:
            print(f"Error creating table: {e}")
        finally:
            connection.close()

    token = os.getenv("BOT_TOKEN")  # Use environment variable for the Telegram bot token
    application = Application.builder().token(token).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("toggle_quote", toggle_daily_quote))
    application.add_handler(CommandHandler("get_chat_id", get_chat_id))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedule the daily quote at a specific time (e.g., 8:00 AM UTC)
    job_queue = application.job_queue
    job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    
    print("Zen Monk Bot has awakened. Press Ctrl+C to return to silence.")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()