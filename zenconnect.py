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


class ZenQuest:
    def __init__(self):
        self.quest_active = {}
        self.player_hp = {}
        self.current_stage = {}
        self.story = {}
        self.current_scene = {}
        self.in_combat = {}
        self.quest_state = {}  # To track overall quest progress

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if update.message.chat.type != 'private':
            await update.message.reply_text("Zen quests can only be started in private chats with the bot.")
            return

        try:
            self.quest_active[user_id] = True
            self.player_hp[user_id] = 100
            self.current_stage[user_id] = 0
            self.quest_state[user_id] = "beginning"
            initial_scene = await self.generate_initial_scene()
            self.current_scene[user_id] = initial_scene
            await self.send_scene(update, context)
        except Exception as e:
            logger.error(f"Error starting quest: {e}")
            await update.message.reply_text("An error occurred while starting the quest. Please try again.")
            self.quest_active[user_id] = False

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False) or update.message.chat.type != 'private':
            return

        user_input = update.message.text.lower()

        if user_input == "/surrender":
            await self.end_quest(update, context, victory=False, reason="You have surrendered your quest.")
            return

        if user_input == "/interrupt":
            await self.interrupt_quest(update, context)
            return

        if self.in_combat.get(user_id, False):
            await execute_pvp_move_wrapper(update, context)
        else:
            await self.progress_story(update, context, user_input)

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
        user_id = update.effective_user.id

        try:
            next_scene = await self.generate_next_scene(self.current_scene[user_id], user_input, self.quest_state[user_id])
            self.current_scene[user_id] = next_scene

            if "COMBAT_START" in next_scene:
                self.in_combat[user_id] = True
                await self.initiate_combat(update, context)
            elif "QUEST_COMPLETE" in next_scene:
                await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
            elif "QUEST_FAIL" in next_scene:
                await self.end_quest(update, context, victory=False, reason="Your journey has come to an unfortunate end.")
            else:
                self.current_stage[user_id] += 1
                await self.update_quest_state(user_id)
                await self.send_scene(update, context)

        except Exception as e:
            logger.error(f"Error progressing story: {e}", exc_info=True)
            await update.message.reply_text("An error occurred while processing your action. Your quest has been reset. Please start a new quest with /zenquest.")
            self.quest_active[user_id] = False

    async def update_quest_state(self, user_id):
        if self.current_stage[user_id] == 1:
            self.quest_state[user_id] = "middle"
        elif self.current_stage[user_id] > 5:  # Arbitrary number, can be adjusted
            self.quest_state[user_id] = "nearing_end"

    async def generate_initial_scene(self):
        prompt = """
        Generate the opening scene for a Zen-themed quest. Include:
        1. A vivid description of the starting environment
        2. The quest's purpose and initial challenge
        3. At least three distinct choices for the player to begin their journey
        4. A hint of the challenges and growth that lie ahead

        The scene should be engaging and open-ended, allowing for various paths forward.
        Do not use any scene titles or headers.
        """
        return await generate_response(prompt, elaborate=True)

    async def generate_next_scene(self, previous_scene, user_input, quest_state):
        prompt = f"""
        Previous scene: {previous_scene}
        User's action: "{user_input}"
        Current quest state: {quest_state}

        Generate the next concise scene of the Zen-themed quest. Include:
        1. A brief description of the environment and consequences of the user's action (max 2 sentences)
        2. Three short, distinct choices for the player (each choice should be one sentence)
        3. A subtle hint or challenge appropriate to the current quest state
        4. A brief Zen-like insight or question

        Keep the response engaging and concise. If appropriate, introduce:
        - A combat situation (indicate with 'COMBAT_START')
        - A successful quest completion (indicate with 'QUEST_COMPLETE')
        - An unsuccessful quest ending (indicate with 'QUEST_FAIL')

        Ensure all situations and actions are realistic for a human character.
        The quest should only end if the player's choices naturally lead to a conclusion.
        Total response should not exceed 100 words.
        """
        return await generate_response(prompt, elaborate=False)

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        opponent_id = 7283636452  # Bot's ID

        db = get_db_connection()
        if db:
            try:
                cursor = db.cursor(dictionary=True)
                cursor.execute("""
                    INSERT INTO pvp_battles (challenger_id, opponent_id, group_id, current_turn, status)
                    VALUES (%s, %s, %s, %s, 'in_progress')
                """, (user_id, opponent_id, update.effective_chat.id, user_id))
                db.commit()

                context.user_data['challenger_energy'] = 50
                context.user_data['opponent_energy'] = 50

                await update.message.reply_text("A hostile presence emerges. Prepare for battle!")
                await send_game_rules(context, user_id, opponent_id)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Choose your move:",
                    reply_markup=generate_pvp_move_buttons(user_id)
                )
            finally:
                if db.is_connected():
                    cursor.close()
                    db.close()

    async def send_scene(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.current_scene.get(user_id):
            await update.message.reply_text("An error occurred. The quest cannot continue.")
            return

        scene = self.current_scene[user_id]
        chunks = self.split_message(scene, max_length=2000)  # Reduced max_length
        for chunk in chunks:
            await update.message.reply_text(chunk)

    def split_message(self, message, max_length=2000):
        chunks = []
        lines = message.split('\n')
        current_chunk = ""
        for line in lines:
            if len(current_chunk) + len(line) + 1 <= max_length:
                current_chunk += line + "\n"
            else:
                chunks.append(current_chunk.strip())
                current_chunk = line + "\n"
        if current_chunk:
            chunks.append(current_chunk.strip())
        return chunks

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        user_id = update.effective_user.id
        self.quest_active[user_id] = False
        self.in_combat[user_id] = False

        conclusion = await self.generate_quest_conclusion(victory, self.current_stage[user_id])
        await update.message.reply_text(conclusion)

        if victory:
            zen_points = random.randint(30, 50)
            await update.message.reply_text(f"{reason} You have earned {zen_points} Zen points!")
            await add_zen_points(update, context, zen_points)
        else:
            await update.message.reply_text(f"{reason} Your quest has ended.")

        # Clear quest data for this user
        self.player_hp.pop(user_id, None)
        self.current_stage.pop(user_id, None)
        self.current_scene.pop(user_id, None)
        self.quest_state.pop(user_id, None)

    async def generate_quest_conclusion(self, victory: bool, stages_completed):
        prompt = f"""
        Generate a concluding paragraph for a Zen-themed quest where the player has {'succeeded' if victory else 'failed'}.
        Stages completed: {stages_completed}
        The conclusion should:
        1. Summarize the journey and its challenges
        2. Highlight the wisdom or lessons learned
        3. Provide a sense of {'accomplishment and growth' if victory else 'reflection and potential for future growth'}
        4. Tie back to the original purpose of the quest
        5. End with a thought-provoking Zen-like statement or question
        """
        return await generate_response(prompt, elaborate=True)

    async def interrupt_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if self.quest_active.get(user_id, False):
            self.quest_active[user_id] = False
            self.in_combat[user_id] = False
            await update.message.reply_text("Your quest has been interrupted. You can start a new one with /zenquest.")
            # Clear quest data for this user
            self.player_hp.pop(user_id, None)
            self.current_stage.pop(user_id, None)
            self.current_scene.pop(user_id, None)
            self.quest_state.pop(user_id, None)
        else:
            await update.message.reply_text("You don't have an active quest to interrupt.")

zen_quest = ZenQuest()



async def zenquest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.chat.type != 'private':
        await update.message.reply_text("Zen quests can only be started in private chats with the bot.")
        return

    if zen_quest.quest_active.get(user_id, False):
        await update.message.reply_text("You are already on a quest. Use /surrender to end your current quest before starting a new one.")
    else:
        await zen_quest.start_quest(update, context)

async def interrupt_quest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.interrupt_quest(update, context)

async def handle_quest_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if zen_quest.quest_active:
        await zen_quest.handle_input(update, context)
    else:
        # If no quest is active, pass the message to the regular message handler
        await handle_message(update, context)


@with_database_connection
async def add_zen_points(update: Update, context: ContextTypes.DEFAULT_TYPE, points: int, db):
    user_id = update.effective_user.id
    try:
        cursor = db.cursor()
        cursor.execute("UPDATE users SET zen_points = zen_points + %s WHERE user_id = %s", (points, user_id))
        db.commit()
        await update_user_level(user_id, points, db)
    except Error as e:
        logger.error(f"Error adding Zen points: {e}")
        await update.message.reply_text("An error occurred while updating your Zen points. Please try again later.")

def setup_zenquest_handlers(application):
    application.add_handler(CommandHandler("zenquest", zenquest_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=lambda id: zen_quest.quest_active),
        handle_quest_input
    ))


