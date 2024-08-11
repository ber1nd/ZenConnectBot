import os
import socket
import asyncio
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from datetime import time, timezone
import mysql.connector
from mysql.connector import Error

# Set up your OpenAI client using environment variables
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Your personal chat ID (use environment variable)
YOUR_CHAT_ID = int(os.getenv("CHAT_ID"))

# ... (rest of the code remains the same until the check_points function)

async def check_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("SELECT total_minutes, zen_points FROM users WHERE user_id = %s", (update.effective_chat.id,))
            result = cursor.fetchone()
            if result:
                total_minutes, zen_points = result
                level = zen_points // 100  # Example level logic
                
                progress_bar = create_progress_bar(zen_points)
                message = f"You have meditated for a total of {total_minutes} minutes and earned {zen_points} Zen points.\n{progress_bar}"
                
                mini_app_url = "https://zenconnectminiapp-production.up.railway.app"  # Replace with your Railway app URL
                message += f"\n\n[Check your progress in the ZenConnect Mini App]({mini_app_url})"
                
                if level >= 10:
                    keyboard = [
                        [InlineKeyboardButton("Unlock Higher Levels", callback_data='upgrade')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(message + "\nYou've reached a high level! Unlock more features!", reply_markup=reply_markup)
                else:
                    await update.message.reply_text(message, disable_web_page_preview=True)
            else:
                await update.message.reply_text("You have not logged any meditation sessions yet.")
        except Error as e:
            print(f"Database error: {e}")
            await update.message.reply_text("I'm sorry, there was an issue retrieving your Zen points.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()

# ... (rest of the code remains the same)

def main():
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

    token = os.getenv("BOT_TOKEN")  # Use environment variable for the Telegram bot token
    application = Application.builder().token(token).build()
    
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
    
    # Schedule the daily quote at a specific time (e.g., 8:00 AM UTC)
    if application.job_queue:
        application.job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    else:
        print("Warning: JobQueue is not available. Daily quotes will not be scheduled.")
    
    print("Zen Monk Bot has awakened. Press Ctrl+C to return to silence.")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()