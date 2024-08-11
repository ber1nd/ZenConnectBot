import os
import socket
import openai
import mysql.connector
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from datetime import time, timezone

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
    return mysql.connector.connect(
        host=os.getenv("MYSQLHOST"),
        user=os.getenv("MYSQLUSER"),
        password=os.getenv("MYSQLPASSWORD"),
        database=os.getenv("MYSQLDATABASE")
    )

async def generate_response(prompt):
    models = ["gpt-4o-mini"]  
    for model in models:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"Error with model {model}: {e}")
            if model == models[-1]:  # If this is the last model to try
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

    # Retrieve memory from the database
    db = get_db_connection()
    cursor = db.cursor()
    cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 1", (user_id,))
    result = cursor.fetchone()

    if result:
        memory = result[0]
        prompt = f"{memory}\nUser: {user_message}\nZen Monk:"
    else:
        prompt = f"User: {user_message}\nZen Monk:"

    response = await generate_response(prompt)

    # Store the new memory in the database
    new_memory = f"{prompt}\n{response}"
    cursor.execute("INSERT INTO user_memory (user_id, memory) VALUES (%s, %s)", (user_id, new_memory))
    db.commit()

    cursor.close()
    db.close()

    await update.message.reply_text(response)

async def start(update: Update, context: CallbackContext):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else
    await update.message.reply_text('Hello! I am your personal Zen monk bot. How can I assist you today?')

async def toggle_daily_quote(update: Update, context: CallbackContext):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return  # Don't respond to anyone else

    if 'daily_quote_active' not in context.bot_data:
        context.bot_data['daily_quote_active'] = True
        await update.message.reply_text("You've subscribed to daily Zen quotes!")
    else:
        del context.bot_data['daily_quote_active']
        await update.message.reply_text("You've unsubscribed from daily Zen quotes.")

async def get_chat_id(update: Update, context: CallbackContext):
    await update.message.reply_text(f"Your Chat ID is: {update.effective_chat.id}")

def main():
    if is_already_running():
        print("Another instance is already running. Exiting.")
        return

    token = os.getenv("BOT_TOKEN")  # Use environment variable for the Telegram bot token
    application = Application.builder().token(token).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("toggle_quote", toggle_daily_quote))
    application.add_handler(CommandHandler("get_chat_id", get_chat_id))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedule the daily quote at a specific time (e.g., 8:00 AM UTC)
    job_queue = application.job_queue
    job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    
    print("Bot started. Press Ctrl+C to stop.")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()