async def initiate_combat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()

    if db:
        try:
            cursor = db.cursor(dictionary=True)

            # Create a new PvP battle against the bot
            opponent_id = 7283636452  # Bot's ID
            context.user_data['challenger_energy'] = 50
            context.user_data['opponent_energy'] = 50

            cursor.execute("""
                INSERT INTO pvp_battles (challenger_id, opponent_id, group_id, current_turn, status)
                VALUES (%s, %s, %s, %s, 'pending')
            """, (user_id, opponent_id, update.effective_chat.id, user_id))
            db.commit()

            await send_game_rules(context, user_id, opponent_id)
            await update.message.reply_text("A hostile presence emerges. Prepare for battle!")

            # Start the combat automatically
            await start_new_battle(update, context)
            await accept_pvp(update, context)  # Auto-accept the challenge if the opponent is the bot

        except mysql.connector.Error as e:
            logger.error(f"MySQL error in initiate_combat: {e}")
            await update.message.reply_text("An error occurred while initiating the combat. Please try again later.")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")

async def send_split_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str):
    max_message_length = 4000  # Telegram's message limit is 4096 characters, but we'll leave a margin
    message_parts = [message[i:i + max_message_length] for i in range(0, len(message), max_message_length)]

    for part in message_parts:
        await update.message.reply_text(part)

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
    opponent_username = "bot" if len(context.args) == 1 and context.args[0].lower() == "bot" else context.args[0].replace('@', '') if context.args else None

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
⚪ {challenger_name}
━━━━━━━━━━━━━━━━━
💚 HP  [{c_hp_bar}] {challenger_hp}
💠 Chi [{c_energy_bar}] {challenger_energy}
━━━━━━━━━━━━━━━━━
           ☯
