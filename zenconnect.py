import os
import asyncio
import logging
import functools
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from datetime import datetime
import mysql.connector
from mysql.connector import Error
from collections import defaultdict

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

# PvP Functionality

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

    except Error as e:
        logger.error(f"Database error in start_pvp: {e}")
        await update.message.reply_text("An error occurred while starting the PvP battle. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()

# Function to generate PvP move buttons
def generate_pvp_move_buttons(user_id):
    keyboard = [
        [InlineKeyboardButton("Strike", callback_data=f"pvp_strike_{user_id}")],
        [InlineKeyboardButton("Defend", callback_data=f"pvp_defend_{user_id}")],
        [InlineKeyboardButton("Focus", callback_data=f"pvp_focus_{user_id}")],
        [InlineKeyboardButton("Zen Strike", callback_data=f"pvp_zenstrike_{user_id}")],
        [InlineKeyboardButton("Mind Trap", callback_data=f"pvp_mindtrap_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Function to accept PvP challenges
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

        if not battle:
            logger.info(f"No pending battles found for user: {user_id}")
            await update.message.reply_text("You have no pending PvP challenges.")
            return

        cursor.execute("""
            UPDATE pvp_battles 
            SET status = 'in_progress', current_turn = %s 
            WHERE id = %s
        """, (battle['challenger_id'], battle['id']))
        db.commit()

        await update.message.reply_text("You have accepted the challenge! The battle begins now.")
        
        # Notify the challenger
        await context.bot.send_message(chat_id=battle['challenger_id'], text="Your challenge has been accepted! The battle begins now.")
        
        # Send move buttons to the challenger
        await context.bot.send_message(
            chat_id=battle['challenger_id'],
            text="It's your turn! Choose your move:",
            reply_markup=generate_pvp_move_buttons(battle['challenger_id'])
        )

    except Error as e:
        logger.error(f"Database error in accept_pvp: {e}")
        await update.message.reply_text("An error occurred while accepting the PvP challenge. Please try again later.")
        if db.is_connected():
            db.rollback()
    finally:
        if db.is_connected():
            cursor.close()

# Function to handle PvP moves
@with_database_connection
async def execute_pvp_move(update: Update, context: ContextTypes.DEFAULT_TYPE, db, bot_mode=False, action=None):
    user_id = 7283636452 if bot_mode else update.effective_user.id
    energy_cost = 0
    energy_gain = 0

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

    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM pvp_battles 
            WHERE (challenger_id = %s OR opponent_id = %s) AND status = 'in_progress'
        """, (user_id, user_id))
        battle = cursor.fetchone()

        if not battle:
            await update.message.reply_text("You are not in an active battle.")
            return

        if battle['current_turn'] != user_id:
            await update.message.reply_text("It's not your turn.")
            return

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

        if action == "strike":
            energy_cost = 12
            if user_energy < energy_cost:
                await update.message.reply_text("Not enough energy to use Strike.")
                return
            damage = random.randint(12, 18)
            opponent_hp = max(0, opponent_hp - damage)
            result_message = f"You used Strike and dealt {damage} damage."

        elif action == "defend":
            energy_gain = 10
            heal = random.randint(15, 25)
            user_hp = min(100, user_hp + heal)
            result_message = f"You used Defend, healed {heal} HP, and gained 10 energy."

        elif action == "focus":
            energy_gain = random.randint(20, 30)
            result_message = f"You used Focus, gained {energy_gain} energy."

        elif action == "zenstrike":
            energy_cost = 40
            if user_energy < energy_cost:
                await update.message.reply_text("Not enough energy to use Zen Strike.")
                return
            damage = random.randint(25, 35)
            opponent_hp = max(0, opponent_hp - damage)
            result_message = f"You used Zen Strike and dealt {damage} damage."

        elif action == "mindtrap":
            energy_cost = 20
            if user_energy < energy_cost:
                await update.message.reply_text("Not enough energy to use Mind Trap.")
                return
            opponent_energy = max(0, opponent_energy - 15)
            result_message = f"You used Mind Trap and reduced your opponent's energy by 15."

        user_energy = max(0, min(100, user_energy - energy_cost + energy_gain))

        if opponent_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (user_id, battle['id']))
            db.commit()
            await update.message.reply_text("You have won the battle!")
            return
        elif user_hp <= 0:
            cursor.execute("UPDATE pvp_battles SET status = 'completed', winner_id = %s WHERE id = %s", (opponent_id, battle['id']))
            db.commit()
            await update.message.reply_text("You have been defeated.")
            return

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

        battle_view = f"Your HP: {user_hp} | Opponent HP: {opponent_hp}\nYour Energy: {user_energy} | Opponent Energy: {opponent_energy}"

        await context.bot.send_message(chat_id=battle['group_id'], text=f"{result_message}\n\n{battle_view}")

        if opponent_id != 7283636452:
            await context.bot.send_message(chat_id=opponent_id, text="Your turn! Choose your move:", reply_markup=generate_pvp_move_buttons(opponent_id))

    except Error as e:
        logger.error(f"Error in execute_pvp_move: {e}")
        await update.message.reply_text("An error occurred while executing the PvP move. Please try again later.")
    finally:
        if db.is_connected():
            cursor.close()

        if update.callback_query:
            await update.callback_query.answer()

# Function to handle messages
@with_database_connection
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, db):
    user_id = update.effective_user.id
    user_message = update.message.text
    chat_type = update.message.chat.type
    group_id = update.message.chat.id if chat_type == 'group' else None

    if chat_type == 'group' and not ('zen' in user_message.lower() or f'@{context.bot.username.lower()}' in user_message.lower()):
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text("Please wait a moment before sending another message. Zen teaches us the value of patience.")
        return

    rate_limit_dict[user_id].append(datetime.now())

    try:
        cursor = db.cursor()
        
        chat_type_db = 'private' if chat_type == 'private' else 'group'
        
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, chat_type)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            username = VALUES(username),
            first_name = VALUES(first_name),
            last_name = VALUES(last_name),
            chat_type = VALUES(chat_type)
        """, (user_id, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name, chat_type_db))

        if group_id:
            cursor.execute("""
                INSERT IGNORE INTO group_memberships (user_id, group_id)
                VALUES (%s, %s)
            """, (user_id, group_id))

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

def setup_handlers(application):
    application.add_handler(CommandHandler("startpvp", start_pvp))
    application.add_handler(CommandHandler("acceptpvp", accept_pvp))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (
        filters.ChatType.PRIVATE |
        (filters.ChatType.GROUPS & (
            filters.Regex(r'(?i)\bzen\b') |
            filters.Regex(r'@\w+')
        ))
    ), handle_message))

    application.add_handler(CallbackQueryHandler(execute_pvp_move, pattern="^pvp_"))
    application.add_error_handler(error_handler)

async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    application = Application.builder().token(token).build()

    setup_handlers(application)

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info("Zen Monk Bot is running. Press Ctrl+C to stop.")

    # Keep the script running
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    setup_database()
    asyncio.run(main())