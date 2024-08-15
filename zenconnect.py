import os
import asyncio
import sys
import logging
import functools
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, PreCheckoutQueryHandler
from datetime import time, timezone, datetime, timedelta
import mysql.connector
from mysql.connector import Error
from aiohttp import web
import json
from dotenv import load_dotenv
from collections import defaultdict
import random

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

RATE_LIMIT = 5
rate_limit_dict = defaultdict(list)

# Telegram payment provider token
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")

def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQLHOST"),
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQLPORT", 3306))
        )
        return connection
    except Error as e:
        logger.error(f"Error connecting to MySQL database: {e}")
        return None

def with_database_connection(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        db = get_db_connection()
        if not db:
            logger.error(f"Failed to establish database connection in {func.__name__}")
            if isinstance(args[0], Update):
                await args[0].message.reply_text("I'm having trouble accessing my memory right now. Please try again later.")
            return
        try:
            return await func(*args, **kwargs, db=db)
        except mysql.connector.Error as e:
            logger.error(f"MySQL error in {func.__name__}: {e}")
            if isinstance(args[0], Update):
                await args[0].message.reply_text("A database error occurred. Please try again later.")
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}")
            if isinstance(args[0], Update):
                await args[0].message.reply_text("An unexpected error occurred. Please try again later.")
        finally:
            if db and db.is_connected():
                db.close()
                logger.info(f"Closed database connection in {func.__name__}")
    return wrapper

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
                    daily_quote TINYINT(1) DEFAULT 0,
                    level INT DEFAULT 0,
                    subscription_status BOOLEAN DEFAULT FALSE
                )
                """)
                
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    group_id BIGINT,
                    memory TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                
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
                
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    start_date DATETIME,
                    end_date DATETIME,
                    status ENUM('active', 'cancelled', 'expired') DEFAULT 'active',
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """)
                
                # Table for PvP battles
                cursor.execute("""
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
                """)
                
                # Check if 'winner_id' column exists, and if not, add it
                cursor.execute("SHOW COLUMNS FROM pvp_battles LIKE 'winner_id'")
                result = cursor.fetchone()
                if not result:
                    cursor.execute("ALTER TABLE pvp_battles ADD COLUMN winner_id BIGINT NULL")
                    logger.info("Added 'winner_id' column to 'pvp_battles' table.")
                
            connection.commit()
            logger.info("Database setup completed successfully.")
        except Error as e:
            logger.error(f"Error setting up database: {e}")
        finally:
            connection.close()
    else:
        logger.error("Failed to connect to the database for setup.")

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
        logger.error(f"Error generating response: {type(e).__name__}: {str(e)}")
        return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

# Define the health bar function near the top of your script
def health_bar(hp, energy):
    total_blocks = 10
    filled_blocks = int((hp / 100) * total_blocks)
    empty_blocks = total_blocks - filled_blocks
    return f"[{'█' * filled_blocks}{'░' * empty_blocks}] {hp}/100 HP | {energy}/100 Energy"

def get_level_name(points):
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

