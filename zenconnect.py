import socket
import openai
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from datetime import time, timezone

# Set up your OpenAI client
client = AsyncOpenAI(api_key='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')  # Replace with your actual API key

# Your personal chat ID (to be filled in later)
YOUR_CHAT_ID = 546589997  # Replace with your actual chat ID

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

    user_message = update.message.text
    prompt = f"Respond as a wise Zen monk: {user_message}"
    response = await generate_response(prompt)
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

    token = '7283636452:AAHDQqsbqNYn5sRWAn3WroKVugHeGMAkXKY'  # Replace with your Telegram bot token
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