⚪ {opponent_name}
━━━━━━━━━━━━━━━━━
💚 HP  [{o_hp_bar}] {opponent_hp}
💠 Chi [{o_energy_bar}] {opponent_energy}
━━━━━━━━━━━━━━━━━
"""
    return battle_view




# Call this function at the start of a new battle
async def start_new_battle(update, context):
    reset_synergies(context)
    await update.message.reply_text("A new battle has begun! All synergies and effects have been reset.")

def reset_synergies(context):
    context.user_data['challenger_previous_move'] = None
    context.user_data['opponent_previous_move'] = None  # Track opponent's last move
    context.user_data['opponent_mind_trap'] = False
    context.user_data['focus_active'] = False
    context.user_data['energy_loss'] = 0
    context.user_data['energy_gain'] = 0
    context.user_data['challenger_energy'] = 50
    context.user_data['opponent_energy'] = 50

async def bot_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
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

            # Determine if the bot is the challenger or opponent
            bot_is_challenger = battle['challenger_id'] == 7283636452

            # Retrieve bot's HP, energy, and previous move
            if bot_is_challenger:
                bot_hp = battle['challenger_hp']
                bot_energy = context.user_data.get('challenger_energy', 50)
                bot_previous_move = context.user_data.get('challenger_previous_move', 'None')
                opponent_hp = battle['opponent_hp']
            else:
                bot_hp = battle['opponent_hp']
                bot_energy = context.user_data.get('opponent_energy', 50)
                bot_previous_move = context.user_data.get('opponent_previous_move', 'None')
                opponent_hp = battle['challenger_hp']

            # Generate AI decision-making prompt
            prompt = f"""
            You are a Zen warrior AI engaged in a strategic duel. Your goal is to win decisively by reducing your opponent's HP to 0 while keeping your HP above 0.

            Current situation:
            - Your HP: {bot_hp}/100
            - Opponent's HP: {opponent_hp}/100
            - Your Energy: {bot_energy}/100
            - Your Last Move: {bot_previous_move}

            Available actions:
            - Strike: Deal moderate damage to the opponent. Costs 12 energy.
            - Defend: Heal yourself and gain energy. Costs 0 energy, gains 10 energy.
            - Focus: Recover energy and increase your critical hit chances for the next turn. Gains 20-30 energy.
            - Zen Strike: A powerful move that deals significant damage. Costs 40 energy.
            - Mind Trap: Reduces the effectiveness of the opponent's next move by 50%. Costs 20 energy.

            Strategy to win:
            - If your energy is low (below 20), prioritize "Focus" or "Defend" to recover energy.
            - Avoid attempting an action if you don't have enough energy to perform it.
            - Manage your energy carefully; don't allow it to drop too low unless you can deliver a finishing blow.
            - If you used "Focus" in the previous move, consider following up with "Strike" or "Zen Strike" for enhanced damage.
            - Use "Mind Trap" to weaken the opponent, particularly if they have high energy or if you want to set up a safer "Zen Strike."
            - Prioritize "Zen Strike" if the opponent's HP is low enough for a potential finishing blow.
            """

            # Generate AI response based on the prompt
            ai_response = await generate_response(prompt)

            # Extract action from AI response, prioritizing energy management
            if "zen strike" in ai_response.lower() and bot_energy >= 40:
                action = "zenstrike"
            elif "strike" in ai_response.lower() and bot_energy >= 12:
                action = "strike"
            elif "mind trap" in ai_response.lower() and bot_energy >= 20:
                action = "mindtrap"
            elif "focus" in ai_response.lower():
                action = "focus"
            else:
                action = "defend"

            logger.info(f"Bot chose action: {action} based on AI response: {ai_response}")

            # Execute the chosen move
            await execute_pvp_move(update, context, db, bot_mode=True, action=action)

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

            # End the quest if it's active
            if zen_quest.quest_active.get(user_id, False):
                zen_quest.in_combat[user_id] = False
                await zen_quest.end_quest(update, context, victory=False, reason="You have surrendered in combat.")

        except mysql.connector.Error as e:
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
    chat_id = update.message.chat_id

    logger.info(f"Message received - Chat Type: {chat_type}, User ID: {user_id}, Chat ID: {chat_id}")
    logger.info(f"Message content: {user_message}")

    # Check if it's a private chat
    if chat_type == 'private':
        logger.info("Private chat detected, processing message")
        if user_message.lower() == '/zenquest':
            await zenquest_command(update, context)
        elif user_message.lower() == '/interrupt':
            await zen_quest.interrupt_quest(update, context)
        elif zen_quest.quest_active.get(user_id, False):
            await zen_quest.handle_input(update, context)
        else:
            await process_message(update, context)
        return

    # From this point, we're dealing with a group chat
    if chat_type != 'group' and chat_type != 'supergroup':
        logger.warning(f"Unexpected chat type: {chat_type}. Ignoring message.")
        return

    bot_username = context.bot.username.lower() if context.bot.username else "unknown"
    logger.info(f"Bot username: {bot_username}")

    # Check if the bot should respond in this group chat
    should_respond = False
    if f'@{bot_username}' in user_message.lower():
        should_respond = True
        logger.info("Bot mentioned in the message")
    elif 'zen' in user_message.lower():
        should_respond = True
        logger.info("'zen' keyword found in the message")
    elif any(cmd in user_message.lower() for cmd in ['/start', '/help', '/zenquest', '/checkpoints', '/startpvp', '/surrender']):
        should_respond = True
        logger.info("Command detected in the message")

    if not should_respond:
        logger.info("Ignoring group message - bot not mentioned and no keywords/commands found")
        return

    logger.info("Bot will respond to this group message")

    # Process the message for group chats
    await process_message(update, context)

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    chat_id = update.message.chat_id

    logger.info(f"Processing message for User ID: {user_id}, Chat ID: {chat_id}")

    # Apply rate limiting
    if not check_rate_limit(user_id):
        logger.info(f"Rate limit exceeded for user {user_id}")
        await update.message.reply_text("Please wait a moment before sending another message. Zen teaches us the value of patience.")
        return

    rate_limit_dict[user_id].append(datetime.now())

    db = get_db_connection()
    if not db:
        logger.error("Failed to establish database connection")
        await update.message.reply_text("I'm having trouble accessing my memory right now. Please try again later.")
        return

    try:
        cursor = db.cursor()

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
              update.effective_user.last_name, 'group' if update.message.chat.type in ['group', 'supergroup'] else 'private'))

        # If it's a group chat, update group membership
        if update.message.chat.type in ['group', 'supergroup']:
            cursor.execute("""
                INSERT IGNORE INTO group_memberships (user_id, group_id)
                VALUES (%s, %s)
            """, (user_id, chat_id))

        # Fetch user memory
        cursor.execute("SELECT memory FROM user_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 5", (user_id,))
        results = cursor.fetchall()

        memory = "\n".join([result[0] for result in results[::-1]]) if results else ""

        elaborate = any(word in user_message.lower() for word in ['why', 'how', 'explain', 'elaborate', 'tell me more'])

        prompt = f"""You are a wise Zen monk having a conversation with a student. 
        Here's the recent conversation history:

        {memory}

        Student: {user_message}
        Zen Monk: """

        logger.info("Generating response")
        response = await generate_response(prompt, elaborate)
        logger.info("Response generated")

        new_memory = f"Student: {user_message}\nZen Monk: {response}"
        cursor.execute("INSERT INTO user_memory (user_id, group_id, memory) VALUES (%s, %s, %s)", (user_id, chat_id if update.message.chat.type in ['group', 'supergroup'] else None, new_memory))
        db.commit()

        # Split and send the response if it's too long
        max_length = 4000
        for i in range(0, len(response), max_length):
            await update.message.reply_text(response[i:i+max_length])
        logger.info("Response sent successfully")

    except Error as e:
        logger.error(f"Database error in handle_message: {e}")
        await update.message.reply_text("I'm having trouble processing your message. Please try again later.")

    finally:
        if db.is_connected():
            cursor.close()
            db.close()
        logger.info("Database connection closed")


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
    application.add_handler(CommandHandler("zenquest", zenquest_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("subscriptionstatus", subscription_status_command))
    application.add_handler(CommandHandler("checkpoints", check_points))
    application.add_handler(CommandHandler("startpvp", start_pvp))
    application.add_handler(CommandHandler("acceptpvp", accept_pvp))
    application.add_handler(CommandHandler("surrender", surrender))
    application.add_handler(CommandHandler("interrupt", interrupt_quest_command))
    application.add_handler(CommandHandler("deletedata", delete_data))
    application.add_handler(CommandHandler("getbotid", getbotid))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_quest_input
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

import re

def escape_markdown_v2(text):
    """
    Helper function to escape special characters for MarkdownV2 format.
    """
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    return re.sub(f"([{''.join(map(re.escape, special_chars))}])", r'\\\1', str(text))

async def perform_action(action, user_hp, opponent_hp, user_energy, current_synergy, context, player_key, bot_mode, opponent_name):
    energy_cost = 0
    energy_gain = 0
    damage = 0
    heal = 0
    synergy_effect = ""
    critical_hit = False

    opponent_key = 'opponent' if player_key == 'challenger' else 'challenger'
    previous_move = context.user_data.get(f'{player_key}_previous_move')
    
    # Define energy costs
    energy_costs = {
        "strike": 12,
        "zenstrike": 40,
        "mindtrap": 20,
        "defend": 0,
        "focus": 0
    }

    # Check if there's enough energy for the move
    if user_energy < energy_costs[action]:
        return None, user_hp, opponent_hp, user_energy, 0, 0, 0, 0, "Not enough energy for this move."

    # Apply Mind Trap effect if it's active
    mind_trap_active = context.user_data.get(f'{player_key}_mind_trap_active', False)
    mind_trap_multiplier = 0.5 if mind_trap_active else 1.0
    context.user_data[f'{player_key}_mind_trap_active'] = False  # Reset after applying

    # Check if Focus is active from the previous turn
    focus_active = context.user_data.get(f'{player_key}_focus_active', False)

    # New function to handle reflect damage
    def apply_reflect_damage(damage, player_key):
        reflect_percentage = context.user_data.get(f'{player_key}_reflect_damage', 0)
        if reflect_percentage > 0:
            reflected_damage = round(damage * reflect_percentage)
            context.user_data[f'{player_key}_reflect_damage'] = 0  # Reset after use
            return reflected_damage
        return 0

    if action == "strike":
        energy_cost = energy_costs["strike"]
        damage = random.randint(12, 18)
        critical_hit_chance = 0.10

        if focus_active:
            damage = round(damage * 1.2)  # 20% increase in damage
            critical_hit_chance = 0.30  # Increased critical hit chance
            synergy_effect = "Focus boosts your Strike, adding extra power and critical chance."
        elif previous_move == 'defend':
            damage = round(damage * 1.1)
            critical_hit_chance = 0.20
            synergy_effect = "Your defensive stance empowers your Strike."
            if random.random() < 0.5:
                energy_gain = 5
                synergy_effect += " You regain 5 energy."
        elif previous_move == 'mindtrap':
            damage = round(damage * 1.15)  # 15% bonus damage
            context.user_data[f'{opponent_key}_next_attack_reduction'] = 0.9  # 10% reduction in opponent's next attack
            synergy_effect = "Your previous Mind Trap enhances your Strike, dealing bonus damage and weakening your opponent's next attack."

        critical_hit = random.random() < critical_hit_chance
        if critical_hit:
            damage *= 2
            synergy_effect += " Critical hit! You double the damage."
            if focus_active:
                energy_gain += 10

        damage = round(damage * mind_trap_multiplier)
        reflected_damage = apply_reflect_damage(damage, opponent_key)
        opponent_hp = max(0, opponent_hp - damage)
        user_hp = max(0, user_hp - reflected_damage)
        if reflected_damage > 0:
            synergy_effect += f" The opponent's reflection deals {reflected_damage} damage back to you."

    elif action == "zenstrike":
        energy_cost = energy_costs["zenstrike"]
        damage = random.randint(20, 30)
        critical_hit_chance = 0.20

        if focus_active:
            damage = round(damage * 1.3)  # 30% increase in damage
            critical_hit_chance = 0.50  # Significantly increased critical hit chance
            synergy_effect = "Focus empowers your Zen Strike, greatly amplifying its impact and critical chance."
        elif previous_move == 'strike':
            damage = round(damage * 1.1)
            critical_hit_chance = 0.35
            synergy_effect = "Your previous Strike enhances Zen Strike's power."

        critical_hit = random.random() < critical_hit_chance
        if critical_hit:
            damage *= 2
            synergy_effect += " Critical hit! Your Zen Strike devastates the opponent."

        damage = round(damage * mind_trap_multiplier)
        reflected_damage = apply_reflect_damage(damage, opponent_key)
        opponent_hp = max(0, opponent_hp - damage)
        user_hp = max(0, user_hp - reflected_damage)
        if reflected_damage > 0:
            synergy_effect += f" The opponent's reflection deals {reflected_damage} damage back to you."

    elif action == "defend":
        energy_gain = 10
        heal = random.randint(15, 25)

        if focus_active:
            heal = round(heal * 1.3)  # 30% increase in healing
            synergy_effect = "Focus increases your healing power significantly."

        if previous_move == 'zenstrike':
            heal += 10
            context.user_data[f'{player_key}_next_move_reduction'] = 0.8  # 20% damage reduction on next opponent's attack
            synergy_effect = "Your previous Zen Strike enhances your Defend, providing additional healing and reducing the next attack against you."
        elif previous_move == 'mindtrap':
            context.user_data[f'{player_key}_reflect_damage'] = 0.1  # 10% reflect damage
            synergy_effect = "Your previous Mind Trap enhances Defend, preparing to reflect 10% of the opponent's next attack damage."

        heal = round(heal * mind_trap_multiplier)
        user_hp = min(100, user_hp + heal)

    elif action == "focus":
        base_energy_gain = random.randint(20, 30)
        energy_gain = round(base_energy_gain * mind_trap_multiplier)
        context.user_data[f'{player_key}_focus_active'] = True
        synergy_effect = f"Focus prepares you for the next move, recovering {energy_gain} energy and enhancing your next action."

        if previous_move == 'zenstrike':
            energy_gain *= 2
            context.user_data[f'{player_key}_next_move_penalty'] = 0.9  # 10% reduction in next move's effectiveness
            synergy_effect += f" Zen Strike boosts Focus, doubling energy gain to {energy_gain} but slightly reducing next move's power."
        elif previous_move == 'strike':
            energy_gain += 10
            context.user_data[f'{player_key}_focus_strike_synergy'] = True
            synergy_effect += " Your previous Strike enhances Focus, providing additional energy recovery and boosting your next Strike."
        elif previous_move == 'mindtrap':
            energy_gain = round(energy_gain * 1.5)  # 50% boost to energy gain
            context.user_data[f'{opponent_key}_next_focus_reduction'] = 0.5  # 50% reduction in opponent's next Focus
            synergy_effect += " Your previous Mind Trap enhances Focus, boosting your energy gain and reducing the effectiveness of the opponent's next Focus."

    elif action == "mindtrap":
        energy_cost = energy_costs["mindtrap"]
        context.user_data[f'{opponent_key}_mind_trap_active'] = True
        context.user_data[f'{opponent_key}_energy_loss'] = 10
        synergy_effect = "Mind Trap set. Your opponent's next move will be weakened, and they'll lose energy if they attack."

        if previous_move == 'strike':
            context.user_data[f'{opponent_key}_energy_loss'] += 5  # Additional energy loss
            synergy_effect += " Your previous Strike enhances Mind Trap, causing additional energy loss to your opponent."
        elif previous_move == 'defend':
            context.user_data[f'{player_key}_reflect_damage'] = 0.1  # 10% reflect damage
            synergy_effect += " Your previous Defend enhances Mind Trap, preparing to reflect 10% of the opponent's next attack damage."

    # Update energy and previous move
    user_energy = max(0, min(100, user_energy - energy_cost + energy_gain))
    context.user_data[f'{player_key}_previous_move'] = action

    # Reset Focus active status if it was used this turn
    if action != "focus":
        context.user_data[f'{player_key}_focus_active'] = False

    # Apply energy loss from opponent's Mind Trap if it's an offensive move
    if action in ['strike', 'zenstrike']:
        energy_loss = context.user_data.get(f'{player_key}_energy_loss', 0)
        additional_energy_drain = context.user_data.get(f'{player_key}_additional_energy_drain', 0)
        user_energy = max(0, user_energy - energy_loss - additional_energy_drain)
        context.user_data[f'{player_key}_energy_loss'] = 0
        context.user_data[f'{player_key}_additional_energy_drain'] = 0

    # Round all numeric values
    user_hp = round(user_hp)
    opponent_hp = round(opponent_hp)
    user_energy = round(user_energy)
    damage = round(damage)
    heal = round(heal)
    energy_cost = round(energy_cost)
    energy_gain = round(energy_gain)

    # Generate dynamic message
    dynamic_message = await generate_response(f"""
    {'' if bot_mode else 'You'} just performed {action}.
    Damage dealt: {damage if action in ['strike', 'zenstrike'] else 'N/A'}
    Healing done: {heal if action == 'defend' else 'N/A'}
    Energy cost: {energy_cost} 
    Energy gained: {energy_gain if action in ['defend', 'focus'] else 'N/A'}
    Synergy effect: {synergy_effect}
    Opponent: {opponent_name}
    """, elaborate=True)

    # Combine dynamic message with results
    result_message = f"{dynamic_message}\n\n{synergy_effect}"
    
    return result_message, user_hp, opponent_hp, user_energy, damage, heal, energy_cost, energy_gain, synergy_effect

async def execute_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE, db, bot_mode=False, action=None):
    user_id = 7283636452 if bot_mode else update.effective_user.id

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

    if action not in ["strike", "defend", "focus", "zenstrike", "mindtrap"]:
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

        is_challenger = battle['challenger_id'] == user_id
        player_key = 'challenger' if is_challenger else 'opponent'
        opponent_key = 'opponent' if is_challenger else 'challenger'

        user_hp = battle[f'{player_key}_hp']
        opponent_hp = battle[f'{opponent_key}_hp']
        user_energy = context.user_data.get(f'{player_key}_energy', 50)
        opponent_energy = context.user_data.get(f'{opponent_key}_energy', 50)

        opponent_id = battle[f'{opponent_key}_id']
        opponent_name = "Bot" if opponent_id == 7283636452 else update.effective_user.first_name if bot_mode else "Opponent"

        # Perform the action
        result = await perform_action(
            action, user_hp, opponent_hp, user_energy, context.user_data.get(f'{player_key}_next_turn_synergy', {}),
            context, player_key, bot_mode, opponent_name
        )

        if result is None:
            if not bot_mode:
                await update.callback_query.answer("Not enough energy for this move!")
            return

        result_message, user_hp, opponent_hp, user_energy, damage, heal, energy_cost, energy_gain, synergy_effect = result

        # Clear the next turn synergy
        context.user_data[f'{player_key}_next_turn_synergy'] = {}

        # Define winner_name and last_move_details here
        winner_name = None
        last_move_details = f"Last move was {action}, dealing {damage} damage."

       
       # Check if the battle ends
        if opponent_hp <= 0 or user_hp <= 0:
            winner_id = user_id if opponent_hp <= 0 else opponent_id
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (winner_id, battle['id']))
            db.commit()
            winner_name = "Bot" if winner_id == 7283636452 else update.effective_user.first_name if bot_mode else "You"
            await send_message(update, f"{winner_name} {'have' if winner_name == 'You' else 'has'} won the battle!\n{last_move_details}")
        
        # End combat in the quest
            if zen_quest.quest_active.get(user_id, False):
                zen_quest.in_combat[user_id] = False
                if winner_id == user_id:
                    await zen_quest.progress_story(update, context, "victory in combat")
                else:
                    await zen_quest.end_quest(update, context, victory=False, reason="You have been defeated in combat.")
            return
        
        # Update the battle status
        cursor.execute(f"""
            UPDATE pvp_battles 
            SET {player_key}_hp = %s, {opponent_key}_hp = %s, current_turn = %s 
            WHERE id = %s
        """, (user_hp, opponent_hp, opponent_id, battle['id']))
        db.commit()

        # Update energy levels in context
        context.user_data[f'{player_key}_energy'] = user_energy
        context.user_data[f'{opponent_key}_energy'] = opponent_energy

        # Create and send the battle view
        battle_view = create_battle_view(
            "Bot" if bot_mode else update.effective_user.first_name,
            user_hp,
            user_energy,
            opponent_name,
            opponent_hp,
            opponent_energy
        )

        # Creating detailed move summary
        numeric_stats = f"Move: {action.capitalize()}, Effect: {synergy_effect or 'None'}, Numeric Stats: Damage: {damage}, Heal: {heal}, Energy Cost: {energy_cost}, Energy Gained: {energy_gain}, Synergy: {synergy_effect or 'None'}"

        # Send the result of the action along with the updated battle view
        try:
            await context.bot.send_message(
                chat_id=battle['group_id'], 
                text=f"{escape_markdown_v2(result_message)}\n\n{escape_markdown_v2(battle_view)}\n\n{escape_markdown_v2(numeric_stats)}",
                parse_mode='MarkdownV2'
            )
        except BadRequest as e:
            logger.error(f"Error sending battle update: {e}")
            await context.bot.send_message(
                chat_id=battle['group_id'],
                text="An error occurred while updating the battle. Please check /pvpstatus for the current state."
            )

        # Notify the next player
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
   

# Call this function at the start of a new battle
async def start_new_battle(update, context):
    reset_synergies(context)
    await update.message.reply_text("A new battle has begun! All synergies and effects have been reset.")

def reset_synergies(context):
    context.user_data['challenger_previous_move'] = None
    context.user_data['opponent_previous_move'] = None  # Track opponent's last move
    context.user_data['opponent_mind_trap'] = False
    context.user_data['focus_active'] = False
    context.user_data['energy_loss'] = 0
    context.user_data['energy_gain'] = 0
    context.user_data['challenger_energy'] = 50
    context.user_data['opponent_energy'] = 50

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
            - If your energy is low (below 20), prioritize "Focus" or "Defend" to recover energy.
            - Avoid attempting an action if you don't have enough energy to perform it.
            - Manage your energy carefully; don't allow it to drop too low unless you can deliver a finishing blow.
            - If you used "Focus" in the previous move, consider following up with "Strike" or "Zen Strike" for enhanced damage.
            - Use "Mind Trap" to weaken the opponent, particularly if they have high energy or if you want to set up a safer "Zen Strike."
            - Prioritize "Zen Strike" if the opponent's HP is low enough for a potential finishing blow.
            """

            # Generate AI response based on the prompt
            ai_response = await generate_response(prompt)

            # Extract action from AI response, prioritizing energy management
            if "zen strike" in ai_response.lower() and bot_energy >= 40:
                action = "zenstrike"
            elif "strike" in ai_response.lower() and bot_energy >= 12:
                action = "strike"
            elif "mind trap" in ai_response.lower() and bot_energy >= 20:
                action = "mindtrap"
            elif "focus" in ai_response.lower():
                action = "focus"
            else:
                action = "defend"

            logger.info(f"Bot chose action: {action} based on AI response: {ai_response}")

            # Execute the chosen move
            await execute_pvp_move(update, context, db, bot_mode=True, action=action)

        except Exception as e:
            logger.error(f"Error during bot move execution: {e}")
        finally:
            if db.is_connected():
                cursor.close()
                db.close()


