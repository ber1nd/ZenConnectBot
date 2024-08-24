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

# PvP Functionality

async def send_game_rules(context: ContextTypes.DEFAULT_TYPE, user_id1: int, user_id2: int):
    rules_message = """
# Zen Warrior PvP Game Rules

Welcome to Zen Warrior PvP, a battle of wisdom and strategy!

## Core Mechanics:
- Each warrior starts with 100 HP and 50 Energy
- The battle continues until one warrior's HP reaches 0
- Energy is used to perform moves and is recovered over time

## Moves:
1. **Strike**: A basic attack (Cost: 12 Energy)
2. **Defend**: Heal and gain energy (Cost: 0 Energy, Gain: 10 Energy)
3. **Focus**: Recover energy and increase critical hit chance (Gain: 20-30 Energy)
4. **Zen Strike**: A powerful attack (Cost: 40 Energy)
5. **Mind Trap**: Weaken opponent's next move (Cost: 20 Energy)

## Key Synergies:
- **Focus → Strike**: Increased damage and critical hit chance
- **Focus → Zen Strike**: Significantly increased damage
- **Strike → Focus**: Extra energy gain
- **Defend → Mind Trap**: Reflect damage on the opponent's next attack

Remember, true mastery comes from understanding the flow of energy and the balance of actions. May your battles be enlightening!
    """
    await context.bot.send_message(chat_id=user_id1, text=rules_message, parse_mode='Markdown')
    if user_id2 != 7283636452:  # Don't send to the bot
        await context.bot.send_message(chat_id=user_id2, text=rules_message, parse_mode='Markdown')

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

        # Send game rules to both players
        await send_game_rules(context, user_id, opponent_id)

        if opponent_id == 7283636452:
            await update.message.reply_text("You have challenged the bot! The battle will begin now.")
            # Call start_new_battle for bot-initiated battle
            await start_new_battle(update, context)
            await accept_pvp(update, context)  # Auto-accept the challenge if the opponent is the bot
        else:
            await update.message.reply_text(f"Challenge sent to @{opponent_username}! They need to accept the challenge by using /acceptpvp.")

        # Automatically send move buttons if the battle is against the bot
        if opponent_id == 7283636452:
            # Here, ensure only one set of buttons is displayed
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

        # Reset synergies and effects at the start of the new battle
        await start_new_battle(update, context)

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
    def create_bar(value, max_value, fill_char='█', empty_char='░'):
        bar_length = 10
        filled = int((value / max_value) * bar_length)
        return f"{fill_char * filled}{empty_char * (bar_length - filled)}"

    c_hp_bar = create_bar(challenger_hp, 100)
    c_energy_bar = create_bar(challenger_energy, 100)
    o_hp_bar = create_bar(opponent_hp, 100)
    o_energy_bar = create_bar(opponent_energy, 100)

    battle_view = f"""
┏━━━━━━━━━━━━━━━ ZEN WARRIOR ARENA ━━━━━━━━━━━━━━━┓
┃                                                  ┃
┃  ⚪ {challenger_name:<20}                         ┃
┃  ✦ HP   [{c_hp_bar}] {challenger_hp:3d}/100                ┃
┃  ✦ Chi  [{c_energy_bar}] {challenger_energy:3d}/100                ┃
┃                                                  ┃
┃                    ⚔  VS  ⚔                     ┃
┃                                                  ┃
┃  ⚪ {opponent_name:<20}                         ┃
┃  ✦ HP   [{o_hp_bar}] {opponent_hp:3d}/100                ┃
┃  ✦ Chi  [{o_energy_bar}] {opponent_energy:3d}/100                ┃
┃                                                  ┃
┗━━━━━━━━━━━ Choose Your Path, Warrior ━━━━━━━━━━━┛
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
            await update.message.reply_text("Invalid move. Please use the provided buttons.")
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
            previous_move = context.user_data.get('challenger_previous_move')  # Track challenger move
        else:
            user_hp, opponent_hp = battle['opponent_hp'], battle['challenger_hp']
            user_energy = context.user_data.get('opponent_energy', 50)
            opponent_energy = context.user_data.get('challenger_energy', 50)
            previous_move = context.user_data.get('opponent_previous_move')  # Track opponent move

        # Reset energy gain/loss conditions
        context.user_data['energy_gain'] = 0
        context.user_data['energy_loss'] = 0

        # Action logic with synergy effects
        if action == "strike":
            energy_cost = 12
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Strike.")
                return
            damage = random.randint(12, 18)

            if previous_move == "focus":
                damage = round(damage * 1.1)
                synergy_message = "The focus enhances the strike, adding extra force."
                synergy_effects['critical_hit_chance'] = 0.20
            elif previous_move == "zenstrike":
                damage = round(damage * 1.1)
                synergy_message = "The momentum from the Zen Strike increases the strike's power."
                synergy_effects['critical_hit_chance'] = 0.15
            else:
                synergy_message = ""
                synergy_effects['critical_hit_chance'] = 0.10

            critical_hit = random.random() < synergy_effects['critical_hit_chance']
            if critical_hit:
                damage *= 2
                critical_hit_message = "A well-placed strike hits critically, doubling the damage!"
            else:
                critical_hit_message = ""

            # Check if Mind Trap is active
            if context.user_data.get('opponent_mind_trap'):
                original_damage = damage
                damage //= 2  # Reduces the damage by half due to Mind Trap
                context.user_data['energy_loss'] = 10
                mind_trap_message = f"But the Mind Trap dampens the force, reducing the damage by {original_damage - damage}. Only {damage} damage is dealt."
            else:
                mind_trap_message = ""

            opponent_hp = max(0, opponent_hp - damage)
            
            # Final Narrative for Strike
            result_message = f"{'Bot' if bot_mode else update.effective_user.first_name} jumps and kicks {opponent_name}, aiming for a strong hit. {synergy_message} {critical_hit_message} {mind_trap_message} The strike deals {damage} damage."

        elif action == "zenstrike":
            energy_cost = 40
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Zen Strike.")
                return
            damage = random.randint(20, 30)

            if previous_move == "focus":
                damage = round(damage * 1.5)
                synergy_message = "The focus amplifies the Zen Strike, increasing its power."
            else:
                synergy_message = ""

            critical_hit = random.random() < 0.20
            if critical_hit:
                damage *= 2
                critical_hit_message = "The Zen Strike hits critically, doubling the damage!"
            else:
                critical_hit_message = ""

            # Check if Mind Trap is active
            if context.user_data.get('opponent_mind_trap'):
                original_damage = damage
                damage //= 2  # Reduces the damage by half due to Mind Trap
                context.user_data['energy_loss'] = 10
                mind_trap_message = f"But the Mind Trap dampens the force, reducing the damage by {original_damage - damage}. Only {damage} damage is dealt."
            else:
                mind_trap_message = ""

            opponent_hp = max(0, opponent_hp - damage)
            
            # Final Narrative for Zen Strike
            result_message = f"{'Bot' if bot_mode else update.effective_user.first_name} channels their inner energy for a powerful Zen Strike. {synergy_message} {critical_hit_message} {mind_trap_message} The Zen Strike deals {damage} damage."

        elif action == "mindtrap":
            energy_cost = 20
            if user_energy < energy_cost:
                await send_message(update, "Not enough energy to use Mind Trap.")
                return
            context.user_data['opponent_mind_trap'] = True
            result_message = f"{'Bot' if bot_mode else update.effective_user.first_name} sets up a Mind Trap, weakening the opponent's next move."

        elif action == "defend":
            energy_gain = 10
            result_message = f"{'Bot' if bot_mode else update.effective_user.first_name} defends, healing and gaining 10 energy."

        elif action == "focus":
            energy_gain = random.randint(20, 30)
            result_message = f"{'Bot' if bot_mode else update.effective_user.first_name} focuses, recovering {energy_gain} energy and increasing critical hit chance."

        # Save the current move as previous_move for next turn's synergy
        if is_challenger:
            context.user_data['challenger_previous_move'] = action
            context.user_data['challenger_energy'] = user_energy
            context.user_data['opponent_energy'] = opponent_energy
        else:
            context.user_data['opponent_previous_move'] = action
            context.user_data['opponent_energy'] = user_energy
            context.user_data['challenger_energy'] = opponent_energy

        # Check if the battle ends
        if opponent_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (user_id, battle['id']))
            db.commit()
            final_action = f"The final move was {action} that dealt {damage} damage." if 'damage' in locals() else "The battle concluded."
            await send_message(update, f"{'Bot' if bot_mode else update.effective_user.first_name} has won the battle! Your opponent is defeated. {final_action}")
            await context.bot.send_message(chat_id=battle['group_id'], text=f"{'Bot' if bot_mode else update.effective_user.username} has won the battle!")
            return
        elif user_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (opponent_id, battle['id']))
            db.commit()
            final_action = f"The final move was {action} that dealt {damage} damage." if 'damage' in locals() else "The battle concluded."
            await send_message(update, f"{'Bot' if bot_mode else update.effective_user.first_name} has been defeated. {final_action}")
            await context.bot.send_message(chat_id=battle['group_id'], text=f"{opponent_name} has won the battle!")
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

    except Exception as e:
        logger.error(f"Error in execute_pvp_move: {e}")
        if not bot_mode and update.callback_query:
            await update.callback_query.answer("An error occurred while executing the PvP move. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()

        if not bot_mode and update.callback_query:
            await update.callback_query.answer()

# Call this function at the start of a new battle
async def start_new_battle(update, context):
    reset_synergies(context)
    await update.message.reply_text("A new battle has begun! All synergies and effects have been reset.")

def reset_synergies(context):
    context.user_data['challenger_previous_move'] = None
    context.user_data['opponent_previous_move'] = None
    context.user_data['opponent_mind_trap'] = False
    context.user_data['focus_active'] = False
    context.user_data['energy_loss'] = 0
    context.user_data['energy_gain'] = 0
    context.user_data['challenger_energy'] = 50
    context.user_data['opponent_energy'] = 50

async def execute_pvp_move_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db_connection()
    if db:
        try:
            await execute_pvp_move(update, context, db)
        finally:
            if db.is_connected():
                db.close()