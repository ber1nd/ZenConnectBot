import os
import asyncio
import sys
import logging
from datetime import time, timezone, datetime, timedelta
import random
from typing import Dict, Any, Tuple, List, Optional

import aiohttp
from aiohttp import web
import aiomysql
from aiomysql.cursors import DictCursor
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, PreCheckoutQueryHandler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
RATE_LIMIT = int(os.getenv('RATE_LIMIT', 5))
PAYMENT_PROVIDER_TOKEN = os.getenv('PAYMENT_PROVIDER_TOKEN')
API_KEY = os.getenv('API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DB_CONFIG = {
    'host': os.getenv('MYSQLHOST'),
    'user': os.getenv('MYSQLUSER'),
    'password': os.getenv('MYSQLPASSWORD'),
    'db': os.getenv('MYSQL_DATABASE'),
    'port': int(os.getenv('MYSQLPORT', 3306)),
}

client = AsyncOpenAI(api_key=API_KEY)

# Database connection pool
db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await aiomysql.create_pool(**DB_CONFIG, autocommit=True)
    return db_pool

async def execute_query(query: str, params: tuple = None) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(DictCursor) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

async def execute_insert(query: str, params: tuple = None) -> int:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return cur.lastrowid

async def setup_database():
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            chat_type ENUM('private', 'group') DEFAULT 'private',
            total_minutes INT DEFAULT 0,
            zen_points INT DEFAULT 0,
            daily_quote TINYINT(1) DEFAULT 0,
            level INT DEFAULT 0,
            subscription_status BOOLEAN DEFAULT FALSE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_memory (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT,
            group_id BIGINT,
            memory TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meditation_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT,
            group_id BIGINT,
            duration INT,
            zen_points INT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_memberships (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT,
            group_id BIGINT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT,
            start_date DATETIME,
            end_date DATETIME,
            status ENUM('active', 'cancelled', 'expired') DEFAULT 'active',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pvp_battles (
            id INT AUTO_INCREMENT PRIMARY KEY,
            challenger_id BIGINT,
            opponent_id BIGINT,
            group_id BIGINT,
            status ENUM('pending', 'in_progress', 'completed') DEFAULT 'pending',
            current_turn BIGINT,
            challenger_hp INT DEFAULT 100,
            opponent_hp INT DEFAULT 100,
            last_move_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (challenger_id) REFERENCES users(user_id),
            FOREIGN KEY (opponent_id) REFERENCES users(user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rate_limit (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_user_timestamp (user_id, timestamp)
        )
        """
    ]
    
    for query in queries:
        await execute_query(query)
    
    logger.info("Database setup completed successfully.")

async def generate_response(prompt: str, elaborate: bool = False) -> str:
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
        logger.error(f"Error generating response: {type(e).__name__}: {str(e)}")
        return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

def get_level_name(points: int) -> str:
    if points < 100:
        return "Beginner"
    elif points < 200:
        return "Novice"
    elif points < 300:
        return "Apprentice"
    elif points < 400:
        return "Adept"
    else:
        return "Master"

async def update_user_level(user_id: int, zen_points: int):
    new_level = min(zen_points // 100, 4)  # Cap at level 4
    try:
        await execute_query("UPDATE users SET level = %s WHERE user_id = %s", (new_level, user_id))
    except Exception as e:
        logger.error(f"Error updating user level: {e}")

async def check_subscription(user_id: int) -> bool:
    try:
        results = await execute_query("SELECT subscription_status FROM users WHERE user_id = %s", (user_id,))
        return results[0]['subscription_status'] if results else False
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return False

async def send_daily_quote(context: ContextTypes.DEFAULT_TYPE):
    try:
        users = await execute_query("SELECT user_id FROM users WHERE daily_quote = 1")
        for user in users:
            quote = await generate_response("Give me a short Zen quote.")
            await context.bot.send_message(chat_id=user['user_id'], text=quote)
    except Exception as e:
        logger.error(f"Error in send_daily_quote: {e}")

async def togglequote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    if chat_type != 'private':
        await update.message.reply_text("This command can only be used in private chats. Please message me directly to toggle your daily quote subscription.")
        return

    try:
        results = await execute_query("SELECT daily_quote FROM users WHERE user_id = %s", (user_id,))
        if not results:
            new_status = 1
            await execute_query("INSERT INTO users (user_id, daily_quote) VALUES (%s, %s)", (user_id, new_status))
        else:
            new_status = 1 if results[0]['daily_quote'] == 0 else 0
            await execute_query("UPDATE users SET daily_quote = %s WHERE user_id = %s", (new_status, user_id))
        
        if new_status == 1:
            await update.message.reply_text("You have chosen to receive daily nuggets of Zen wisdom. May they light your path.")
        else:
            await update.message.reply_text("You have chosen to pause the daily Zen quotes. Remember, wisdom is all around us, even in silence.")
    except Exception as e:
        logger.error(f"Error in togglequote: {e}")
        await update.message.reply_text("I apologize, I'm having trouble updating your preferences. Please try again later.")

async def zen_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = await generate_response("Give me a short Zen quote.")
    await update.message.reply_text(quote)

async def meditate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        duration = int(context.args[0]) if context.args else 5  # Default to 5 minutes
        if duration <= 0:
            raise ValueError("Meditation duration must be a positive number.")
    except ValueError as e:
        await update.message.reply_text(f"Invalid duration: {str(e)}. Please provide a positive number of minutes.")
        return

    user_id = update.effective_user.id
    try:
        results = await execute_query("SELECT zen_points, level, subscription_status FROM users WHERE user_id = %s", (user_id,))
        
        if not results:
            await execute_query("INSERT INTO users (user_id, zen_points, level, subscription_status) VALUES (%s, 0, 0, FALSE)", (user_id,))
            user_data = {'zen_points': 0, 'level': 0, 'subscription_status': False}
        else:
            user_data = results[0]

        if user_data['level'] == 0 and duration > 5:
            await update.message.reply_text("As a beginner, you can only meditate for up to 5 minutes at a time. Let's start with 5 minutes.")
            duration = 5

        await update.message.reply_text(f"Start meditating for {duration} minutes. Focus on your breath.")
    
        interval = 2  # Interval in minutes
        total_intervals = duration // interval
    
        for i in range(total_intervals):
            await asyncio.sleep(interval * 60)  # Wait for the interval duration
            motivational_message = await generate_response("Give me a short Zen meditation guidance message.")
            await update.message.reply_text(motivational_message)
    
        await asyncio.sleep((duration % interval) * 60)  # Sleep for the remaining time
        zen_points = duration + (5 if duration > 15 else 0)  # 1 point per minute, +5 for sessions > 15 minutes

        new_zen_points = user_data['zen_points'] + zen_points
        new_level = min(new_zen_points // 100, 4)

        if new_level > user_data['level']:
            if new_level >= 2 and not user_data['subscription_status']:
                await prompt_subscription(update, context)
                new_level = 1  # Cap at level 1 if not subscribed
            else:
                level_name = get_level_name(new_zen_points)
                await update.message.reply_text(f"Congratulations! You've reached a new level: {level_name}")

        await execute_query("""
            UPDATE users 
            SET total_minutes = total_minutes + %s, 
                zen_points = %s,
                level = %s
            WHERE user_id = %s
        """, (duration, new_zen_points, new_level, user_id))

        await update.message.reply_text(f"Your meditation session is over. You earned {zen_points} Zen points!")

    except Exception as e:
        logger.error(f"Error in meditate: {e}")
        await update.message.reply_text("I'm sorry, there was an issue logging your meditation session.")

async def prompt_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Subscribe Now", callback_data="subscribe")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "To advance beyond Level 1, you need to subscribe for $1 per month. "
        "This subscription allows you to access higher levels and unlock more features.",
        reply_markup=reply_markup
    )

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    title = "Zen Monk Bot Subscription"
    description = "Monthly subscription to access advanced levels"
    payload = "Monthly_Sub"
    currency = "USD"
    price = 100  # $1.00
    prices = [LabeledPrice("Monthly Subscription", price)]

    await context.bot.send_invoice(
        chat_id, title, description, payload,
        PAYMENT_PROVIDER_TOKEN, currency, prices
    )

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    title = "Zen Monk Bot Subscription"
    description = "Monthly subscription to access advanced levels"
    payload = "Monthly_Sub"
    currency = "USD"
    price = 100  # $1.00
    prices = [LabeledPrice("Monthly Subscription", price)]

    try:
        await context.bot.send_invoice(
            chat_id, title, description, payload,
            PAYMENT_PROVIDER_TOKEN, currency, prices
        )
    except Exception as e:
        logger.error(f"Error sending invoice: {e}")
        await update.message.reply_text("There was an error processing your subscription request. Please try again later.")

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload != "Monthly_Sub":
        await query.answer(ok=False, error_message="Something went wrong...")
    else:
        await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        # Update user's subscription status
        await execute_query("UPDATE users SET subscription_status = TRUE WHERE user_id = %s", (user_id,))
        
        # Add subscription record
        end_date = datetime.now() + timedelta(days=30)  # Subscription for 30 days
        await execute_query("""
            INSERT INTO subscriptions (user_id, start_date, end_date, status)
            VALUES (%s, NOW(), %s, 'active')
        """, (user_id, end_date))
        
        logger.info(f"Subscription activated for user {user_id}")
        await update.message.reply_text("Thank you for your subscription! You now have access to all levels for the next 30 days.")
    except Exception as e:
        logger.error(f"Error in successful_payment_callback: {e}")
        await update.message.reply_text("There was an error processing your subscription. Please contact support.")

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        results = await execute_query("SELECT subscription_status FROM users WHERE user_id = %s", (user_id,))
        
        if results and results[0]['subscription_status']:
            # Update user's subscription status
            await execute_query("UPDATE users SET subscription_status = FALSE WHERE user_id = %s", (user_id,))
            
            # Update subscription record
            await execute_query("""
                UPDATE subscriptions 
                SET status = 'cancelled', end_date = NOW()
                WHERE user_id = %s AND status = 'active'
            """, (user_id,))
            
            logger.info(f"Subscription cancelled for user {user_id}")
            await update.message.reply_text("Your subscription has been cancelled. You will have access to premium features until the end of your current billing cycle.")
        else:
            await update.message.reply_text("You don't have an active subscription to cancel.")
    except Exception as e:
        logger.error(f"Error in unsubscribe_command: {e}")
        await update.message.reply_text("There was an error processing your unsubscription request. Please try again later.")

async def subscription_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        results = await execute_query("""
            SELECT u.subscription_status, s.end_date, u.level
            FROM users u
            LEFT JOIN subscriptions s ON u.user_id = s.user_id AND s.status = 'active'
            WHERE u.user_id = %s
        """, (user_id,))
        
        if results:
            result = results[0]
            subscription_status = result['subscription_status']
            end_date = result['end_date']
            level = result['level']
            
            if subscription_status:
                status_message = f"Your subscription is active until {end_date.strftime('%Y-%m-%d')}."
            else:
                status_message = "You don't have an active subscription."
            
            level_name = get_level_name(level * 100)  # Assuming 100 points per level
            await update.message.reply_text(f"{status_message}\nYour current level: {level_name} (Level {level})")
        else:
            await update.message.reply_text("You don't have any subscription information.")
    except Exception as e:
        logger.error(f"Error in subscription_status_command: {e}")
        await update.message.reply_text("There was an error retrieving your subscription status. Please try again later.")

async def check_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    try:
        results = await execute_query("SELECT total_minutes, zen_points, level FROM users WHERE user_id = %s", (user_id,))
        if results:
            result = results[0]
            total_minutes = result['total_minutes']
            zen_points = result['zen_points']
            level = result['level']
            level_name = get_level_name(zen_points)
            message = f"Your Zen journey:\nLevel: {level_name} (Level {level})\nTotal meditation time: {total_minutes} minutes\nZen points: {zen_points}"
            if chat_type == 'private':
                mini_app_url = "https://zenconnectbot-production.up.railway.app/"
                keyboard = [[InlineKeyboardButton("Open Zen Stats", web_app=WebAppInfo(url=mini_app_url))]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(message, reply_markup=reply_markup)
            else:
                await update.message.reply_text(message)
        else:
            await update.message.reply_text("You haven't started your Zen journey yet. Try meditating to earn some points!")
    except Exception as e:
        logger.error(f"Error in check_points: {e}")
        await update.message.reply_text("I apologize, I'm having trouble accessing your stats right now. Please try again later.")

async def delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        await execute_query("DELETE FROM users WHERE user_id = %s", (user_id,))
        await execute_query("DELETE FROM user_memory WHERE user_id = %s", (user_id,))
        await execute_query("DELETE FROM meditation_log WHERE user_id = %s", (user_id,))
        await execute_query("DELETE FROM group_memberships WHERE user_id = %s", (user_id,))
        await execute_query("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
        await update.message.reply_text("Your data has been deleted. Your journey with us ends here, but remember that every ending is a new beginning.")
    except Exception as e:
        logger.error(f"Error in delete_data: {e}")
        await update.message.reply_text("I apologize, I'm having trouble deleting your data. Please try again later.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    chat_type = update.message.chat.type
    group_id = update.message.chat.id if chat_type == 'group' else None

    # Check if the message is in a group and contains 'Zen' or mentions the bot
    bot_username = context.bot.username.lower()
    if chat_type == 'group' and not (
        'zen' in user_message.lower() or 
        f'@{bot_username}' in user_message.lower()
    ):
        return  # Exit the function if it's a group message not meant for the bot

    # Apply rate limiting
    if not await check_rate_limit(user_id):
        await update.message.reply_text("Please wait a moment before sending another message. Zen teaches us the value of patience.")
        return

    try:
        # Update or insert user information
        chat_type_db = 'private' if chat_type == 'private' else 'group'
        await execute_query("""
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
            await execute_query("""
                INSERT IGNORE INTO group_memberships (user_id, group_id)
                VALUES (%s, %s)
            """, (user_id, group_id))

        # Fetch user memory
        if group_id:
            results = await execute_query("SELECT memory FROM user_memory WHERE user_id = %s AND group_id = %s ORDER BY timestamp DESC LIMIT 5", (user_id, group_id))
        else:
            results = await execute_query("SELECT memory FROM user_memory WHERE user_id = %s AND group_id IS NULL ORDER BY timestamp DESC LIMIT 5", (user_id,))
        
        memory = "\n".join([result['memory'] for result in results[::-1]]) if results else ""
        
        elaborate = any(word in user_message.lower() for word in ['why', 'how', 'explain', 'elaborate', 'tell me more'])
        
        prompt = f"""You are a wise Zen monk having a conversation with a student. 
        Here's the recent conversation history:

        {memory}

        Student: {user_message}
        Zen Monk: """

        response = await generate_response(prompt, elaborate)

        new_memory = f"Student: {user_message}\nZen Monk: {response}"
        await execute_query("INSERT INTO user_memory (user_id, group_id, memory) VALUES (%s, %s, %s)", (user_id, group_id, new_memory))

        await update.message.reply_text(response)

    except Exception as e:
        logger.error(f"Error in handle_message: {e}")
        await update.message.reply_text("I'm having trouble processing your message. Please try again later.")

async def check_rate_limit(user_id: int) -> bool:
    now = datetime.now()
    try:
        results = await execute_query(
            "SELECT COUNT(*) as count FROM rate_limit WHERE user_id = %s AND timestamp > %s",
            (user_id, now - timedelta(minutes=1))
        )
        count = results[0]['count'] if results else 0
        
        if count < RATE_LIMIT:
            await execute_query("INSERT INTO rate_limit (user_id, timestamp) VALUES (%s, %s)", (user_id, now))
            return True
        return False
    except Exception as e:
        logger.error(f"Error in check_rate_limit: {e}")
        return False

# PvP-related functions
async def challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    challenger = update.effective_user
    chat_id = update.effective_chat.id
    chat_type = update.message.chat.type

    if chat_type == 'private':
        await update.message.reply_text("You can only challenge others in group chats.")
        return

    if not context.args:
        await update.message.reply_text("Please mention the user you want to challenge.")
        return

    opponent_username = context.args[0].replace("@", "")
    
    try:
        # Get challenger's info
        challenger_info = await execute_query("SELECT user_id, zen_points FROM users WHERE user_id = %s", (challenger.id,))
        
        if not challenger_info:
            await update.message.reply_text("You need to start your Zen journey before challenging others.")
            return

        # Get opponent's info
        opponent_info = await execute_query("SELECT user_id, zen_points FROM users WHERE username = %s", (opponent_username,))
        
        if not opponent_info:
            await update.message.reply_text(f"User @{opponent_username} is not found in our records.")
            return

        # Check for existing battles
        existing_battle = await execute_query("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id IN (%s, %s) OR opponent_id IN (%s, %s))
            AND status != 'completed'
        """, (challenger.id, opponent_info[0]['user_id'], challenger.id, opponent_info[0]['user_id']))

        if existing_battle:
            if existing_battle[0]['challenger_id'] == challenger.id or existing_battle[0]['opponent_id'] == challenger.id:
                await update.message.reply_text("You are already in a battle. Complete it before starting a new one.")
            else:
                await update.message.reply_text(f"@{opponent_username} is already in a battle. Try challenging someone else.")
            return

        # Create a new battle
        battle_id = await execute_insert("""
            INSERT INTO pvp_battles (challenger_id, opponent_id, group_id, status, current_turn)
            VALUES (%s, %s, %s, 'pending', %s)
        """, (challenger.id, opponent_info[0]['user_id'], chat_id, challenger.id))

        # Create inline keyboard for opponent to accept or decline
        keyboard = [
            [InlineKeyboardButton("Accept", callback_data=f"accept_challenge:{battle_id}"),
             InlineKeyboardButton("Decline", callback_data=f"decline_challenge:{battle_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"{challenger.mention_html()} has challenged {context.args[0]} to a Zen battle! "
            f"{context.args[0]}, do you accept?",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Error in challenge: {e}")
        await update.message.reply_text("An error occurred while processing your challenge. Please try again later.")

async def handle_challenge_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    response, battle_id = query.data.split(':')
    responder_id = query.from_user.id

    try:
        # Fetch the battle information
        battle = await execute_query("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))

        if not battle:
            await query.edit_message_text("This challenge is no longer valid.")
            return

        if battle[0]['status'] != 'pending':
            await query.edit_message_text("This challenge has already been responded to.")
            return

        if responder_id != battle[0]['opponent_id']:
            await query.answer("You are not the challenged player!", show_alert=True)
            return

        if response == 'accept_challenge':
            await execute_query("""
                UPDATE pvp_battles 
                SET status = 'in_progress' 
                WHERE id = %s
            """, (battle_id,))
            
            await start_battle(context.bot, query.message.chat_id, battle[0]['challenger_id'], battle[0]['opponent_id'], battle_id)
        else:  # decline_challenge
            await execute_query("DELETE FROM pvp_battles WHERE id = %s", (battle_id,))
            
            await query.edit_message_text("The challenge has been declined.")

    except Exception as e:
        logger.error(f"Error in handle_challenge_response: {e}")
        await query.edit_message_text("An error occurred while processing your response. Please try again later.")

async def start_battle(bot, chat_id, challenger_id, opponent_id, battle_id):
    keyboard = [
        [InlineKeyboardButton("Attack", callback_data=f"battle_move:attack:{battle_id}")],
        [InlineKeyboardButton("Defend", callback_data=f"battle_move:defend:{battle_id}")],
        [InlineKeyboardButton("Special Move", callback_data=f"battle_move:special:{battle_id}")],
        [InlineKeyboardButton("Focus", callback_data=f"battle_move:focus:{battle_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    challenger = await bot.get_chat_member(chat_id, challenger_id)
    opponent = await bot.get_chat_member(chat_id, opponent_id)
    
    await bot.send_message(
        chat_id=chat_id,
        text=f"The Zen battle between {challenger.user.mention_html()} and {opponent.user.mention_html()} begins!\n"
             f"{challenger.user.mention_html()}, it's your turn. Choose your move:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def handle_battle_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    move, battle_id = query.data.split(':')[1:]
    current_player_id = query.from_user.id

    try:
        # Fetch battle information
        battle = await execute_query("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))

        if not battle:
            await query.edit_message_text("This battle is no longer active.")
            return

        battle = battle[0]  # Get the first (and only) result

        if battle['status'] != 'in_progress':
            await query.edit_message_text("This battle has already ended.")
            return

        if battle['current_turn'] != current_player_id:
            await query.answer("It's not your turn!", show_alert=True)
            return

        # Process the move
        damage, message = process_move(move, current_player_id, battle)

        # Update battle state
        new_hp = battle['opponent_hp'] - damage if current_player_id == battle['challenger_id'] else battle['challenger_hp'] - damage
        new_turn = battle['opponent_id'] if current_player_id == battle['challenger_id'] else battle['challenger_id']

        await execute_query("""
            UPDATE pvp_battles 
            SET challenger_hp = CASE WHEN challenger_id = %s THEN challenger_hp ELSE GREATEST(challenger_hp - %s, 0) END,
                opponent_hp = CASE WHEN opponent_id = %s THEN opponent_hp ELSE GREATEST(opponent_hp - %s, 0) END,
                current_turn = %s, 
                last_move_timestamp = NOW()
            WHERE id = %s
        """, (current_player_id, damage, current_player_id, damage, new_turn, battle['id']))

        # Fetch updated battle info
        updated_battle = await execute_query("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))
        updated_battle = updated_battle[0]  # Get the first (and only) result

        # Check if the battle is over
        if updated_battle['challenger_hp'] <= 0 or updated_battle['opponent_hp'] <= 0:
            await end_battle(context.bot, battle['group_id'], updated_battle)
        else:
            # Prepare for the next turn
            keyboard = [
                [InlineKeyboardButton("Attack", callback_data=f"battle_move:attack:{battle_id}")],
                [InlineKeyboardButton("Defend", callback_data=f"battle_move:defend:{battle_id}")],
                [InlineKeyboardButton("Special Move", callback_data=f"battle_move:special:{battle_id}")],
                [InlineKeyboardButton("Focus", callback_data=f"battle_move:focus:{battle_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            current_player = await context.bot.get_chat_member(battle['group_id'], current_player_id)
            next_player = await context.bot.get_chat_member(battle['group_id'], new_turn)

            await query.edit_message_text(
                text=f"{message}\n\n"
                     f"Challenger HP: {updated_battle['challenger_hp']}\n"
                     f"Opponent HP: {updated_battle['opponent_hp']}\n\n"
                     f"{next_player.user.mention_html()}, it's your turn. Choose your move:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

    except Exception as e:
        logger.error(f"Error in handle_battle_move: {e}")
        await query.edit_message_text("An error occurred while processing your move. Please try again later.")

def process_move(move: str, player_id: int, battle: Dict[str, Any]) -> Tuple[int, str]:
    base_damage = {
        'attack': (15, 25),
        'defend': (5, 10),
        'special': (20, 30),
        'focus': (0, 0)
    }

    min_damage, max_damage = base_damage[move]
    damage = random.randint(min_damage, max_damage)

    if move == 'defend':
        message = f"Player {player_id} used Defend, reducing incoming damage and counter-attacking for {damage} damage!"
    elif move == 'special':
        message = f"Player {player_id} used a Special Move, dealing {damage} damage!"
    elif move == 'focus':
        message = f"Player {player_id} used Focus, preparing for a stronger attack next turn!"
        damage = 0
    else:  # attack
        message = f"Player {player_id} attacked, dealing {damage} damage!"

    return damage, message

async def end_battle(bot, chat_id: int, battle: Dict[str, Any]):
    try:
        # Determine the winner
        if battle['challenger_hp'] <= 0:
            winner_id = battle['opponent_id']
            loser_id = battle['challenger_id']
        else:
            winner_id = battle['challenger_id']
            loser_id = battle['opponent_id']

        # Update battle status
        await execute_query("""
            UPDATE pvp_battles 
            SET status = 'completed'
            WHERE id = %s
        """, (battle['id'],))

        # Fetch user data
        user_data = await execute_query("SELECT user_id, zen_points FROM users WHERE user_id IN (%s, %s)", (winner_id, loser_id))
        user_data = {row['user_id']: row['zen_points'] for row in user_data}

        # Calculate Zen points transfer
        points_transfer = min(user_data[loser_id] // 10, 50)  # 10% of loser's points, max 50

        # Update Zen points
        await execute_query("UPDATE users SET zen_points = zen_points + %s WHERE user_id = %s", (points_transfer, winner_id))
        await execute_query("UPDATE users SET zen_points = GREATEST(zen_points - %s, 0) WHERE user_id = %s", (points_transfer, loser_id))

        winner = await bot.get_chat_member(chat_id, winner_id)
        loser = await bot.get_chat_member(chat_id, loser_id)

        await bot.send_message(
            chat_id=chat_id,
            text=f"The Zen battle has ended! {winner.user.mention_html()} is victorious and gains {points_transfer} Zen points from {loser.user.mention_html()}.",
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Error in end_battle: {e}")
        await bot.send_message(chat_id, "An error occurred while ending the battle. Please contact support.")

async def cancel_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        # Check for active battles involving the user
        battle = await execute_query("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id = %s OR opponent_id = %s)
            AND status != 'completed'
            AND group_id = %s
        """, (user_id, user_id, chat_id))
        
        if not battle:
            await update.message.reply_text("You don't have any active battles in this group.")
            return

        # Cancel the battle
        await execute_query("DELETE FROM pvp_battles WHERE id = %s", (battle[0]['id'],))
        await update.message.reply_text("Your active battle has been cancelled.")

    except Exception as e:
        logger.error(f"Error in cancel_battle: {e}")
        await update.message.reply_text("An error occurred while cancelling the battle. Please try again later.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An error occurred while processing your request. Please try again later.")

async def serve_mini_app(request):
    return web.FileResponse('./zen_stats.html')

async def get_user_stats(request):
    user_id = request.query.get('user_id')
    logger.info(f"Fetching stats for user_id: {user_id}")
    try:
        result = await execute_query("""
            SELECT total_minutes, zen_points, 
                   COALESCE(username, '') as username, 
                   COALESCE(first_name, '') as first_name, 
                   COALESCE(last_name, '') as last_name,
                   level
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        logger.info(f"Query result: {result}")
        if result:
            return web.json_response(result[0])
        else:
            logger.warning(f"User not found: {user_id}")
            return web.json_response({"error": "User not found", "user_id": user_id}, status=404)
    except Exception as e:
        logger.error(f"Database error: {e}")
        return web.json_response({"error": "Database error", "details": str(e)}, status=500)

async def main():
    # Use environment variable to determine webhook or polling
    use_webhook = os.getenv('USE_WEBHOOK', 'false').lower() == 'true'

    port = int(os.environ.get('PORT', 8080))

    # Initialize bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("togglequote", togglequote))
    application.add_handler(CommandHandler("zenquote", zen_quote))
    application.add_handler(CommandHandler("meditate", meditate))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("subscriptionstatus", subscription_status_command))
    application.add_handler(CommandHandler("deletedata", delete_data))
    application.add_handler(CommandHandler("challenge", challenge))
    application.add_handler(CommandHandler("cancelbattle", cancel_battle))
    
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (
            filters.ChatType.PRIVATE |
            (filters.ChatType.GROUPS & (
                filters.Regex(r'(?i)\bzen\b') |
                filters.Regex(r'@\w+')
            ))
        ),
        handle_message
    ))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(subscribe_callback, pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(handle_challenge_response, pattern="^(accept|decline)_challenge:"))
    application.add_handler(CallbackQueryHandler(handle_battle_move, pattern="^battle_move:"))
    
    # Pre-checkout and successful payment handlers
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    application.add_error_handler(error_handler)

    # Schedule daily quote
    if application.job_queue:
        application.job_queue.run_daily(send_daily_quote, time=time(hour=8, minute=0, tzinfo=timezone.utc))
    else:
        logger.warning("JobQueue is not available. Daily quotes will not be scheduled.")

    # Set up web app for stats
    app = web.Application()
    app.router.add_get('/', serve_mini_app)
    app.router.add_get('/api/stats', get_user_stats)

    if use_webhook:
        # Webhook settings
        webhook_url = os.getenv('WEBHOOK_URL')
        if not webhook_url:
            logger.error("Webhook URL not set. Please set the WEBHOOK_URL environment variable.")
            return

        await application.bot.set_webhook(url=webhook_url)
        
        async def webhook_handler(request):
            update = await application.update_queue.put(
                Update.de_json(await request.json(), application.bot)
            )
            return web.Response()

        app.router.add_post(f'/{TELEGRAM_TOKEN}', webhook_handler)

        # Start the web application
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()

        logger.info(f"Webhook set up on port {port}")
        
        # Keep the script running
        while True:
            await asyncio.sleep(3600)
    else:
        # Polling mode
        await application.initialize()
        await application.start()
        await application.updater.start_polling()

        # Start the web application in a separate task
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()

        logger.info("Zen Monk Bot and Web App are awakening. Press Ctrl+C to return to silence.")
        
        # Keep the script running
        while True:
            await asyncio.sleep(1)

if __name__ == '__main__':
    asyncio.run(setup_database())  # Ensure the database is set up before starting the bot
    asyncio.run(main())