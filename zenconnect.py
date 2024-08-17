import os
import asyncio
import sys
import logging
import functools
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, PreCheckoutQueryHandler
from datetime import time, timezone, datetime, timedelta
import mysql.connector
from mysql.connector import Error
from aiohttp import web
from telegram.error import BadRequest
from telegram import WebAppInfo
import re
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
                await args[0].message.reply_text("I'm having trouble accessing my memory right now. Please try again later")
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
                {"role": "system", "content": "You are a wise Zen warrior. Respond to battle strategies and PvP moves with concise, insightful decisions."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating response: {type(e).__name__}: {str(e)}")
        return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

# Utility Functions

async def send_message(update, text):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)

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
    title = "Zen Warrior PvP Subscription"
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
    title = "Zen Warrior PvP Subscription"
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
@with_database_connection
async def start_pvp(update: Update, context: ContextTypes.DEFAULT_TYPE, db):
    user_id = update.effective_user.id
    opponent_username = context.args[0].replace('@', '') if context.args else None

    if not opponent_username:
        await update.message.reply_text("Please specify a valid opponent or type 'bot' to challenge the bot.")
        return

    try:
        cursor = db.cursor(dictionary=True)

        opponent_id = None
        if opponent_username == 'bot':
            opponent_id = 7283636452  # Bot's ID
        else:
            cursor.execute("SELECT user_id FROM users WHERE username = %s", (opponent_username,))
            result = cursor.fetchone()
            cursor.fetchall()  # Consume any remaining results
            if result:
                opponent_id = result['user_id']
            else:
                await update.message.reply_text(f"Could not find user with username @{opponent_username}. Please make sure they have interacted with the bot.")
                return

        # Ensure the challenger exists in the users table
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        challenger = cursor.fetchone()
        cursor.fetchall()  # Consume any remaining results

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
        cursor.fetchall()  # Consume any remaining results

        if battle:
            await update.message.reply_text("There's already an ongoing battle between you and this opponent.")
            return

        # Initialize energy for both players
        context.user_data['challenger_energy'] = 50
        context.user_data['opponent_energy'] = 50

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

    except mysql.connector.Error as e:
        logger.error(f"MySQL error in start_pvp: {e}")
        await update.message.reply_text("An error occurred while starting the PvP battle. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in start_pvp: {e}", exc_info=True)
        await update.message.reply_text("An unexpected error occurred. Please try again later.")


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
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (opponent_id = %s AND status = 'pending')
            OR (challenger_id = %s AND opponent_id = 7283636452 AND status = 'pending')
        """, (user_id, user_id))
        battle = cursor.fetchone()
        
        # Consume any remaining results
        cursor.fetchall()
        
        if not battle:
            logger.info(f"No pending battles found for user: {user_id}")
            await update.message.reply_text("You have no pending PvP challenges.")
            return
        
        logger.info(f"Found pending battle: {battle}")
        
        cursor.execute("""
            UPDATE pvp_battles 
            SET status = 'in_progress', current_turn = %s 
            WHERE id = %s
        """, (battle['challenger_id'], battle['id']))
        db.commit()
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

def generate_pvp_move_buttons(user_id):
    keyboard = [
        [InlineKeyboardButton("Strike", callback_data=f"pvp_strike_{user_id}")],
        [InlineKeyboardButton("Defend", callback_data=f"pvp_defend_{user_id}")],
        [InlineKeyboardButton("Focus", callback_data=f"pvp_focus_{user_id}")],
        [InlineKeyboardButton("Zen Strike", callback_data=f"pvp_zenstrike_{user_id}")],
        [InlineKeyboardButton("Mind Trap", callback_data=f"pvp_mindtrap_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def escape_markdown(text):
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def create_battle_view(challenger_name, challenger_hp, challenger_energy, opponent_name, opponent_hp, opponent_energy):
    max_name_length = max(len(challenger_name), len(opponent_name))
    challenger_name = challenger_name.ljust(max_name_length)
    opponent_name = opponent_name.ljust(max_name_length)
    
    hp_bar_length = 20
    energy_bar_length = 10
    
    challenger_hp_bar = '█' * int(challenger_hp / 5) + '░' * (hp_bar_length - int(challenger_hp / 5))
    opponent_hp_bar = '█' * int(opponent_hp / 5) + '░' * (hp_bar_length - int(opponent_hp / 5))
    
    challenger_energy_bar = '█' * int(challenger_energy / 10) + '░' * (energy_bar_length - int(challenger_energy / 10))
    opponent_energy_bar = '█' * int(opponent_energy / 10) + '░' * (energy_bar_length - int(opponent_energy / 10))
    
    battle_view = f"""
