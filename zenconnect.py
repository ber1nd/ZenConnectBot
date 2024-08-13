import os
import asyncio
import sys
import logging
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
        return mysql.connector.connect(
            host=os.getenv("MYSQLHOST"),
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQLPORT", 3306))
        )
    except Error as e:
        logger.error(f"Error connecting to MySQL database: {e}")
        return None

async def delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor()
            
            # Start a transaction
            cursor.execute("START TRANSACTION")
            
            # Delete user data from all related tables
            tables = [
                "pvp_cooldowns",
                "pvp_battles",
                "subscriptions",
                "group_memberships",
                "meditation_log",
                "user_memory",
                "users"
            ]
            
            for table in tables:
                if table == "pvp_battles":
                    cursor.execute(f"DELETE FROM {table} WHERE challenger_id = %s OR opponent_id = %s", (user_id, user_id))
                else:
                    cursor.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
            
            # Commit the transaction
            cursor.execute("COMMIT")
            
            await update.message.reply_text("Your data has been deleted, including any active subscriptions. Your journey with us ends here, but remember that every ending is a new beginning.")
        except Error as e:
            # Rollback in case of error
            cursor.execute("ROLLBACK")
            logger.error(f"Database error in delete_data: {e}")
            await update.message.reply_text("I apologize, I'm having trouble deleting your data. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

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

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS pvp_cooldowns (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    move_name VARCHAR(50),
                    cooldown_end TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """)

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
            model="gpt-3.5-turbo",
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

async def zen_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    story = await generate_response("Tell me a short Zen story.")
    await update.message.reply_text(story)

async def zen_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = await generate_response("Give me a short Zen quote.")
    await update.message.reply_text(quote)

async def zen_advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    advice = await generate_response("Provide a piece of Zen advice for daily life.")
    await update.message.reply_text(advice)

async def random_wisdom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wisdom_type = random.choice(["quote", "advice", "story"])
    if wisdom_type == "quote":
        wisdom = await generate_response("Give me a short Zen quote.")
    elif wisdom_type == "advice":
        wisdom = await generate_response("Provide a piece of Zen advice for daily life.")
    else:
        wisdom = await generate_response("Tell me a short Zen story.")
    await update.message.reply_text(wisdom)

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
            cursor.execute("SELECT total_minutes, zen_points, level FROM users WHERE user_id = %s", (user_id,))
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
    
    db = get_db_connection()
    if not db:
        await update.message.reply_text("Unable to process your challenge right now. Please try again later.")
        return

    try:
        cursor = db.cursor(dictionary=True)
        
        # Get challenger's info
        cursor.execute("SELECT user_id, zen_points FROM users WHERE user_id = %s", (challenger.id,))
        challenger_info = cursor.fetchone()
        
        if not challenger_info:
            await update.message.reply_text("You need to start your Zen journey before challenging others.")
            return

        # Get opponent's info
        cursor.execute("SELECT user_id, zen_points FROM users WHERE username = %s", (opponent_username,))
        opponent_info = cursor.fetchone()
        
        if not opponent_info:
            await update.message.reply_text(f"User @{opponent_username} is not found in our records.")
            return

        # Check for existing battles for both challenger and opponent
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id IN (%s, %s) OR opponent_id IN (%s, %s))
            AND status != 'completed'
        """, (challenger.id, opponent_info['user_id'], challenger.id, opponent_info['user_id']))
        existing_battle = cursor.fetchone()

        if existing_battle:
            if existing_battle['challenger_id'] == challenger.id or existing_battle['opponent_id'] == challenger.id:
                await update.message.reply_text("You are already in a battle. Complete it before starting a new one.")
            else:
                await update.message.reply_text(f"@{opponent_username} is already in a battle. Try challenging someone else.")
            return

        # Create a new battle
        cursor.execute("""
            INSERT INTO pvp_battles (challenger_id, opponent_id, group_id, status, current_turn)
            VALUES (%s, %s, %s, 'pending', %s)
        """, (challenger.id, opponent_info['user_id'], chat_id, challenger.id))
        
        battle_id = cursor.lastrowid
        db.commit()

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

    except Error as e:
        logger.error(f"Database error in challenge: {e}")
        await update.message.reply_text("An error occurred while processing your challenge. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()
            db.close()

async def handle_challenge_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    response, battle_id = query.data.split(':')
    responder_id = query.from_user.id

    db = get_db_connection()
    if not db:
        await query.edit_message_text("Unable to process your response right now. Please try again later.")
        return

    try:
        cursor = db.cursor(dictionary=True)

        # Fetch the battle information
        cursor.execute("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))
        battle = cursor.fetchone()

        if not battle:
            await query.edit_message_text("This challenge is no longer valid.")
            return

        if battle['status'] != 'pending':
            await query.edit_message_text("This challenge has already been responded to.")
            return

        if responder_id != battle['opponent_id']:
            await query.answer("You are not the challenged player!", show_alert=True)
            return

        if response == 'accept_challenge':
            cursor.execute("""
                UPDATE pvp_battles 
                SET status = 'in_progress' 
                WHERE id = %s
            """, (battle_id,))
            
            db.commit()
            await start_battle(context.bot, query.message.chat_id, battle['challenger_id'], battle['opponent_id'], battle_id)
        else:  # decline_challenge
            cursor.execute("DELETE FROM pvp_battles WHERE id = %s", (battle_id,))
            
            db.commit()
            await query.edit_message_text("The challenge has been declined.")

    except Error as e:
        logger.error(f"Database error in handle_challenge_response: {e}")
        await query.edit_message_text("An error occurred while processing your response. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()
            db.close()

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

    db = get_db_connection()
    if not db:
        await query.edit_message_text("Unable to process your move right now. Please try again later.")
        return

    try:
        cursor = db.cursor(dictionary=True)
        
        # Fetch battle information
        cursor.execute("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))
        battle = cursor.fetchone()

        if not battle:
            await query.edit_message_text("This battle is no longer active.")
            return

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

        cursor.execute("""
            UPDATE pvp_battles 
            SET challenger_hp = CASE WHEN challenger_id = %s THEN challenger_hp ELSE GREATEST(challenger_hp - %s, 0) END,
                opponent_hp = CASE WHEN opponent_id = %s THEN opponent_hp ELSE GREATEST(opponent_hp - %s, 0) END,
                current_turn = %s, 
                last_move_timestamp = NOW()
            WHERE id = %s
        """, (current_player_id, damage, current_player_id, damage, new_turn, battle['id']))

        db.commit()

        # Fetch updated battle info
        cursor.execute("SELECT * FROM pvp_battles WHERE id = %s", (battle_id,))
        updated_battle = cursor.fetchone()

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

    except Error as e:
        logger.error(f"Database error in handle_battle_move: {e}")
        await query.edit_message_text("An error occurred while processing your move. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()
            db.close()

def process_move(move, player_id, battle):
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

async def end_battle(bot, chat_id, battle):
    db = get_db_connection()
    if not db:
        await bot.send_message(chat_id, "Unable to end the battle. Please contact support.")
        return

    try:
        cursor = db.cursor(dictionary=True)

        # Determine the winner
        if battle['challenger_hp'] <= 0:
            winner_id = battle['opponent_id']
            loser_id = battle['challenger_id']
        else:
            winner_id = battle['challenger_id']
            loser_id = battle['opponent_id']

        # Update battle status
        cursor.execute("""
            UPDATE pvp_battles 
            SET status = 'completed'
            WHERE id = %s
        """, (battle['id'],))

        # Fetch user data
        cursor.execute("SELECT user_id, zen_points FROM users WHERE user_id IN (%s, %s)", (winner_id, loser_id))
        user_data = {row['user_id']: row['zen_points'] for row in cursor.fetchall()}

        # Calculate Zen points transfer
        points_transfer = min(user_data[loser_id] // 10, 50)  # 10% of loser's points, max 50

        # Update Zen points
        cursor.execute("UPDATE users SET zen_points = zen_points + %s WHERE user_id = %s", (points_transfer, winner_id))
        cursor.execute("UPDATE users SET zen_points = GREATEST(zen_points - %s, 0) WHERE user_id = %s", (points_transfer, loser_id))

        db.commit()

        winner = await bot.get_chat_member(chat_id, winner_id)
        loser = await bot.get_chat_member(chat_id, loser_id)

        await bot.send_message(
            chat_id=chat_id,
            text=f"The Zen battle has ended! {winner.user.mention_html()} is victorious and gains {points_transfer} Zen points from {loser.user.mention_html()}.",
            parse_mode='HTML'
        )

    except Error as e:
        logger.error(f"Database error in end_battle: {e}")
        await bot.send_message(chat_id, "An error occurred while ending the battle. Please contact support.")
    finally:
        if db.is_connected():
            cursor.close()
            db.close()

async def cancel_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    db = get_db_connection()
    if not db:
        await update.message.reply_text("Unable to process your request right now. Please try again later.")
        return

    try:
        cursor = db.cursor(dictionary=True)

        # Check for active battles involving the user
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id = %s OR opponent_id = %s)
            AND status != 'completed'
            AND group_id = %s
        """, (user_id, user_id, chat_id))
        
        battle = cursor.fetchone()

        if not battle:
            await update.message.reply_text("You don't have any active battles in this group.")
            return

        # Cancel the battle
        cursor.execute("DELETE FROM pvp_battles WHERE id = %s", (battle['id'],))
        db.commit()

        await update.message.reply_text("Your active battle has been cancelled.")

    except Error as e:
        logger.error(f"Database error in cancel_battle: {e}")
        await update.message.reply_text("An error occurred while cancelling the battle. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()
            db.close()

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

def main():
    setup_database()

    application = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("zenstory", zen_story))
    application.add_handler(CommandHandler("zenquote", zen_quote))
    application.add_handler(CommandHandler("zenadvice", zen_advice))
    application.add_handler(CommandHandler("randomwisdom", random_wisdom))
    application.add_handler(CommandHandler("meditate", meditate))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("togglequote", togglequote))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("subscriptionstatus", subscription_status_command))
    application.add_handler(CommandHandler("challenge", challenge))
    application.add_handler(CommandHandler("cancelbattle", cancel_battle))
    application.add_handler(CommandHandler("deletedata", delete_data))

    application.add_handler(CallbackQueryHandler(handle_challenge_response, pattern="^(accept_challenge|decline_challenge):"))
    application.add_handler(CallbackQueryHandler(handle_battle_move, pattern="^battle_move:"))
    application.add_handler(CallbackQueryHandler(subscribe_callback, pattern="^subscribe$"))

    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.add_error_handler(error_handler)

    app = web.Application()
    app.router.add_get('/', serve_mini_app)
    app.router.add_get('/user_stats', get_user_stats)

    web.run_app(app, host='0.0.0.0', port=8080)

    application.run_polling()

if __name__ == '__main__':
    setup_database()  # Ensure the database is set up before starting the bot
    asyncio.run(main())