async def update_user_level(user_id, zen_points, db):
    new_level = min(zen_points // 100, 4)  # Cap at level 4
    try:
        cursor = db.cursor()
        cursor.execute("UPDATE users SET level = %s WHERE user_id = %s", (new_level, user_id))
        db.commit()
    except Error as e:
        logger.error(f"Error updating user level: {e}")
    finally:
        cursor.close()

async def check_subscription(user_id, db):
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT subscription_status FROM users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        return result['subscription_status'] if result else False
    except Error as e:
        logger.error(f"Error checking subscription: {e}")
        return False
    finally:
        cursor.close()

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
            logger.error(f"Database error in send_daily_quote: {e}")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()

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
            logger.error(f"Database error in togglequote: {e}")
            await update.message.reply_text("I apologize, I'm having trouble updating your preferences. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def zen_advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    advice = await generate_response("Provide a piece of Zen advice for daily life.")
    await update.message.reply_text(advice)

async def meditate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        duration = int(context.args[0]) if context.args else 5  # Default to 5 minutes
        if duration <= 0:
            raise ValueError("Meditation duration must be a positive number.")
    except ValueError as e:
        await update.message.reply_text(f"Invalid duration: {str(e)}. Please provide a positive number of minutes.")
        return

    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT zen_points, level, subscription_status FROM users WHERE user_id = %s", (user_id,))
            user_data = cursor.fetchone()
            
            if not user_data:
                cursor.execute("INSERT INTO users (user_id, zen_points, level, subscription_status) VALUES (%s, 0, 0, FALSE)", (user_id,))
                db.commit()
                user_data = {'zen_points': 0, 'level': 0, 'subscription_status': False}

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

            cursor.execute("""
                UPDATE users 
                SET total_minutes = total_minutes + %s, 
                    zen_points = %s,
                    level = %s
                WHERE user_id = %s
            """, (duration, new_zen_points, new_level, user_id))
            db.commit()

            await update.message.reply_text(f"Your meditation session is over. You earned {zen_points} Zen points!")

        except Error as e:
            logger.error(f"Database error in meditate: {e}")
            await update.message.reply_text("I'm sorry, there was an issue logging your meditation session.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
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
    except BadRequest as e:
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
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            
            # Update user's subscription status
            cursor.execute("UPDATE users SET subscription_status = TRUE WHERE user_id = %s", (user_id,))
            
            # Add subscription record
            end_date = datetime.now() + timedelta(days=30)  # Subscription for 30 days
            cursor.execute("""
                INSERT INTO subscriptions (user_id, start_date, end_date, status)
                VALUES (%s, NOW(), %s, 'active')
            """, (user_id, end_date))
            
            db.commit()
            logger.info(f"Subscription activated for user {user_id}")
            await update.message.reply_text("Thank you for your subscription! You now have access to all levels for the next 30 days.")
        except Error as e:
            logger.error(f"Database error in successful_payment_callback: {e}")
            await update.message.reply_text("There was an error processing your subscription. Please contact support.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        logger.error("Failed to connect to database in successful_payment_callback")
        await update.message.reply_text("There was an error processing your subscription. Please try again later.")

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            
            # Check if user is subscribed
            cursor.execute("SELECT subscription_status FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            
            if result and result[0]:
                # Update user's subscription status
                cursor.execute("UPDATE users SET subscription_status = FALSE WHERE user_id = %s", (user_id,))
                
                # Update subscription record
                cursor.execute("""
                    UPDATE subscriptions 
                    SET status = 'cancelled', end_date = NOW()
                    WHERE user_id = %s AND status = 'active'
                """, (user_id,))
                
                db.commit()
                logger.info(f"Subscription cancelled for user {user_id}")
                await update.message.reply_text("Your subscription has been cancelled. You will have access to premium features until the end of your current billing cycle.")
            else:
                await update.message.reply_text("You don't have an active subscription to cancel.")
        except Error as e:
            logger.error(f"Database error in unsubscribe_command: {e}")
            await update.message.reply_text("There was an error processing your unsubscription request. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        logger.error("Failed to connect to database in unsubscribe_command")
        await update.message.reply_text("There was an error processing your request. Please try again later.")

async def subscription_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("""
                SELECT u.subscription_status, s.end_date, u.level
                FROM users u
                LEFT JOIN subscriptions s ON u.user_id = s.user_id AND s.status = 'active'
                WHERE u.user_id = %s
            """, (user_id,))
            result = cursor.fetchone()
            
            if result:
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
        except Error as e:
            logger.error(f"Database error in subscription_status_command: {e}")
            await update.message.reply_text("There was an error retrieving your subscription status. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        logger.error("Failed to connect to database in subscription_status_command")
        await update.message.reply_text("There was an error retrieving your subscription status. Please try again later.")

async def check_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT total_minutes, zen_points, level FROM users WHERE user_id = %s",(user_id,))
            result = cursor.fetchone()
            if result:
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
        except Error as e:
            logger.error(f"Database error in check_points: {e}")
            await update.message.reply_text("I apologize, I'm having trouble accessing your stats right now. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("DELETE FROM pvp_battles WHERE challenger_id = %s OR opponent_id = %s", (user_id, user_id))
            cursor.execute("DELETE FROM group_memberships WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM meditation_log WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM user_memory WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            db.commit()
            await update.message.reply_text("Your data has been deleted. Your journey with us ends here, but remember that every ending is a new beginning.")
        except Error as e:
            logger.error(f"Database error in delete_data: {e}")
            await update.message.reply_text("I apologize, I'm having trouble deleting your data. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

# PvP Functionality
from datetime import datetime, timezone

@with_database_connection
async def start_pvp(update: Update, context: ContextTypes.DEFAULT_TYPE, db):
    user_id = update.effective_user.id
    opponent_username = context.args[0].replace('@', '') if context.args else None

    if not opponent_username:
        await update.message.reply_text("Please specify a valid opponent or type 'bot' to challenge the bot.")
        return

    if not db:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")
        return

    opponent_id = None
    if opponent_username == 'bot':
        opponent_id = 7283636452  # Bot's ID
    else:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT user_id FROM users WHERE username = %s", (opponent_username,))
            result = cursor.fetchone()
            if result:
                opponent_id = result['user_id']
            else:
                await update.message.reply_text(f"Could not find user with username @{opponent_username}. Please make sure they have interacted with the bot.")
                return
        finally:
            cursor.close()

    try:
        cursor = db.cursor(dictionary=True)

        # Ensure the challenger exists in the users table
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        challenger = cursor.fetchone()

        if not challenger:
            await update.message.reply_text("You are not registered in the system. Please interact with the bot first.")
            return

        # Check if there's an ongoing PvP battle between these two users
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE ((challenger_id = %s AND opponent_id = %s) 
            OR (challenger_id = %s AND opponent_id = %s)) 
            AND status = 'in_progress'
        """, (user_id, opponent_id, opponent_id, user_id))
        battle = cursor.fetchone()

        if battle:
            await update.message.reply_text("There's already an ongoing battle between you and this opponent.")
            return

        # Create a new PvP battle
        cursor.execute("""
            INSERT INTO pvp_battles (challenger_id, opponent_id, group_id, current_turn, status)
            VALUES (%s, %s, %s, %s, 'pending')
        """, (user_id, opponent_id, update.effective_chat.id, user_id))
        db.commit()

        if opponent_id == 7283636452:
            await update.message.reply_text("You have challenged the bot! The battle will begin now.")
            await accept_pvp(update, context)  # Auto-accept the challenge if the opponent is the bot
        else:
            await update.message.reply_text(f"Challenge sent to @{opponent_username}! They need to accept the challenge by using /acceptpvp.")

        # Automatically send move buttons if the battle is against the bot
        if opponent_id == 7283636452:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Choose your move:", reply_markup=generate_pvp_move_buttons(user_id))

    except Error as e:
        logger.error(f"Database error in start_pvp: {e}")
        await update.message.reply_text("An error occurred while starting the PvP battle. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()
  

def fetch_pending_battle(db, user_id):
    with db.cursor(dictionary=True) as cursor:
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (opponent_id = %s AND status = 'pending')
            OR (challenger_id = %s AND opponent_id = 7283636452 AND status = 'pending')
        """, (user_id, user_id))
        battle = cursor.fetchone()
        cursor.fetchall()  # Consume any remaining results
    return battle

def update_battle_status(db, battle_id, challenger_id):
    with db.cursor() as cursor:
        cursor.execute("""
            UPDATE pvp_battles 
            SET status = 'in_progress', current_turn = %s 
            WHERE id = %s
        """, (challenger_id, battle_id))
        db.commit()

@with_database_connection
async def accept_pvp(update: Update, context: ContextTypes.DEFAULT_TYPE, db):
    user_id = update.effective_user.id
    logger.info(f"Accepting PvP challenge for user: {user_id}")
    
    try:
        battle = fetch_pending_battle(db, user_id)
        
        if not battle:
            logger.info(f"No pending battles found for user: {user_id}")
            await update.message.reply_text("You have no pending PvP challenges.")
            return
        
        logger.info(f"Found pending battle: {battle}")
        
        update_battle_status(db, battle['id'], battle['challenger_id'])
        logger.info(f"Updated battle status to in_progress for battle ID: {battle['id']}")

        await update.message.reply_text("You have accepted the challenge! The battle begins now.")
        
        # Notify the challenger
        await context.bot.send_message(chat_id=battle['challenger_id'], text="Your challenge has been accepted! The battle begins now.")
        
        # Send move buttons to the challenger
        await context.bot.send_message(
            chat_id=battle['challenger_id'],
            text="It's your turn! Choose your move:",
            reply_markup=generate_pvp_move_buttons(battle['challenger_id'])
        )

    except mysql.connector.Error as e:
        logger.error(f"MySQL error in accept_pvp: {e}")
        await update.message.reply_text("An error occurred while accepting the PvP challenge. Please try again later.")
        if db.is_connected():
            db.rollback()
    except Exception as e:
        logger.error(f"Unexpected error in accept_pvp: {e}", exc_info=True)
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        if db.is_connected():
            db.rollback()

def setup_pvp_commands(application):
    application.add_handler(CommandHandler("pvpmove", execute_pvp_move))

import random
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def generate_pvp_move_buttons(user_id):
    keyboard = [
        [InlineKeyboardButton("Strike", callback_data=f"pvp_strike_{user_id}")],
        [InlineKeyboardButton("Defend", callback_data=f"pvp_defend_{user_id}")],
        [InlineKeyboardButton("Focus", callback_data=f"pvp_focus_{user_id}")],
        [InlineKeyboardButton("Zen Strike", callback_data=f"pvp_zenstrike_{user_id}")],
        [InlineKeyboardButton("Mind Trap", callback_data=f"pvp_mindtrap_{user_id}")],
        [InlineKeyboardButton("Meditate", callback_data=f"pvp_meditate_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

import random
import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

async def bot_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            # Fetch the active battle where the bot is involved
            cursor.execute("""
                SELECT * FROM pvp_battles 
                WHERE (challenger_id = 7283636452 OR opponent_id = 7283636452) AND status = 'in_progress'
            """)
            battle = cursor.fetchone()

            if not battle:
                logger.info("No active battle found for the bot.")
                return

            if battle['current_turn'] != 7283636452:
                logger.info("It's not the bot's turn.")
                return

            # Determine bot's HP and opponent's HP
            bot_hp = battle['challenger_hp'] if battle['challenger_id'] == 7283636452 else battle['opponent_hp']
            opponent_hp = battle['opponent_hp'] if battle['challenger_id'] == 7283636452 else battle['challenger_hp']

            # Retrieve bot's energy level
            bot_energy = context.user_data.get('energy', 100)

            # Generate AI decision-making prompt
            prompt = f"""
            You are a Zen warrior AI engaged in a strategic duel. Your goal is to win decisively by reducing your opponent's HP to 0 while keeping your HP above 0. 
            
            Current situation:
            - Your HP: {bot_hp}/100
            - Opponent's HP: {opponent_hp}/100
            - Your Energy: {bot_energy}/100

            Available actions:
            - Strike: Deal moderate damage to the opponent. Costs 12 energy.
            - Defend: Heal yourself and gain energy. Costs 0 energy, gains 10 energy.
            - Focus: Recover energy and increase your critical hit chances for the next turn. Gains 20-30 energy.
            - Zen Strike: A powerful move that deals significant damage. Costs 40 energy.
            - Mind Trap: Reduces the effectiveness of the opponent's next move by 50%. Costs 20 energy.
            - Meditate: Heal yourself moderately and restore energy. Gains 15-25 energy.

            Strategy to win:
            - Manage your energy carefully; don't allow it to drop too low unless you can deliver a finishing blow.
            - Use "Zen Strike" only if you have sufficient energy and can guarantee significant damage or a win.
            - If your energy is low, prioritize using "Defend," "Focus," or "Meditate" to regain energy and prolong the fight.
            - Strike with "Mind Trap" if the opponent has high energy, to reduce their effectiveness in the next turn.
            """

            # Generate AI response based on the prompt
            ai_response = await generate_response(prompt)

            # Default action to strike unless energy constraints or AI logic dictate otherwise
            action = "strike"

            # Avoid getting stuck without energy by selecting appropriate moves
            if bot_energy < 12:  # Not enough energy for Strike
                if bot_energy >= 20:
                    action = "focus"
                elif bot_energy >= 15:
                    action = "meditate"
                else:
                    action = "defend"
            else:
                # Extract action from AI response, if provided
                for move in ["strike", "defend", "focus", "zenstrike", "mindtrap", "meditate"]:
                    if move in ai_response.lower():
                        action = move
                        break

            logger.info(f"Bot chose action: {action} based on AI response: {ai_response}")

            # Execute the chosen move
            await execute_pvp_move(update, context, db, bot_mode=True, action=action)

            # Send AI's explanation to the chat for transparency
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Zen Bot's strategy: {ai_response}")

        except Exception as e:
            logger.error(f"Error during bot move execution: {e}")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()

async def execute_pvp_move_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db_connection()
    if db:
        try:
            await execute_pvp_move(update, context, db=db)
        finally:
            if db.is_connected():
                db.close()

async def execute_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE, db, bot_mode=False, action=None):
    user_id = 7283636452 if bot_mode else update.effective_user.id
    energy_cost = 0  # Initialize energy cost
    energy_gain = 0  # Initialize energy gain
    
    valid_moves = ["strike", "defend", "focus", "zenstrike", "mindtrap", "meditate"]

    if not bot_mode:
        if update.callback_query:
            query = update.callback_query
            data = query.data.split('_')
            action = data[1]
            user_id_from_callback = int(data[-1])

            # Ensure the action is for the correct player
            if user_id_from_callback != user_id:
                await query.answer("It's not your turn!")
                return
        else:
            await update.message.reply_text("Invalid move. Please use the provided buttons.")
            return

    logger.info(f"Received action: {action}")

    if not action or action not in valid_moves:
        logger.error(f"Invalid action received: {action}")
        if not bot_mode:
            await update.callback_query.answer("Invalid move!")
        return

    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id = %s OR opponent_id = %s) AND status = 'in_progress'
        """, (user_id, user_id))
        battle = cursor.fetchone()
        
        if not battle:
            if update.message:
                await update.message.reply_text("You are not in an active battle.")
            return
        
        if battle['current_turn'] != user_id:
            if update.message:
                await update.message.reply_text("It's not your turn.")
            return

        # Get user and opponent info
        if battle['challenger_id'] == user_id:
            opponent_id = battle['opponent_id']
            user_hp, opponent_hp = battle['challenger_hp'], battle['opponent_hp']
        else:
            opponent_id = battle['challenger_id']
            user_hp, opponent_hp = battle['opponent_hp'], battle['challenger_hp']

        # Initialize energy if not set
        if 'energy' not in context.user_data:
            context.user_data['energy'] = 100

        player_energy = context.user_data.get('energy', 100)
        bot_energy = context.user_data.get('bot_energy', 100)

        # Action logic for each move
        if action == "strike":
            energy_cost = 12
            if context.user_data['energy'] < energy_cost:
                await context.bot.send_message(chat_id=battle['group_id'], text="Not enough energy to use Strike.")
                return

            damage = random.randint(12, 18)
            critical_hit = random.random() < 0.15  # 15% chance of critical hit
            if context.user_data.get('focus_critical', 0) > 0:
                critical_hit = True
                context.user_data['focus_critical'] = 0  # Reset critical boost after use

            if critical_hit:
                damage *= 2
            opponent_hp -= damage
            result_message = f"You used Strike and dealt {damage} damage{' (Critical Hit!)' if critical_hit else ''}."

        elif action == "defend":
            energy_gain = 10
            heal = random.randint(10, 20)
            user_hp += heal
            result_message = f"You used Defend, healed {heal} HP, and gained 10 energy."

        elif action == "focus":
            energy_gain = random.randint(20, 30)
            context.user_data['focus_critical'] = 0.25  # Next move has 25% more critical chance
            result_message = f"You used Focus, gained {energy_gain} energy, and increased your critical hit chance by 25% for the next move."

        elif action == "zenstrike":
            energy_cost = 40
            if context.user_data['energy'] < energy_cost:
                await context.bot.send_message(chat_id=battle['group_id'], text="Not enough energy to use Zen Strike.")
                return

            damage = random.randint(25, 35)
            critical_hit = random.random() < 0.2  # 20% chance of critical hit
            if context.user_data.get('focus_critical', 0) > 0:
                critical_hit = True
                context.user_data['focus_critical'] = 0  # Reset critical boost after use

            if critical_hit:
                damage *= 2
            opponent_hp -= damage
            result_message = f"You used Zen Strike and dealt {damage} damage{' (Critical Hit!)' if critical_hit else ''}."

        elif action == "mindtrap":
            energy_cost = 20
            if context.user_data['energy'] < energy_cost:
                await context.bot.send_message(chat_id=battle['group_id'], text="Not enough energy to use Mind Trap.")
                return

            context.user_data['mind_trap'] = True  # Opponent's next move is 50% effective
            result_message = "You used Mind Trap, the opponent's next move will be 50% effective."

        elif action == "meditate":
            energy_gain = random.randint(15, 25)
            heal = random.randint(5, 15)
            user_hp += heal
            result_message = f"You used Meditate, healed {heal} HP, and restored {energy_gain} energy."

        # Apply energy changes
        context.user_data['energy'] = max(0, min(100, context.user_data['energy'] - energy_cost + energy_gain))

        # Check if the battle ends
        if opponent_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (user_id, battle['id']))
            db.commit()
            await context.bot.send_message(chat_id=battle['group_id'], text=f"You have won the battle! Your opponent is defeated.")
            return
        elif user_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (opponent_id, battle['id']))
            db.commit()
            await context.bot.send_message(chat_id=battle['group_id'], text=f"You have been defeated.")
            return

        # Update the battle status
        cursor.execute("""
            UPDATE pvp_battles 
            SET challenger_hp = %s, opponent_hp = %s, current_turn = %s 
            WHERE id = %s
        """, (user_hp if user_id == battle['challenger_id'] else opponent_hp,
              opponent_hp if user_id == battle['challenger_id'] else user_hp,
              opponent_id,
              battle['id']))
        db.commit()

                # Visual health bar (10 blocks total) and energy display
        def health_bar(hp, energy):
            total_blocks = 10
            filled_blocks = int((hp / 100) * total_blocks)
            empty_blocks = total_blocks - filled_blocks
            return f"[{'█' * filled_blocks}{'░' * empty_blocks}] {hp}/100 HP | {energy}/100 Energy"

        # Ensure the player's health and energy are always on the left, and the bot's on the right
        player_health = user_hp if user_id != 7283636452 else opponent_hp
        player_energy = context.user_data.get('energy', 100) if user_id != 7283636452 else context.user_data.get('opponent_energy', 100)
        bot_health = opponent_hp if user_id != 7283636452 else user_hp
        bot_energy = context.user_data.get('opponent_energy', 100) if user_id != 7283636452 else context.user_data.get('energy', 100)

        try:
            # Display the health and energy bars for both players
            await context.bot.send_message(
                chat_id=battle['group_id'], 
                text=f"{result_message}\n\n{health_bar(player_health, player_energy)} vs {health_bar(bot_health, bot_energy)}"
            )  

            # Send move buttons for the next turn if the opponent is not the bot
            if opponent_id != 7283636452:
                await context.bot.send_message(chat_id=opponent_id, text="Your turn! Choose your move:", reply_markup=generate_pvp_move_buttons(opponent_id))
            else:
                await bot_pvp_move(update, context)

        except Exception as e:
            logger.error(f"Error in execute_pvp_move: {e}")
            if not bot_mode and update.callback_query:
                await update.callback_query.answer("An error occurred while executing the PvP move. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()

        if not bot_mode and update.callback_query:
            await update.callback_query.answer()

    except Exception as e:
        logger.error(f"Error in execute_pvp_move: {e}")
        if not bot_mode and update.callback_query:
            await update.callback_query.answer("An error occurred while executing the PvP move. Please try again later.")

async def surrender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, challenger_id, opponent_id FROM pvp_battles 
                WHERE (challenger_id = %s OR opponent_id = %s) AND status = 'in_progress'
            """, (user_id, user_id))
            battle_data = cursor.fetchone()
            if not battle_data:
                await update.message.reply_text("No active battles found to surrender.")
                return

            winner_id = battle_data['opponent_id'] if user_id == battle_data['challenger_id'] else battle_data['challenger_id']
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (winner_id, battle_data['id']))
            db.commit()
            await update.message.reply_text("You have surrendered the battle. Your opponent is victorious.")
        except Error as e:
            logger.error(f"Database error in surrender: {e}")
            await update.message.reply_text("An error occurred while surrendering. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

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
        logger.error(f"Database error in handle_message: {e}")
        await update.message.reply_text("I'm having trouble processing your message. Please try again later.")

    finally:
        if db.is_connected():
            cursor.close()
            db.close()

def check_rate_limit(user_id):
    now = datetime.now()
    user_messages = rate_limit_dict[user_id]
    user_messages = [time for time in user_messages if now - time < timedelta(minutes=1)]
    rate_limit_dict[user_id] = user_messages
    return len(user_messages) < RATE_LIMIT

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An error occurred while processing your request. Please try again later.")

async def serve_mini_app(request):
    return web.FileResponse('./zen_stats.html')

async def get_user_stats(request):
    user_id = request.query.get('user_id')
    logger.info(f"Fetching stats for user_id: {user_id}")
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            query = """
                SELECT total_minutes, zen_points, 
                       COALESCE(username, '') as username, 
                       COALESCE(first_name, '') as first_name, 
                       COALESCE(last_name, '') as last_name,
                       level
                FROM users
                WHERE user_id = %s
            """
            logger.info(f"Executing query: {query}")
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            logger.info(f"Query result: {result}")
            if result:
                return web.json_response(result)
            else:
                logger.warning(f"User not found: {user_id}")
                return web.json_response({"error": "User not found", "user_id": user_id}, status=404)
        except Error as e:
            logger.error(f"Database error: {e}")
            return web.json_response({"error": "Database error", "details": str(e)}, status=500)
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        logger.error("Failed to connect to database")
        return web.json_response({"error": "Database connection failed"}, status=500)

async def getbotid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_user = await context.bot.get_me()
    bot_id = bot_user.id
    await update.message.reply_text(f"My user ID is: {bot_id}")

def setup_handlers(application):
    application.add_handler(CommandHandler("togglequote", togglequote))
    application.add_handler(CommandHandler("zenadvice", zen_advice))
    application.add_handler(CommandHandler("meditate", meditate))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("subscriptionstatus", subscription_status_command))
    application.add_handler(CommandHandler("deletedata", delete_data))
    application.add_handler(CommandHandler("startpvp", start_pvp))
    application.add_handler(CommandHandler("acceptpvp", accept_pvp))
    application.add_handler(CommandHandler("surrender", surrender))
    application.add_handler(CommandHandler("getbotid", getbotid))
    
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
    application.add_handler(CallbackQueryHandler(execute_pvp_move_wrapper, pattern="^pvp_"))
    
    # Pre-checkout and successful payment handlers
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    application.add_error_handler(error_handler)

async def main():
    # Use environment variable to determine webhook or polling
    use_webhook = os.getenv('USE_WEBHOOK', 'false').lower() == 'true'

    token = os.getenv("TELEGRAM_TOKEN")
    port = int(os.environ.get('PORT', 8080))

    # Initialize bot
    application = Application.builder().token(token).build()

    # Set up handlers
    setup_handlers(application)

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

        app.router.add_post(f'/{token}', webhook_handler)

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
    setup_database()  # Ensure the database is set up before starting the bot
    asyncio.run(main())