┌{'─' * (max_name_length + 24)}┐
│ {challenger_name} │ {int(challenger_hp):3d}/100 HP    │
│ {challenger_hp_bar} │
│ {challenger_energy_bar} {int(challenger_energy):3d}/100 Energy │
├{'─' * (max_name_length + 24)}┤
│ {opponent_name} │ {int(opponent_hp):3d}/100 HP    │
│ {opponent_hp_bar} │
│ {opponent_energy_bar} {int(opponent_energy):3d}/100 Energy │
└{'─' * (max_name_length + 24)}┘
"""
    return battle_view

async def execute_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE, db, bot_mode=False, action=None):
    user_id = 7283636452 if bot_mode else update.effective_user.id
    energy_cost = 0
    energy_gain = 0
    synergy_effects = {}

    valid_moves = ["strike", "defend", "focus", "zenstrike", "mindtrap"]

    if not bot_mode:
        if update.callback_query:
            query = update.callback_query
            data = query.data.split('_')
            action = data[1]
            user_id_from_callback = int(data[-1])

            if user_id_from_callback != user_id:
                await query.answer("It's not your turn!")
                return
        else:
            await update.message.reply_text("Invalid move. Please use the provided buttons")
            return

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
            if not bot_mode:
                await send_message(update, "You are not in an active battle.")
            return

        if battle['current_turn'] != user_id:
            if not bot_mode:
                await send_message(update, "It's not your turn.")
            return

        # Determine if the user is the challenger or opponent
        is_challenger = battle['challenger_id'] == user_id

        if is_challenger:
            user_hp, opponent_hp = battle['challenger_hp'], battle['opponent_hp']
            user_energy = context.user_data.get('challenger_energy', 50)
            opponent_energy = context.user_data.get('opponent_energy', 50)
        else:
            user_hp, opponent_hp = battle['opponent_hp'], battle['challenger_hp']
            user_energy = context.user_data.get('opponent_energy', 50)
            opponent_energy = context.user_data.get('challenger_energy', 50)

        opponent_id = battle['opponent_id'] if is_challenger else battle['challenger_id']

        # Reset energy gain/loss conditions
        context.user_data['energy_gain'] = 0
        context.user_data['energy_loss'] = 0

        # Track the previous move for synergy
        previous_move = context.user_data.get('previous_move')

        # Action logic with synergy effects
        if action == "strike":
            energy_cost = 12
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Strike.")
                return
            damage = random.randint(12, 18)

            if previous_move == "focus":
                damage = round(damage * 1.1)
                synergy_effects['critical_hit_chance'] = 0.20
            if previous_move == "zenstrike":
                damage = round(damage * 1.1)
                synergy_effects['critical_hit_chance'] = 0.15
            if previous_move == "mindtrap":
                damage = round(damage * 0.85)
                context.user_data['energy_loss'] = 10

            critical_hit = random.random() < synergy_effects.get('critical_hit_chance', 0.15)
            if critical_hit:
                damage *= 2

            opponent_hp = max(0, opponent_hp - damage)
            result_message = f"{'Bot' if bot_mode else 'You'} used Strike and dealt {damage} damage{' (Critical Hit!)' if critical_hit else ''}."

        elif action == "defend":
            energy_gain = 10
            heal = random.randint(15, 25)

            if previous_move == "zenstrike":
                heal += 10
                context.user_data['energy_loss'] = 10

            if previous_move == "focus":
                heal = round(heal * 1.15)
                synergy_effects['next_move_boost'] = True

            user_hp = min(100, user_hp + heal)
            result_message = f"{'Bot' if bot_mode else 'You'} used Defend, healed {heal} HP, and gained 10 energy."

        elif action == "focus":
            energy_gain = random.randint(20, 30)

            if previous_move == "strike":
                energy_gain += 10
                synergy_effects['critical_hit_chance'] = 0.30
            if previous_move == "zenstrike":
                energy_gain is max(50, energy_gain + 20)
                synergy_effects['next_move_penalty'] = True

            if previous_move == "mindtrap":
                context.user_data['energy_loss'] = 15

            context.user_data['focus_active'] = True
            result_message = f"{'Bot' if bot_mode else 'You'} used Focus, gained {energy_gain} energy, and increased your critical hit chance for the next move."

        elif action == "zenstrike":
            energy_cost = 40
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Zen Strike.")
                return
            damage = random.randint(25, 35)
            if previous_move == "focus":
                damage = round(damage * 1.2)
                critical_hit_chance = 0.30
            else:
                critical_hit_chance = 0.20

            critical_hit = random.random() < critical_hit_chance
            if critical_hit:
                damage *= 2

            if context.user_data.get('opponent_mind_trap'):
                damage //= 2
                context.user_data['energy_loss'] = 15

            opponent_hp = max(0, opponent_hp - damage)
            result_message = f"{'Bot' if bot_mode else 'You'} used Zen Strike and dealt {damage} damage{' (Critical Hit!)' if critical_hit else ''}."

        elif action == "mindtrap":
            energy_cost = 20
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Mind Trap.")
                return
            context.user_data['opponent_mind_trap'] = True

            if previous_move == "strike":
                opponent_hp = max(0, opponent_hp - 5)
                result_message = f"{'Bot' if bot_mode else 'You'} used Mind Trap. The opponent's next move will be 50% effective and they'll lose energy if they attack."

            elif previous_move == "defend":
                reflect_damage = random.randint(5, 10)
                opponent_hp = max(0, opponent_hp - reflect_damage)
                result_message = f"{'Bot' if bot_mode else 'You'} used Mind Trap. The opponent's next move will be reflected by {reflect_damage} damage."

            elif previous_move == "focus":
                context.user_data['energy_loss'] = 15
                result_message = f"{'Bot' if bot_mode else 'You'} used Mind Trap. The opponent's next move will be weakened, and they will lose additional energy if they attempt to recover."

            else:
                result_message = f"{'Bot' if bot_mode else 'You'} used Mind Trap. The opponent's next move will be 50% effective."

        # Apply energy changes
        user_energy = max(0, min(100, user_energy - energy_cost + energy_gain))

        # Apply energy loss from previous turn's effects
        user_energy = max(0, user_energy - context.user_data.get('energy_loss', 0))

        # Reset focus and mind trap effects after applying them
        context.user_data['focus_active'] = False
        context.user_data['opponent_mind_trap'] = False

        # Save the current move as previous_move for next turn's synergy
        context.user_data['previous_move'] = action

        # Store updated energy values
        if is_challenger:
            context.user_data['challenger_energy'] = user_energy
            context.user_data['opponent_energy'] = opponent_energy
        else:
            context.user_data['opponent_energy'] = user_energy
            context.user_data['challenger_energy'] = opponent_energy

        # Check if the battle ends
        if opponent_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (user_id, battle['id']))
            db.commit()
            await send_message(update, f"{'Bot' if bot_mode else 'You'} have won the battle! Your opponent is defeated.")
            await context.bot.send_message(chat_id=battle['group_id'], text=f"{'Bot' if bot_mode else update.effective_user.username} has won the battle!")
            return
        elif user_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (opponent_id, battle['id']))
            db.commit()
            await send_message(update, f"{'Bot' if bot_mode else 'You'} have been defeated.")
            await context.bot.send_message(chat_id=battle['group_id'], text=f"{'Bot' if bot_mode else update.effective_user.username} has been defeated.")
            return

        # Update the battle status
        if is_challenger:
            cursor.execute("""
                UPDATE pvp_battles 
                SET challenger_hp = %s, opponent_hp = %s, current_turn = %s 
                WHERE id = %s
            """, (user_hp, opponent_hp, opponent_id, battle['id']))
        else:
            cursor.execute("""
                UPDATE pvp_battles 
                SET challenger_hp = %s, opponent_hp = %s, current_turn = %s 
                WHERE id = %s
            """, (opponent_hp, user_hp, opponent_id, battle['id']))
        db.commit()

        # Create the battle view
        if bot_mode:
            player_name = "Bot"
            opponent_name = update.effective_user.first_name
        elif is_challenger:
            player_name = update.effective_user.first_name
            opponent_name = "Bot" if opponent_id == 7283636452 else "Opponent"
        else:
            player_name = "Bot" if user_id == 7283636452 else "Opponent"
            opponent_name = update.effective_user.first_name

        battle_view = create_battle_view(
            player_name,
            user_hp,
            user_energy,
            opponent_name,
            opponent_hp,
            opponent_energy
        )

        # Send the result with updated HP and energy bars
        try:
            await context.bot.send_message(
                chat_id=battle['group_id'], 
                text=f"{escape_markdown(result_message)}\n\n{battle_view}",
                parse_mode='MarkdownV2'
            )
        except BadRequest as e:
            logger.error(f"Error sending battle update: {e}")
            await context.bot.send_message(
                chat_id=battle['group_id'],
                text="An error occurred while updating the battle. Please check /pvpstatus for the current state."
            )

        # Notify players in the group chat
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
            bot_energy = context.user_data.get('challenger_energy' if battle['challenger_id'] == 7283636452 else 'opponent_energy', 50)

            # Generate AI decision-making prompt
            prompt = f"""
            You are a Zen warrior AI engaged in a strategic duel. Your goal is to win decisively by reducing your opponent's HP to 0 while keeping your HP above 0.

            Current situation:
            - Your HP: {bot_hp}/100
            - Opponent's HP: {opponent_hp}/100
            - Your Energy: {bot_energy}/100
            - Last Move: {context.user_data.get('previous_move', 'None')}

            Available actions:
            - Strike: Deal moderate damage to the opponent. Costs 12 energy.
            - Defend: Heal yourself and gain energy. Costs 0 energy, gains 10 energy.
            - Focus: Recover energy and increase your critical hit chances for the next turn. Gains 20-30 energy.
            - Zen Strike: A powerful move that deals significant damage. Costs 40 energy.
            - Mind Trap: Reduces the effectiveness of the opponent's next move by 50%. Costs 20 energy.

            Strategy to win:
            - Manage your energy carefully; don't allow it to drop too low unless you can deliver a finishing blow.
            - If you used "Focus" in the previous move, consider following up with "Strike" or "Zen Strike" for enhanced damage.
            - Use "Zen Strike" if you have enough energy, especially if "Focus" was used previously for a critical hit.
            - Use "Mind Trap" to weaken the opponent, particularly if they have high energy or if you want to set up a safer "Zen Strike."
            - Use "Defend" to recover HP and energy, especially if your HP is low or if you need to prepare for a powerful move.
            - If your energy is too low, prioritize using "Focus" or "Defend" to recover before attempting to attack.
            """

            # Generate AI response based on the prompt
            ai_response = await generate_response(prompt)

            # Extract action from AI response
            action = next((move for move in ["strike", "defend", "focus", "zenstrike", "mindtrap"] if move in ai_response.lower()), "strike")

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
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("subscriptionstatus", subscription_status_command))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("startpvp", start_pvp))
    application.add_handler(CommandHandler("acceptpvp", accept_pvp))
    application.add_handler(CommandHandler("surrender", surrender))
    application.add_handler(CommandHandler("deletedata", delete_data))
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

        logger.info("Zen Warrior PvP Bot and Web App are live. Press Ctrl+C to stop.")
        
        # Keep the script running
        while True:
            await asyncio.sleep(1)

if __name__ == '__main__':
    setup_database()  # Ensure the database is set up before starting the bot
    asyncio.run(main())