async def surrender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = get_db_connection()
    if db:
        try:
            cursor = db.cursor(dictionary=True)
            
            # Fetch the active battle where the user is a challenger or opponent
            cursor.execute("""
                SELECT id, challenger_id, opponent_id FROM pvp_battles 
                WHERE (challenger_id = %s OR opponent_id = %s) AND status = 'in_progress'
            """, (user_id, user_id))
            battle_data = cursor.fetchone()

            # Ensure all results are consumed
            cursor.fetchall()

            if not battle_data:
                await update.message.reply_text("No active battles found to surrender.")
                return

            # Determine the winner (opponent wins if the user surrenders)
            winner_id = battle_data['opponent_id'] if user_id == battle_data['challenger_id'] else battle_data['challenger_id']
            
            # Update the battle status to 'completed' and set the winner
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (winner_id, battle_data['id']))
            db.commit()
            
            await update.message.reply_text("You have surrendered the battle. Your opponent is victorious.")
        
        except mysql.connector.Error as e:
            logger.error(f"Database error in surrender: {e}")
            await update.message.reply_text("An error occurred while surrendering. Please try again later.")
        
        finally:
            if db.is_connected():
                cursor.close()
                db.close()
    else:
        await update.message.reply_text("I'm sorry, I'm having trouble accessing my memory right now. Please try again later.")


def check_rate_limit(user_id):
    now = datetime.now()
    user_messages = rate_limit_dict[user_id]
    user_messages = [time for time in user_messages if now - time < timedelta(minutes=1)]
    rate_limit_dict[user_id] = user_messages
    return len(user_messages) < RATE_LIMIT

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=True)
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