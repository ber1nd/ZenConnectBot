import json
import os
import logging
import random
import asyncio
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error
from datetime import datetime
from collections import defaultdict
import math
import urllib.parse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI
from openai import OpenAIError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import aiohttp
from telegram.error import InvalidToken

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Database Connection

def get_db_connection():
    database_name = os.getenv("MYSQL_DATABASE")
    if not database_name:
        logger.error("Environment variable MYSQL_DATABASE is not set.")
        return None
    try:
        connection = mysql.connector.connect(
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            host=os.getenv("MYSQLHOST"),
            database=database_name,
            port=int(os.getenv("MYSQLPORT", 3306)),
            raise_on_warnings=True,
        )
        logger.info("Database connection established successfully.")
        return connection
    except mysql.connector.Error as err:
        logger.error(f"Database connection error: {err}")
        return None

# OpenAI Client Initialization

def get_openai_api_key():
    api_key = os.getenv("API_KEY")
    if not api_key:
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                cursor.execute("SELECT value FROM settings WHERE key = 'API_KEY'")
                result = cursor.fetchone()
                if result:
                    api_key = result['value']
            except mysql.connector.Error as e:
                logger.error(f"Error retrieving API key from database: {e}")
            finally:
                cursor.close()
                connection.close()
    return api_key

openai_api_key = get_openai_api_key()
if not openai_api_key:
    logger.error("API_KEY environment variable is not set. Please set it and restart the application.")
    raise ValueError("API_KEY is not set")

try:
    client = AsyncOpenAI(api_key=openai_api_key)
except Exception as e:
    logger.error(f"Error initializing OpenAI client: {e}")
    raise

# Initialize FastAPI app
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Rate limiting parameters
RATE_LIMIT = 10
RATE_TIME_WINDOW = 30
GROUP_RATE_LIMIT = 20
GROUP_RATE_TIME_WINDOW = 60
rate_limit_lock = asyncio.Lock()
chat_message_times = defaultdict(list)

# Character Classes and Basic Attributes

class Character:
    def __init__(self, name, hp, energy, abilities):
        self.name = name
        self.max_hp = hp
        self.current_hp = hp
        self.max_energy = energy
        self.current_energy = energy
        self.abilities = abilities
        self.wisdom = random.randint(8, 18)
        self.intelligence = random.randint(8, 18)
        self.strength = random.randint(8, 18)
        self.dexterity = random.randint(8, 18)
        self.constitution = random.randint(8, 18)
        self.charisma = random.randint(8, 18)
        self.level = 1
        self.xp = 0

    def level_up(self):
        self.level += 1
        self.max_hp += 10
        self.current_hp = self.max_hp
        self.max_energy += 5
        self.current_energy = self.max_energy
        self.strength += random.randint(1, 3)
        self.dexterity += random.randint(1, 3)
        self.constitution += random.randint(1, 3)
        logger.info(f"{self.name} leveled up to level {self.level}")

character_classes = {
    "Monk": Character("Monk", 100, 100, ["Meditate", "Chi Strike", "Healing Touch"]),
    "Samurai": Character("Samurai", 120, 80, ["Katana Slash", "Bushido Stance", "Honor Guard"]),
    "Shaman": Character("Shaman", 90, 110, ["Nature's Wrath", "Spirit Link", "Elemental Shield"]),
}

# ZenQuest Implementation
class ZenQuest:
    def __init__(self):
        self.quest_active = defaultdict(bool)
        self.characters = {}
        self.current_stage = defaultdict(int)
        self.total_stages = defaultdict(int)
        self.quest_goal = {}
        self.quest_state = {}
        self.player_karma = defaultdict(lambda: 100)
        self.group_quests = {}

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if self.quest_active.get(chat_id, False):
            await update.message.reply_text(
                "A quest is already active in this chat. Use /status to check progress or /interrupt to end the current quest."
            )
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    class_name, callback_data=f"class_{class_name.lower()}"
                )
                for class_name in character_classes.keys()
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Choose your character class to begin your Zen journey:", reply_markup=reply_markup
        )

    async def select_character_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        data_parts = query.data.split("_")
        if len(data_parts) < 2:
            await query.edit_message_text("Invalid class selection. Please try again.")
            return
        class_name = data_parts[1].capitalize()
        if class_name not in character_classes:
            await query.edit_message_text("Invalid class selection. Please choose a valid class.")
            return
        character = character_classes[class_name]
        self.characters[user_id] = character
        self.quest_active[chat_id] = True
        self.current_stage[chat_id] = 0
        self.total_stages[chat_id] = random.randint(10, 20)
        self.quest_state[chat_id] = "beginning"
        self.player_karma[user_id] = 100
        await query.edit_message_text(f"Your quest as a {class_name} begins! Good luck!")
        logger.info(f"Character class {class_name} selected for user {user_id}")

    async def progress_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        character = self.characters.get(user_id)
        if not character:
            await update.message.reply_text("You do not have an active character. Start a quest first!")
            return

        user_input = update.message.text.strip()
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100
        if progress < 33:
            self.quest_state[chat_id] = "beginning"
        elif progress < 66:
            self.quest_state[chat_id] = "middle"
        else:
            self.quest_state[chat_id] = "nearing_end"
        
        next_scene = await self.generate_next_scene(chat_id, user_input, character)
        await update.message.reply_text(next_scene)
        self.current_stage[chat_id] += 1
        if self.current_stage[chat_id] >= self.total_stages[chat_id]:
            await update.message.reply_text("Congratulations! You have completed your journey.")
            self.quest_active[chat_id] = False

    async def generate_next_scene(self, chat_id: int, user_input: str, character):
        prompt = f"""
        User action: "{user_input}"
        Character class: {character.name}
        Quest state: {self.quest_state[chat_id]}

        Generate the next scene of the Zen-themed D&D-style quest. Include a description of the situation and a challenge.
        """
        return await self.generate_response(prompt)

    async def generate_response(self, prompt):
        try:
            response = await client.acreate(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant for a Zen-themed D&D-style game."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=300,
                n=1,
                temperature=0.7,
            )
            return response.choices[0].message['content'].strip()
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {e}")
            return "An error occurred while generating the response. Please try again."

# Main function to set up and run the bot
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable is not set!")
        return

    try:
        application = Application.builder().token(token).build()
    except InvalidToken:
        logger.error("Invalid Telegram Bot Token. Please check your TELEGRAM_TOKEN environment variable.")
        return

    zen_quest = ZenQuest()
    application.add_handler(CommandHandler("start", zen_quest.start_quest))
    application.add_handler(CommandHandler("zenquest", zen_quest.start_quest))
    application.add_handler(CallbackQueryHandler(zen_quest.select_character_class, pattern="^class_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, zen_quest.progress_quest))

    application.run_polling()

if __name__ == "__main__":
    main()