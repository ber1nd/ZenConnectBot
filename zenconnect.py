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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn
import aiohttp

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Move this function before get_openai_api_key()
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


# Initialize OpenAI client
def get_openai_api_key():
    # First, try to get the API key from the environment variable
    api_key = os.getenv("API_KEY")
    
    # If not found in environment, try to get it from the database
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

from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=5)

# Initialize FastAPI app
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Rate limiting parameters
RATE_LIMIT = 10  # Increased from 5 to 10 messages
RATE_TIME_WINDOW = 30  # Reduced from 60 to 30 seconds
GROUP_RATE_LIMIT = 20  # Higher limit for group chats
GROUP_RATE_TIME_WINDOW = 60  # 1 minute window for group chats
rate_limit_lock = asyncio.Lock()
chat_message_times = defaultdict(list)

# OpenAI Moderation Endpoint
MODERATION_URL = "https://api.openai.com/v1/moderations"

# Mount the static files directory
# app.mount("/static", StaticFiles(directory="static"), name="static")

def setup_database():
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            # Create characters table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS characters (
                    user_id BIGINT PRIMARY KEY,
                    name VARCHAR(255),
                    class VARCHAR(255),
                    hp INT,
                    max_hp INT,
                    energy INT,
                    max_energy INT,
                    karma INT,
                    wisdom INT,
                    intelligence INT,
                    strength INT,
                    dexterity INT,
                    constitution INT,
                    charisma INT
                )
                """
            )
            connection.commit()
            logger.info("Database setup completed successfully.")
        except mysql.connector.Error as e:
            logger.error(f"Error setting up database: {e}")
        finally:
            cursor.close()
            connection.close()
    else:
        logger.error("Failed to connect to the database for setup.")


class Character:
    def __init__(self, name, hp, energy, abilities, strengths, weaknesses):
        self.name = name
        self.max_hp = hp
        self.current_hp = hp
        self.max_energy = energy
        self.current_energy = energy
        self.abilities = abilities
        self.strengths = strengths
        self.weaknesses = weaknesses
        # Add D&D-like attributes
        self.wisdom = random.randint(8, 18)
        self.intelligence = random.randint(8, 18)
        self.strength = random.randint(8, 18)
        self.dexterity = random.randint(8, 18)
        self.constitution = random.randint(8, 18)
        self.charisma = random.randint(8, 18)
        self.status_effects = []

    def roll_skill_check(self, attribute):
        return random.randint(1, 20) + (getattr(self, attribute) - 10) // 2

    def apply_status_effect(self, effect, duration):
        self.status_effects.append({"effect": effect, "duration": duration})

    def update_status_effects(self):
        self.status_effects = [
            effect for effect in self.status_effects if effect["duration"] > 0
        ]
        for effect in self.status_effects:
            effect["duration"] -= 1


class CombatSystem:
    def __init__(self):
        self.turn_order = []
        self.current_turn = 0

    def initialize_combat(self, players, opponents):
        all_combatants = players + opponents
        self.turn_order = sorted(
            all_combatants,
            key=lambda x: random.randint(1, 20) + (x.dexterity - 10) // 2,
            reverse=True,
        )
        self.current_turn = 0

    def next_turn(self):
        self.current_turn = (self.current_turn + 1) % len(self.turn_order)
        return self.turn_order[self.current_turn]

    def calculate_damage(self, attacker, defender, base_damage):
        crit_multiplier = 2 if random.random() < 0.05 else 1  # 5% crit chance
        damage = base_damage * crit_multiplier
        if isinstance(attacker, Character):
            damage += (attacker.strength - 10) // 2
        if isinstance(defender, Character):
            damage -= (defender.constitution - 10) // 4
        return max(1, math.floor(damage))  # Minimum 1 damage


class ZenQuest:
    def __init__(self):
        # Initialize all necessary dictionaries with default values
        self.quest_active = defaultdict(bool)
        self.characters = {}
        self.current_stage = defaultdict(int)
        self.total_stages = defaultdict(int)
        self.current_scene = {}
        self.in_combat = defaultdict(bool)
        self.quest_state = {}
        self.quest_goal = {}
        self.player_karma = defaultdict(lambda: 100)
        self.current_opponent = {}
        self.riddles = {}
        self.moral_dilemmas = {}
        self.unfeasible_actions = [
            "fly",
            "teleport",
            "time travel",
            "breathe underwater",
            "become invisible",
            "read minds",
            "shoot lasers",
            "transform",
            "resurrect",
            "conjure",
            "summon creatures",
            "control weather",
            "phase through walls",
        ]
        self.failure_actions = [
            "give up",
            "abandon quest",
            "betray",
            "surrender",
            "destroy sacred artifact",
            "harm innocent",
            "break vow",
            "ignore warning",
            "consume poison",
            "jump off cliff",
            "attack ally",
            "steal from temple",
        ]
        self.character_classes = {
            "Monk": Character(
                "Monk",
                100,
                100,
                ["Meditate", "Chi Strike", "Healing Touch", "Spirit Ward"],
                ["spiritual challenges", "endurance"],
                ["physical combat", "technology"],
            ),
            "Samurai": Character(
                "Samurai",
                120,
                80,
                ["Katana Slash", "Bushido Stance", "Focused Strike", "Honor Guard"],
                ["physical combat", "honor-based challenges"],
                ["spiritual challenges", "deception"],
            ),
            "Shaman": Character(
                "Shaman",
                90,
                110,
                ["Nature's Wrath", "Spirit Link", "Elemental Shield", "Ancestral Guidance"],
                ["nature-based challenges", "spiritual insight"],
                ["urban environments", "technology"],
            ),
        }
        self.min_stages = 10
        self.max_stages = 20
        self.group_quests = {}
        self.skill_check_difficulty = {"easy": 10, "medium": 15, "hard": 20}
        self.max_riddle_attempts = 3
        self.puzzles = {}
        self.combat_systems = {}

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id

        if self.quest_active.get(chat_id, False):
            await update.message.reply_text(
                "A quest is already active in this chat. Use /status to check progress or /interrupt to end the current quest."
            )
            return

        if update.effective_chat.type in ["group", "supergroup"]:
            await self.start_group_quest(update, context)
        else:
            keyboard = [
                [
                    InlineKeyboardButton(
                        class_name, callback_data=f"class_{class_name.lower()}"
                    )
                    for class_name in self.character_classes.keys()
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Choose your character class to begin your Zen journey:",
                reply_markup=reply_markup,
            )

    async def start_group_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.group_quests[chat_id] = {
            "players": {},
            "ready": False,
            "current_player": None,
        }
        await update.message.reply_text(
            "A group quest is starting! Each player should use /join to select their class. "
            "Use /start_journey when all players are ready."
        )

    async def join_group_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if chat_id not in self.group_quests:
            await update.message.reply_text(
                "There is no active group quest. Use /zenquest to start one."
            )
            return

        if user_id in self.group_quests[chat_id]["players"]:
            await update.message.reply_text("You have already joined the quest.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    class_name, callback_data=f"group_class_{class_name.lower()}"
                )
                for class_name in self.character_classes.keys()
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Choose your character class for the group quest:", reply_markup=reply_markup
        )

    async def select_group_character_class(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        data_parts = query.data.split("_")
        if len(data_parts) < 3:
            await query.edit_message_text("Invalid class selection. Please try again.")
            return
        class_name = data_parts[2].capitalize()

        if class_name not in self.character_classes:
            await query.edit_message_text("Invalid class selection. Please choose a valid class.")
            return

        self.group_quests[chat_id]["players"][user_id] = self.character_classes[class_name]
        await query.edit_message_text(f"You have joined the quest as a {class_name}.")

    async def start_group_journey(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id

        if chat_id not in self.group_quests or self.group_quests[chat_id]["ready"]:
            await update.message.reply_text("There is no group quest waiting to start.")
            return

        if len(self.group_quests[chat_id]["players"]) < 2:
            await update.message.reply_text("At least two players are needed to start the group quest.")
            return

        self.group_quests[chat_id]["ready"] = True
        self.quest_active[chat_id] = True
        self.current_stage[chat_id] = 0
        self.total_stages[chat_id] = random.randint(self.min_stages, self.max_stages)
        self.quest_state[chat_id] = "beginning"
        self.in_combat[chat_id] = False
        self.player_karma[chat_id] = 100

        self.quest_goal[chat_id] = await self.generate_group_quest_goal(chat_id)
        self.current_scene[chat_id] = await self.generate_group_initial_scene(chat_id)

        # Set the first player to act
        players = list(self.group_quests[chat_id]["players"].keys())
        self.group_quests[chat_id]["current_player"] = players[0]

        start_message = (
            f"Your group quest begins!\n\n"
            f"{self.quest_goal[chat_id]}\n\n"
            f"{self.current_scene[chat_id]}"
        )
        await update.message.reply_text(start_message)

    async def generate_group_quest_goal(self, chat_id: int):
        classes = [
            character.name for character in self.group_quests[chat_id]["players"].values()
        ]
        prompt = f"""
        Generate a quest goal for a group of {', '.join(classes)} in a Zen-themed adventure.
        The goal should be challenging, spiritual in nature, and relate to self-improvement and teamwork.
        Keep it concise, about 2-3 sentences.
        """
        return await self.generate_response(prompt)

    async def generate_group_initial_scene(self, chat_id: int):
        classes = [
            character.name for character in self.group_quests[chat_id]["players"].values()
        ]
        prompt = f"""
        Quest goal: {self.quest_goal[chat_id]}
        Group composition: {', '.join(classes)}

        Generate an initial scene for the group quest. Include:
        1. A brief description of the starting location
        2. An introduction to the quest's first challenge
        3. Three possible actions for the group

        Keep the response under 200 words.
        """
        return await self.generate_response(prompt)

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

        if class_name not in self.character_classes:
            await query.edit_message_text("Invalid class selection. Please choose a valid class.")
            return

        character = self.character_classes[class_name]
        self.characters[user_id] = character
        self.quest_active[chat_id] = True
        self.current_stage[chat_id] = 0
        self.total_stages[chat_id] = random.randint(self.min_stages, self.max_stages)
        self.quest_state[chat_id] = "beginning"
        self.in_combat[chat_id] = False
        self.player_karma[user_id] = 100

        # Save character to database
        self.save_character_to_db(user_id, character)

        self.quest_goal[chat_id] = await self.generate_quest_goal(class_name)
        self.current_scene[chat_id] = await self.generate_initial_scene(
            self.quest_goal[chat_id], class_name
        )

        start_message = (
            f"Your quest as a {class_name} begins!\n\n"
            f"{self.quest_goal[chat_id]}\n\n"
            f"{self.current_scene[chat_id]}"
        )
        await query.edit_message_text(start_message)
        logger.info(f"Character class {class_name} selected for user {user_id}")

    def save_character_to_db(self, user_id, character):
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                query = """
                INSERT INTO characters (user_id, name, class, hp, max_hp, energy, max_energy, karma, 
                                        wisdom, intelligence, strength, dexterity, constitution, charisma)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                name = VALUES(name), class = VALUES(class), hp = VALUES(hp), max_hp = VALUES(max_hp),
                energy = VALUES(energy), max_energy = VALUES(max_energy), karma = VALUES(karma),
                wisdom = VALUES(wisdom), intelligence = VALUES(intelligence), strength = VALUES(strength),
                dexterity = VALUES(dexterity), constitution = VALUES(constitution), charisma = VALUES(charisma)
                """
                values = (user_id, character.name, character.__class__.__name__, character.current_hp, 
                          character.max_hp, character.current_energy, character.max_energy, 
                          self.player_karma.get(user_id, 100), character.wisdom, character.intelligence, 
                          character.strength, character.dexterity, character.constitution, character.charisma)
                cursor.execute(query, values)
                connection.commit()
                logger.info(f"Character saved to database for user {user_id}")
            except Error as e:
                logger.error(f"Error saving character to database: {e}")
            finally:
                cursor.close()
                connection.close()
        else:
            logger.error("Failed to connect to the database when saving character")

    def get_character_stats(self, user_id):
        logger.info(f"Attempting to get character stats for user {user_id}")
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                query = "SELECT * FROM characters WHERE user_id = %s"
                cursor.execute(query, (user_id,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"Character stats retrieved from database for user {user_id}")
                    return {
                        "name": result['name'],
                        "class": result['class'],
                        "hp": result['hp'],
                        "max_hp": result['max_hp'],
                        "energy": result['energy'],
                        "max_energy": result['max_energy'],
                        "karma": result['karma'],
                        "abilities": [],  # You might want to store abilities separately
                        "wisdom": result['wisdom'],
                        "intelligence": result['intelligence'],
                        "strength": result['strength'],
                        "dexterity": result['dexterity'],
                        "constitution": result['constitution'],
                        "charisma": result['charisma'],
                    }
                else:
                    logger.info(f"No character found in database for user {user_id}")
            except Error as e:
                logger.error(f"Error retrieving character from database: {e}")
            finally:
                cursor.close()
                connection.close()
        else:
            logger.error("Failed to connect to the database when retrieving character stats")
        
        # If no character is found in the database, check the in-memory storage
        if user_id in self.characters:
            character = self.characters[user_id]
            logger.info(f"Character found in memory for user {user_id}")
            return {
                "name": character.name,
                "class": character.__class__.__name__,
                "hp": character.current_hp,
                "max_hp": character.max_hp,
                "energy": character.current_energy,
                "max_energy": character.max_energy,
                "karma": self.player_karma.get(user_id, 100),
                "abilities": character.abilities,
                "wisdom": character.wisdom,
                "intelligence": character.intelligence,
                "strength": character.strength,
                "dexterity": character.dexterity,
                "constitution": character.constitution,
                "charisma": character.charisma,
            }
        
        # If no character is found, return default data
        logger.info(f"No character found for user {user_id}")
        return {
            "name": "No Active Character",
            "class": "None",
            "hp": 0,
            "max_hp": 0,
            "energy": 0,
            "max_energy": 0,
            "karma": 0,
            "abilities": [],
            "wisdom": 0,
            "intelligence": 0,
            "strength": 0,
            "dexterity": 0,
            "constitution": 0,
            "charisma": 0,
        }

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        is_group_chat = update.effective_chat.type in ["group", "supergroup"]

        # Rate limiting using asyncio.Lock
        async with rate_limit_lock:
            current_time = datetime.now()
            chat_times = chat_message_times[chat_id]
            if is_group_chat:
                chat_times = [t for t in chat_times if (current_time - t).total_seconds() < GROUP_RATE_TIME_WINDOW]
                limit = GROUP_RATE_LIMIT
                window = GROUP_RATE_TIME_WINDOW
            else:
                chat_times = [t for t in chat_times if (current_time - t).total_seconds() < RATE_TIME_WINDOW]
                limit = RATE_LIMIT
                window = RATE_TIME_WINDOW
            
            chat_times.append(current_time)
            chat_message_times[chat_id] = chat_times
            if len(chat_times) > limit:
                await update.message.reply_text(
                    f"This chat is very active. Please wait a moment before sending more messages. (Limit: {limit} messages per {window} seconds)"
                )
                return

        if update.message and update.message.text:
            user_input = update.message.text.strip()
        else:
            return  # Non-text message received

        if is_group_chat:
            if not self.group_quests.get(chat_id, {}).get("ready", False):
                return
            if self.group_quests[chat_id]["current_player"] != user_id:
                await update.message.reply_text("It's not your turn to act.")
                return

        if not self.quest_active.get(chat_id, False):
            if update.effective_chat.type == "private":
                await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
            return

        if self.in_combat.get(chat_id, False):
            await update.message.reply_text("You're in combat! Use the combat options provided.")
            return

        if chat_id in self.riddles and self.riddles[chat_id]["active"]:
            await self.handle_riddle_input(update, context, user_input)
            return

        if any(
            word in user_input.lower()
            for word in ["hurt myself", "self-harm", "suicide", "kill myself", "cut"]
        ):
            await self.handle_self_harm(update, context, user_input)
            return

        # Check for special commands
        if user_input.startswith("/"):
            command = user_input[1:].split()[0]
            if command == "meditate":
                await self.meditate(update, context)
            elif command == "status":
                await self.get_quest_status(update, context)
            elif command == "interrupt":
                await self.interrupt_quest(update, context)
            elif command == "hint":
                await self.handle_hint(update, context)
            else:
                await update.message.reply_text(
                    "Unknown command. Available commands: /meditate, /status, /interrupt, /hint"
                )
            return

        # Process action
        action_result = await self.process_action(chat_id, user_input)

        # Handle special events without sending additional messages
        if "[COMBAT_START]" in action_result:
            clean_result = action_result.replace("[COMBAT_START]", "").strip()
            await self.send_message(update, clean_result)
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in action_result:
            clean_result = action_result.replace("[RIDDLE_START]", "").strip()
            await self.send_message(update, clean_result)
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in action_result:
            clean_result = action_result.replace("[QUEST_COMPLETE]", "").strip()
            await self.send_message(update, clean_result)
            await self.end_quest(
                update, context, victory=True, reason="You have completed your journey!"
            )
        elif "[QUEST_FAIL]" in action_result:
            clean_result = action_result.replace("[QUEST_FAIL]", "").strip()
            await self.send_message(update, clean_result)
            await self.end_quest(
                update, context, victory=False, reason="Your quest has come to an unfortunate end."
            )
        elif "[MORAL_CHOICE]" in action_result:
            clean_result = action_result.replace("[MORAL_CHOICE]", "").strip()
            await self.send_message(update, clean_result)
            await self.present_moral_choice(update, context)
        else:
            await self.send_message(update, action_result)

        # Update quest state
        self.current_scene[chat_id] = action_result
        self.current_stage[chat_id] += 1
        await self.update_quest_state(chat_id)

    async def send_message(self, update: Update, text: str, reply_markup=None):
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def process_action(self, chat_id: int, user_input: str):
        if chat_id in self.group_quests:
            characters = list(self.group_quests[chat_id]["players"].values())
            current_player = self.group_quests[chat_id]["current_player"]
            current_character = self.group_quests[chat_id]["players"][current_player]
        else:
            characters = [self.characters[chat_id]]
            current_character = characters[0]

        current_scene = self.current_scene[chat_id]
        quest_state = self.quest_state[chat_id]
        karma = self.player_karma[chat_id]

        if self.is_action_unfeasible(user_input):
            return "That action is not possible in this realm. Please choose a different path."
        elif self.is_action_failure(user_input):
            return "[QUEST_FAIL]: Your choice leads to an unfortunate end."

        prompt = f"""
        Current scene: {current_scene}
        Character class: {current_character.name}
        Quest state: {quest_state}
        Player karma: {karma}
        Player action: "{user_input}"

        Generate a concise result (4-5 sentences) for the player's action. Include:
        1. The immediate outcome of the action
        2. Any changes to the environment or situation
        3. A new challenge, opportunity, or decision point
        4. A subtle Zen teaching or insight related to the action and its consequences
        5. Three numbered options for the player's next action

        If applicable, include one of the following tags at the end of the response:
        [COMBAT_START], [RIDDLE_START], [QUEST_COMPLETE], [QUEST_FAIL], or [MORAL_CHOICE]

        Keep the entire response under 200 words.
        """
        action_result = await self.generate_response(prompt)

        # Update karma based on the action
        karma_change = await self.evaluate_karma_change(user_input, action_result)
        self.player_karma[chat_id] = max(0, min(100, self.player_karma[chat_id] + karma_change))

        return action_result

    async def evaluate_karma_change(self, user_input: str, action_result: str):
        prompt = f"""
        Player action: "{user_input}"
        Action result: "{action_result}"

        Evaluate the karmic impact of this action and its result. Provide a karma change value between -10 and 10. Return only the numeric value.
        """
        karma_change_str = await self.generate_response(prompt)
        try:
            return int(float(karma_change_str))
        except ValueError:
            logger.error(f"Invalid karma change value: {karma_change_str}")
            return 0

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        if chat_id in self.characters:
            character = self.characters[chat_id]
        else:
            await self.send_message(update, "An error occurred: Character not found.")
            return

        next_scene = await self.generate_next_scene(chat_id, user_input)
        self.current_scene[chat_id] = next_scene

        # Send the scene and handle special events in one step
        if "[COMBAT_START]" in next_scene:
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in next_scene:
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in next_scene:
            await self.end_quest(
                update, context, victory=True, reason="You have completed your journey!"
            )
        elif "[QUEST_FAIL]" in next_scene:
            await self.end_quest(
                update, context, victory=False, reason="Your quest has come to an unfortunate end."
            )
        else:
            # Remove the tag if present and send the message
            clean_scene = (
                next_scene.replace("[COMBAT_START]", "")
                .replace("[RIDDLE_START]", "")
                .replace("[QUEST_COMPLETE]", "")
                .replace("[QUEST_FAIL]", "")
                .strip()
            )
            await self.send_message(update, clean_scene)
            self.current_stage[chat_id] += 1

        # Update quest state and check for quest completion
        await self.update_quest_state(chat_id)
        if self.current_stage[chat_id] >= self.total_stages[chat_id]:
            await self.end_quest(
                update, context, victory=True, reason="You have reached the end of your journey!"
            )

        # Update karma and check for quest failure due to low karma
        self.player_karma[chat_id] = max(
            0, min(100, self.player_karma[chat_id] + random.randint(-3, 3))
        )
        if character.current_hp <= 0:
            await self.end_quest(
                update, context, victory=False, reason="Your life force has been depleted."
            )
        elif self.player_karma[chat_id] <= 0:
            await self.end_quest(
                update, context, victory=False, reason="Your karma has fallen too low."
            )

    async def generate_next_scene(self, chat_id: int, user_input: str):
        character = self.characters[chat_id]
        player_karma = self.player_karma[chat_id]
        current_stage = self.current_stage[chat_id]
        total_stages = self.total_stages[chat_id]
        progress = (current_stage / max(1, total_stages)) * 100

        event_type = random.choices(
            [
                "normal",
                "challenge",
                "reward",
                "meditation",
                "npc_encounter",
                "moral_dilemma",
                "spiritual_trial",
                "natural_obstacle",
                "mystical_phenomenon",
                "combat",
                "riddle",
                "puzzle",
            ],
            weights=[15, 15, 10, 5, 10, 10, 5, 5, 5, 10, 5, 5],
            k=1,
        )[0]

        if event_type == "puzzle":
            return await self.generate_puzzle(chat_id)
        elif event_type == "moral_dilemma":
            return await self.generate_moral_dilemma(chat_id)

        prompt = f"""
        Previous scene: {self.current_scene[chat_id]}
        User's action: "{user_input}"
        Character Class: {character.name}
        Character strengths: {', '.join(character.strengths)}
        Character weaknesses: {', '.join(character.weaknesses)}
        Current quest state: {self.quest_state[chat_id]}
        Quest goal: {self.quest_goal[chat_id]}
        Player karma: {player_karma}
        Current stage: {current_stage}
        Total stages: {total_stages}
        Progress: {progress:.2f}%
        Event type: {event_type}

        Generate the next engaging and concise scene of the Zen-themed quest, incorporating elements that contribute to a cohesive storyline. Include:
        1. A brief description of the new environment or situation (1-2 sentences).
        2. The outcome of the user's previous action and its impact on the quest (1 sentence).
        3. A new challenge or decision that relates to the overarching quest goal.
        4. A Zen teaching or insight that offers depth to the narrative.
        5. Three numbered options for the player's next action.

        If applicable, include one of the following tags at the end of the response:
        [COMBAT_START], [RIDDLE_START], [QUEST_COMPLETE], or [QUEST_FAIL]

        Ensure the scene advances the quest toward its conclusion, especially if progress is over 90%.
        Keep the entire response under 200 words.
        """
        next_scene = await self.generate_response(prompt)
        return next_scene

    async def generate_quest_goal(self, class_name):
        prompt = f"""
        Generate a quest goal for a {class_name} in a Zen-themed adventure.
        The goal should be challenging, spiritual in nature, and relate to self-improvement.
        Keep it concise, about 2-3 sentences.
        """
        return await self.generate_response(prompt)

    async def generate_initial_scene(self, quest_goal, class_name):
        prompt = f"""
        Quest goal: {quest_goal}
        Character class: {class_name}

        Generate an initial scene for the quest. Include:
        1. A brief description of the starting location
        2. An introduction to the quest's first challenge
        3. Three possible actions for the player

        Keep the response under 150 words.
        """
        return await self.generate_response(prompt)

    async def handle_riddle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        riddle = self.riddles[chat_id]

        riddle["attempts"] += 1

        if user_input.lower() == riddle["answer"].lower():
            success_message = await self.generate_response(
                f"Generate a brief success message for solving the riddle: {riddle['riddle']}. "
                f"Include a small reward or positive outcome for the {self.characters[chat_id].name}. "
                f"Keep it under 100 words."
            )
            await self.send_message(update, success_message)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "solved riddle")
        elif riddle["attempts"] >= self.max_riddle_attempts:
            failure_consequence = await self.generate_riddle_failure_consequence(chat_id)
            await self.send_message(update, failure_consequence)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "failed riddle")
        else:
            remaining_attempts = self.max_riddle_attempts - riddle["attempts"]
            await self.send_message(
                update,
                f"That's not correct. You have {remaining_attempts} attempts remaining. Use /hint for a clue.",
            )

    async def handle_self_harm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        support_message = (
            "I'm sorry to hear that you're feeling this way. Please consider reaching out to a mental health professional or someone you trust for support."
        )
        await self.send_message(update, support_message)

    async def meditate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You must be on a quest to meditate.")
            return

        character = self.characters[chat_id]
        meditation_result = await self.generate_response(
            f"Generate a brief meditation outcome for a {character.name} in their current quest state. "
            "Include a small health and energy boost, and a Zen insight. Keep it under 100 words."
        )

        character.current_hp = min(character.max_hp, character.current_hp + 10)
        character.current_energy = min(character.max_energy, character.current_energy + 10)

        await self.send_message(update, meditation_result)

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        character = self.characters[chat_id]
        progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100

        status_message = (
            f"Quest Progress: {progress:.1f}%\n"
            f"Character: {character.name}\n"
            f"HP: {character.current_hp}/{character.max_hp}\n"
            f"Energy: {character.current_energy}/{character.max_energy}\n"
            f"Karma: {self.player_karma[chat_id]}\n"
            f"Current State: {self.quest_state[chat_id].capitalize()}\n"
            f"Goal: {self.quest_goal[chat_id]}"
        )
        await self.send_message(update, status_message)

    async def interrupt_quest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if not zen_quest.quest_active.get(chat_id, False):
            await update.message.reply_text("You are not currently on a quest.")
            return

        if chat_id in zen_quest.group_quests:
            # Group quest
            if user_id not in zen_quest.group_quests[chat_id]['players']:
                await update.message.reply_text("You are not part of this group quest.")
                return
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by a group member.")
        else:
            # Individual quest
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by user.")

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            return

        if chat_id in self.group_quests:
            # Group quest
            players = self.group_quests[chat_id]['players']
            end_message = (
                f"The group quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Participants: {', '.join([p.name for p in players.values()])}\n"
            )
            for user_id, character in players.items():
                end_message += (
                    f"\n{character.name}:\n"
                    f"HP: {character.current_hp}/{character.max_hp}\n"
                    f"Energy: {character.current_energy}/{character.max_energy}\n"
                )
            
            # Reset group quest data
            self.group_quests.pop(chat_id, None)
        else:
            # Individual quest
            character = self.characters[chat_id]
            progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100
            end_message = (
                f"Your quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Progress: {progress:.1f}%\n"
                f"Final Karma: {self.player_karma[chat_id]}\n"
                f"Character: {character.name}\n"
                f"HP: {character.current_hp}/{character.max_hp}\n"
                f"Energy: {character.current_energy}/{character.max_energy}\n"
            )

        await self.send_message(update, end_message)

        # Reset quest data
        self.quest_active[chat_id] = False
        self.characters.pop(chat_id, None)
        self.current_stage.pop(chat_id, None)
        self.total_stages.pop(chat_id, None)
        self.current_scene.pop(chat_id, None)
        self.in_combat.pop(chat_id, None)
        self.quest_state.pop(chat_id, None)
        self.quest_goal.pop(chat_id, None)
        self.player_karma.pop(chat_id, None)
        self.current_opponent.pop(chat_id, None)
        self.riddles.pop(chat_id, None)
        self.moral_dilemmas.pop(chat_id, None)
        self.combat_systems.pop(chat_id, None)

    async def present_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        prompt = (
            "Generate a moral dilemma for the player's Zen quest. "
            "Present three choices, each with potential consequences. "
            "Format the choices as numbered options (1, 2, 3). "
            "Keep the description and choices under 200 words total."
        )
        dilemma = await self.generate_response(prompt)
        self.moral_dilemmas[chat_id] = {"active": True, "dilemma": dilemma}
        await self.send_message(update, dilemma)

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id

        if chat_id in self.group_quests:
            players = list(self.group_quests[chat_id]["players"].values())
        else:
            players = [self.characters[chat_id]]

        opponent_prompt = f"Generate a challenging opponent or group of opponents for {', '.join([p.name for p in players])} in a Zen-themed quest. Include name(s), brief description(s), HP, and two unique abilities for each. Format as JSON."
        opponent_json = await self.generate_response(opponent_prompt)

        try:
            opponents_data = json.loads(opponent_json)
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing opponent JSON: {e}")
            await self.send_message(
                update,
                "An error occurred while generating opponents for combat. Please try again later.",
            )
            return

        opponents = []
        if isinstance(opponents_data, list):
            for opp_data in opponents_data:
                opponent = Character(
                    opp_data.get("name", "Unknown Opponent"),
                    opp_data.get("HP", 50),
                    0,  # Energy not used for opponents here
                    opp_data.get("abilities", []),
                    [],
                    [],
                )
                opponent.description = opp_data.get("description", "")
                opponents.append(opponent)
        else:
            opponent = Character(
                opponents_data.get("name", "Unknown Opponent"),
                opponents_data.get("HP", 50),
                0,
                opponents_data.get("abilities", []),
                [],
                [],
            )
            opponent.description = opponents_data.get("description", "")
            opponents.append(opponent)

        self.current_opponent[chat_id] = opponents
        self.in_combat[chat_id] = True

        # Initialize a combat system for this chat
        combat_system = CombatSystem()
        combat_system.initialize_combat(players, opponents)
        self.combat_systems[chat_id] = combat_system

        combat_start_message = (
            f"You encounter {', '.join([o.name for o in opponents])}!\n"
            f"{' '.join([o.description for o in opponents if hasattr(o, 'description')])}\n"
            f"Prepare for combat!\n"
            f"Combat order: {', '.join([c.name for c in combat_system.turn_order])}\n"
        )

        await self.send_message(update, combat_start_message)
        await self.present_combat_options(update, context)

    async def present_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        combat_system = self.combat_systems.get(chat_id)
        if not combat_system:
            await self.send_message(update, "Combat system not initialized.")
            return

        current_character = combat_system.turn_order[combat_system.current_turn]

        if isinstance(current_character, Character):  # Player's turn
            keyboard = [
                [InlineKeyboardButton("Basic Attack", callback_data="combat_basic_attack")],
                [InlineKeyboardButton("Defend", callback_data="combat_defend")],
                [
                    InlineKeyboardButton(
                        f"Use {current_character.abilities[0]}",
                        callback_data=f"combat_ability_{current_character.abilities[0]}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Use {current_character.abilities[1]}",
                        callback_data=f"combat_ability_{current_character.abilities[1]}",
                    )
                ],
                [InlineKeyboardButton("Attempt to flee", callback_data="combat_flee")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.send_message(
                update,
                f"{current_character.name}'s turn!\nChoose your action:",
                reply_markup=reply_markup,
            )
        else:  # Opponent's turn
            await self.handle_opponent_turn(update, context)

    async def handle_combat_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        data_parts = query.data.split("_", 1)
        if len(data_parts) < 2:
            await query.edit_message_text("Invalid combat action.")
            return
        action = data_parts[1]  # Remove 'combat_' prefix

        combat_system = self.combat_systems.get(chat_id)
        if not combat_system:
            await query.edit_message_text("Combat system not initialized.")
            return

        current_character = combat_system.turn_order[combat_system.current_turn]
        opponents = self.current_opponent[chat_id]

        action_result = await self.resolve_combat_action(current_character, opponents, action)
        await query.edit_message_text(action_result)

        if "combat ended" in action_result.lower():
            self.in_combat[chat_id] = False
            self.current_opponent.pop(chat_id, None)
            self.combat_systems.pop(chat_id, None)
            await self.progress_story(update, context, "combat ended")
        else:
            combat_system.next_turn()
            await self.present_combat_options(update, context)

    async def handle_opponent_turn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        combat_system = self.combat_systems.get(chat_id)
        if not combat_system:
            await self.send_message(update, "Combat system not initialized.")
            return

        opponent = combat_system.turn_order[combat_system.current_turn]
        if chat_id in self.group_quests:
            players = list(self.group_quests[chat_id]["players"].values())
        else:
            players = [self.characters[chat_id]]

        action_prompt = f"Generate a strategic combat action for {opponent.name} against {', '.join([p.name for p in players])}. Consider the opponent's abilities and the characters' strengths/weaknesses. Keep it under 50 words."
        action = await self.generate_response(action_prompt)

        result = await self.resolve_combat_action(opponent, players, action)
        await self.send_message(update, f"{opponent.name}'s turn:\n{result}")

        if "combat ended" in result.lower():
            self.in_combat[chat_id] = False
            self.current_opponent.pop(chat_id, None)
            self.combat_systems.pop(chat_id, None)
            await self.progress_story(update, context, "combat ended")
        else:
            combat_system.next_turn()
            await self.present_combat_options(update, context)

    async def resolve_combat_action(self, attacker, defenders, action):
        if isinstance(attacker, Character):
            attacker_name = attacker.name
            attacker_abilities = attacker.abilities
        else:
            attacker_name = attacker.name
            attacker_abilities = attacker.abilities

        defenders_info = ", ".join([d.name for d in defenders])

        prompt = f"""
        Attacker: {attacker_name}
        Attacker's abilities: {', '.join(attacker_abilities)}
        Defenders: {defenders_info}
        Action: {action}

        Resolve the combat action, considering the attacker's abilities and the defenders' strengths/weaknesses.
        Include any damage dealt, status effects applied, or other relevant outcomes.
        If the combat ends, clearly state whether the attacker or defenders won.
        Keep the response under 100 words.
        """
        result = await self.generate_response(prompt)

        # Update HP based on the result (simplified for now)
        damage = random.randint(5, 15)
        if isinstance(attacker, Character):
            for defender in defenders:
                defender.current_hp = max(0, defender.current_hp - damage)
        else:
            attacker.current_hp = max(0, attacker.current_hp - damage)

        # Check for combat end
        if all(defender.current_hp <= 0 for defender in defenders):
            result += "\nCombat ended. The attacker is victorious."
        elif attacker.current_hp <= 0:
            result += "\nCombat ended. The defenders are victorious."

        return result

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]
        prompt = f"Generate a Zen-themed riddle related to {character.name}'s quest. Include the riddle, its answer, and three hints of increasing clarity. Format as JSON."
        riddle_json = await self.generate_response(prompt)

        try:
            riddle_data = json.loads(riddle_json)
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing riddle JSON: {e}")
            await self.send_message(
                update,
                "An error occurred while generating the riddle. Please try again later.",
            )
            return

        self.riddles[chat_id] = {
            "active": True,
            "riddle": riddle_data["riddle"],
            "answer": riddle_data["answer"],
            "hints": riddle_data["hints"],
            "attempts": 0,
            "hints_used": 0,
        }

        riddle_message = (
            f"As you progress on your journey, you encounter a mystical challenge:\n\n"
            f"{riddle_data['riddle']}\n\n"
            f"Solve this riddle to continue your quest. You have {self.max_riddle_attempts} attempts. "
            f"Use /hint for a clue (up to 3 times)."
        )
        await self.send_message(update, riddle_message)

    async def handle_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id in self.riddles and self.riddles[chat_id]["active"]:
            riddle = self.riddles[chat_id]
            if riddle["hints_used"] < len(riddle["hints"]):
                hint = riddle["hints"][riddle["hints_used"]]
                riddle["hints_used"] += 1
                await self.send_message(update, f"Hint: {hint}")
            else:
                await self.send_message(
                    update,
                    "You've used all available hints. Try to solve the riddle or face the consequences of failure.",
                )
        else:
            await self.send_message(update, "There is no active riddle to hint for.")

    async def generate_riddle_failure_consequence(self, chat_id: int):
        character = self.characters[chat_id]
        consequence_type = random.choice(["combat", "karma_loss", "hp_loss"])

        if consequence_type == "combat":
            self.in_combat[chat_id] = True
            opponent_prompt = f"Generate a challenging opponent for a {character.name} as a consequence of failing to solve a riddle. Include name, brief description, HP, and two unique abilities. Format as JSON."
            opponent_json = await self.generate_response(opponent_prompt)
            try:
                opponent_data = json.loads(opponent_json)
                opponent = Character(
                    opponent_data.get("name", "Mystical Adversary"),
                    opponent_data.get("HP", 50),
                    0,
                    opponent_data.get("abilities", []),
                    [],
                    [],
                )
                opponent.description = opponent_data.get("description", "")
                self.current_opponent[chat_id] = [opponent]
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing opponent JSON: {e}")
                opponent = Character(
                    "Mystical Adversary", 50, 0, ["Unknown Ability"], [], []
                )
                self.current_opponent[chat_id] = [opponent]
            return (
                f"Your failure to solve the riddle has summoned {opponent.name}! Prepare for combat!"
            )
        elif consequence_type == "karma_loss":
            karma_loss = random.randint(10, 20)
            self.player_karma[chat_id] = max(0, self.player_karma[chat_id] - karma_loss)
            return f"Your failure to solve the riddle has disturbed the cosmic balance. You lose {karma_loss} karma points."
        else:  # hp_loss
            hp_loss = random.randint(10, 20)
            character.current_hp = max(0, character.current_hp - hp_loss)
            return (
                f"The mystical energies of the unsolved riddle lash out at you. You lose {hp_loss} HP."
            )

    def is_action_unfeasible(self, action):
        return any(word in action.lower() for word in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(word in action.lower() for word in self.failure_actions)

    async def generate_response(self, prompt, max_tokens=500):
        try:
            messages = [
                {
                    "role": "system",
                    "content": "You are a wise Zen master guiding a quest. Avoid any disallowed content and maintain appropriate language. Provide challenging moral dilemmas and opportunities for growth.",
                },
                {"role": "user", "content": prompt},
            ]
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )
            content = response.choices[0].message.content.strip()

            # Perform content moderation
            if await self.is_disallowed_content(content):
                logger.warning("Disallowed content detected in the generated response.")
                return "I'm sorry, but I can't provide a response to that request."
            return content
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

    async def is_disallowed_content(self, content):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('API_KEY')}",
        }
        data = {"input": content}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MODERATION_URL, headers=headers, json=data
                ) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"Moderation API request failed with status {resp.status}"
                        )
                        return False  # Default to not flagged
                    result = await resp.json()
                    flagged = result.get("results", [{}])[0].get("flagged", False)
                    return flagged
        except Exception as e:
            logger.error(f"Error during content moderation: {e}")
            return False  # Default to not flagged

    async def update_quest_state(self, chat_id: int):
        progress = self.current_stage[chat_id] / max(1, self.total_stages[chat_id])
        if progress < 0.33:
            self.quest_state[chat_id] = "beginning"
        elif progress < 0.66:
            self.quest_state[chat_id] = "middle"
        else:
            self.quest_state[chat_id] = "end"

        if chat_id in self.group_quests:
            # Rotate to the next player
            players = list(self.group_quests[chat_id]['players'].values())
            current_player_index = self.group_quests[chat_id]['current_player_index']
            next_player_index = (current_player_index + 1) % len(players)

    async def handle_riddle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        riddle = self.riddles[chat_id]

        riddle["attempts"] += 1

        if user_input.lower() == riddle["answer"].lower():
            success_message = await self.generate_response(
                f"Generate a brief success message for solving the riddle: {riddle['riddle']}. "
                f"Include a small reward or positive outcome for the {self.characters[chat_id].name}. "
                f"Keep it under 100 words."
            )
            await self.send_message(update, success_message)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "solved riddle")
        elif riddle["attempts"] >= self.max_riddle_attempts:
            failure_consequence = await self.generate_riddle_failure_consequence(chat_id)
            await self.send_message(update, failure_consequence)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "failed riddle")
        else:
            remaining_attempts = self.max_riddle_attempts - riddle["attempts"]
            await self.send_message(
                update,
                f"That's not correct. You have {remaining_attempts} attempts remaining. Use /hint for a clue.",
            )

    async def handle_self_harm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        support_message = (
            "I'm sorry to hear that you're feeling this way. Please consider reaching out to a mental health professional or someone you trust for support."
        )
        await self.send_message(update, support_message)

    async def meditate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You must be on a quest to meditate.")
            return

        character = self.characters[chat_id]
        meditation_result = await self.generate_response(
            f"Generate a brief meditation outcome for a {character.name} in their current quest state. "
            "Include a small health and energy boost, and a Zen insight. Keep it under 100 words."
        )

        character.current_hp = min(character.max_hp, character.current_hp + 10)
        character.current_energy = min(character.max_energy, character.current_energy + 10)

        await self.send_message(update, meditation_result)

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        character = self.characters[chat_id]
        progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100

        status_message = (
            f"Quest Progress: {progress:.1f}%\n"
            f"Character: {character.name}\n"
            f"HP: {character.current_hp}/{character.max_hp}\n"
            f"Energy: {character.current_energy}/{character.max_energy}\n"
            f"Karma: {self.player_karma[chat_id]}\n"
            f"Current State: {self.quest_state[chat_id].capitalize()}\n"
            f"Goal: {self.quest_goal[chat_id]}"
        )
        await self.send_message(update, status_message)

    async def interrupt_quest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if not zen_quest.quest_active.get(chat_id, False):
            await update.message.reply_text("You are not currently on a quest.")
            return

        if chat_id in zen_quest.group_quests:
            # Group quest
            if user_id not in zen_quest.group_quests[chat_id]['players']:
                await update.message.reply_text("You are not part of this group quest.")
                return
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by a group member.")
        else:
            # Individual quest
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by user.")

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            return

        if chat_id in self.group_quests:
            # Group quest
            players = self.group_quests[chat_id]['players']
            end_message = (
                f"The group quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Participants: {', '.join([p.name for p in players.values()])}\n"
            )
            for user_id, character in players.items():
                end_message += (
                    f"\n{character.name}:\n"
                    f"HP: {character.current_hp}/{character.max_hp}\n"
                    f"Energy: {character.current_energy}/{character.max_energy}\n"
                )
            
            # Reset group quest data
            self.group_quests.pop(chat_id, None)
        else:
            # Individual quest
            character = self.characters[chat_id]
            progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100
            end_message = (
                f"Your quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Progress: {progress:.1f}%\n"
                f"Final Karma: {self.player_karma[chat_id]}\n"
                f"Character: {character.name}\n"
                f"HP: {character.current_hp}/{character.max_hp}\n"
                f"Energy: {character.current_energy}/{character.max_energy}\n"
            )

        await self.send_message(update, end_message)

        # Reset quest data
        self.quest_active[chat_id] = False
        self.characters.pop(chat_id, None)
        self.current_stage.pop(chat_id, None)
        self.total_stages.pop(chat_id, None)
        self.current_scene.pop(chat_id, None)
        self.in_combat.pop(chat_id, None)
        self.quest_state.pop(chat_id, None)
        self.quest_goal.pop(chat_id, None)
        self.player_karma.pop(chat_id, None)
        self.current_opponent.pop(chat_id, None)
        self.riddles.pop(chat_id, None)
        self.moral_dilemmas.pop(chat_id, None)
        self.combat_systems.pop(chat_id, None)

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        if chat_id in self.characters:
            character = self.characters[chat_id]
        else:
            await self.send_message(update, "An error occurred: Character not found.")
            return

        next_scene = await self.generate_next_scene(chat_id, user_input)
        self.current_scene[chat_id] = next_scene

        # Send the scene and handle special events in one step
        if "[COMBAT_START]" in next_scene:
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in next_scene:
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in next_scene:
            await self.end_quest(
                update, context, victory=True, reason="You have completed your journey!"
            )
        elif "[QUEST_FAIL]" in next_scene:
            await self.end_quest(
                update, context, victory=False, reason="Your quest has come to an unfortunate end."
            )
        else:
            # Remove the tag if present and send the message
            clean_scene = (
                next_scene.replace("[COMBAT_START]", "")
                .replace("[RIDDLE_START]", "")
                .replace("[QUEST_COMPLETE]", "")
                .replace("[QUEST_FAIL]", "")
                .strip()
            )
            await self.send_message(update, clean_scene)
            self.current_stage[chat_id] += 1

        # Update quest state and check for quest completion
        await self.update_quest_state(chat_id)
        if self.current_stage[chat_id] >= self.total_stages[chat_id]:
            await self.end_quest(
                update, context, victory=True, reason="You have reached the end of your journey!"
            )

        # Update karma and check for quest failure due to low karma
        self.player_karma[chat_id] = max(
            0, min(100, self.player_karma[chat_id] + random.randint(-3, 3))
        )
        if character.current_hp <= 0:
            await self.end_quest(
                update, context, victory=False, reason="Your life force has been depleted."
            )
        elif self.player_karma[chat_id] <= 0:
            await self.end_quest(
                update, context, victory=False, reason="Your karma has fallen too low."
            )

    async def generate_next_scene(self, chat_id: int, user_input: str):
        character = self.characters[chat_id]
        player_karma = self.player_karma[chat_id]
        current_stage = self.current_stage[chat_id]
        total_stages = self.total_stages[chat_id]
        progress = (current_stage / max(1, total_stages)) * 100

        event_type = random.choices(
            [
                "normal",
                "challenge",
                "reward",
                "meditation",
                "npc_encounter",
                "moral_dilemma",
                "spiritual_trial",
                "natural_obstacle",
                "mystical_phenomenon",
                "combat",
                "riddle",
                "puzzle",
            ],
            weights=[15, 15, 10, 5, 10, 10, 5, 5, 5, 10, 5, 5],
            k=1,
        )[0]

        if event_type == "puzzle":
            return await self.generate_puzzle(chat_id)
        elif event_type == "moral_dilemma":
            return await self.generate_moral_dilemma(chat_id)

        prompt = f"""
        Previous scene: {self.current_scene[chat_id]}
        User's action: "{user_input}"
        Character Class: {character.name}
        Character strengths: {', '.join(character.strengths)}
        Character weaknesses: {', '.join(character.weaknesses)}
        Current quest state: {self.quest_state[chat_id]}
        Quest goal: {self.quest_goal[chat_id]}
        Player karma: {player_karma}
        Current stage: {current_stage}
        Total stages: {total_stages}
        Progress: {progress:.2f}%
        Event type: {event_type}

        Generate the next engaging and concise scene of the Zen-themed quest, incorporating elements that contribute to a cohesive storyline. Include:
        1. A brief description of the new environment or situation (1-2 sentences).
        2. The outcome of the user's previous action and its impact on the quest (1 sentence).
        3. A new challenge or decision that relates to the overarching quest goal.
        4. A Zen teaching or insight that offers depth to the narrative.
        5. Three numbered options for the player's next action.

        If applicable, include one of the following tags at the end of the response:
        [COMBAT_START], [RIDDLE_START], [QUEST_COMPLETE], or [QUEST_FAIL]

        Ensure the scene advances the quest toward its conclusion, especially if progress is over 90%.
        Keep the entire response under 200 words.
        """
        next_scene = await self.generate_response(prompt)
        return next_scene

    async def generate_quest_goal(self, class_name):
        prompt = f"""
        Generate a quest goal for a {class_name} in a Zen-themed adventure.
        The goal should be challenging, spiritual in nature, and relate to self-improvement.
        Keep it concise, about 2-3 sentences.
        """
        return await self.generate_response(prompt)

    async def generate_initial_scene(self, quest_goal, class_name):
        prompt = f"""
        Quest goal: {quest_goal}
        Character class: {class_name}

        Generate an initial scene for the quest. Include:
        1. A brief description of the starting location
        2. An introduction to the quest's first challenge
        3. Three possible actions for the player

        Keep the response under 150 words.
        """
        return await self.generate_response(prompt)

    async def handle_riddle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        riddle = self.riddles[chat_id]

        riddle["attempts"] += 1

        if user_input.lower() == riddle["answer"].lower():
            success_message = await self.generate_response(
                f"Generate a brief success message for solving the riddle: {riddle['riddle']}. "
                f"Include a small reward or positive outcome for the {self.characters[chat_id].name}. "
                f"Keep it under 100 words."
            )
            await self.send_message(update, success_message)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "solved riddle")
        elif riddle["attempts"] >= self.max_riddle_attempts:
            failure_consequence = await self.generate_riddle_failure_consequence(chat_id)
            await self.send_message(update, failure_consequence)
            self.riddles[chat_id]["active"] = False
            await self.progress_story(update, context, "failed riddle")
        else:
            remaining_attempts = self.max_riddle_attempts - riddle["attempts"]
            await self.send_message(
                update,
                f"That's not correct. You have {remaining_attempts} attempts remaining. Use /hint for a clue.",
            )

    async def handle_self_harm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        support_message = (
            "I'm sorry to hear that you're feeling this way. Please consider reaching out to a mental health professional or someone you trust for support."
        )
        await self.send_message(update, support_message)

    async def meditate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You must be on a quest to meditate.")
            return

        character = self.characters[chat_id]
        meditation_result = await self.generate_response(
            f"Generate a brief meditation outcome for a {character.name} in their current quest state. "
            "Include a small health and energy boost, and a Zen insight. Keep it under 100 words."
        )

        character.current_hp = min(character.max_hp, character.current_hp + 10)
        character.current_energy = min(character.max_energy, character.current_energy + 10)

        await self.send_message(update, meditation_result)

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        character = self.characters[chat_id]
        progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100

        status_message = (
            f"Quest Progress: {progress:.1f}%\n"
            f"Character: {character.name}\n"
            f"HP: {character.current_hp}/{character.max_hp}\n"
            f"Energy: {character.current_energy}/{character.max_energy}\n"
            f"Karma: {self.player_karma[chat_id]}\n"
            f"Current State: {self.quest_state[chat_id].capitalize()}\n"
            f"Goal: {self.quest_goal[chat_id]}"
        )
        await self.send_message(update, status_message)

    async def interrupt_quest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if not zen_quest.quest_active.get(chat_id, False):
            await update.message.reply_text("You are not currently on a quest.")
            return

        if chat_id in zen_quest.group_quests:
            # Group quest
            if user_id not in zen_quest.group_quests[chat_id]['players']:
                await update.message.reply_text("You are not part of this group quest.")
                return
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by a group member.")
        else:
            # Individual quest
            await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by user.")

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            return

        if chat_id in self.group_quests:
            # Group quest
            players = self.group_quests[chat_id]['players']
            end_message = (
                f"The group quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Participants: {', '.join([p.name for p in players.values()])}\n"
            )
            for user_id, character in players.items():
                end_message += (
                    f"\n{character.name}:\n"
                    f"HP: {character.current_hp}/{character.max_hp}\n"
                    f"Energy: {character.current_energy}/{character.max_energy}\n"
                )
            
            # Reset group quest data
            self.group_quests.pop(chat_id, None)
        else:
            # Individual quest
            character = self.characters[chat_id]
            progress = (self.current_stage[chat_id] / max(1, self.total_stages[chat_id])) * 100
            end_message = (
                f"Your quest has ended.\n"
                f"Reason: {reason}\n"
                f"Victory: {'Yes' if victory else 'No'}\n"
                f"Progress: {progress:.1f}%\n"
                f"Final Karma: {self.player_karma[chat_id]}\n"
                f"Character: {character.name}\n"
                f"HP: {character.current_hp}/{character.max_hp}\n"
                f"Energy: {character.current_energy}/{character.max_energy}\n"
            )

        await self.send_message(update, end_message)

        # Reset quest data
        self.quest_active[chat_id] = False
        self.characters.pop(chat_id, None)
        self.current_stage.pop(chat_id, None)
        self.total_stages.pop(chat_id, None)
        self.current_scene.pop(chat_id, None)
        self.in_combat.pop(chat_id, None)
        self.quest_state.pop(chat_id, None)
        self.quest_goal.pop(chat_id, None)
        self.player_karma.pop(chat_id, None)
        self.current_opponent.pop(chat_id, None)
        self.riddles.pop(chat_id, None)
        self.moral_dilemmas.pop(chat_id, None)
        self.combat_systems.pop(chat_id, None)

    def is_action_unfeasible(self, action):
        return any(word in action.lower() for word in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(word in action.lower() for word in self.failure_actions)

    async def generate_response(self, prompt, max_tokens=500):
        try:
            messages = [
                {
                    "role": "system",
                    "content": "You are a wise Zen master guiding a quest. Avoid any disallowed content and maintain appropriate language. Provide challenging moral dilemmas and opportunities for growth.",
                },
                {"role": "user", "content": prompt},
            ]
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )
            content = response.choices[0].message.content.strip()

            # Perform content moderation
            if await self.is_disallowed_content(content):
                logger.warning("Disallowed content detected in the generated response.")
                return "I'm sorry, but I can't provide a response to that request."
            return content
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

    async def is_disallowed_content(self, content):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('API_KEY')}",
        }
        data = {"input": content}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MODERATION_URL, headers=headers, json=data
                ) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"Moderation API request failed with status {resp.status}"
                        )
                        return False  # Default to not flagged
                    result = await resp.json()
                    flagged = result.get("results", [{}])[0].get("flagged", False)
                    return flagged
        except Exception as e:
            logger.error(f"Error during content moderation: {e}")
            return False  # Default to not flagged

    async def update_quest_state(self, chat_id: int):
        progress = self.current_stage[chat_id] / max(1, self.total_stages[chat_id])
        if progress < 0.33:
            self.quest_state[chat_id] = "beginning"
        elif progress < 0.66:
            self.quest_state[chat_id] = "middle"
        else:
            self.quest_state[chat_id] = "end"

        if chat_id in self.group_quests:
            # Rotate to the next player
            players = list(self.group_quests[chat_id]['players'].values())
            current_player_index = self.group_quests[chat_id]['current_player_index']
            next_player_index = (current_player_index + 1) % len(players)
            self.group_quests[chat_id]['current_player_index'] = next_player_index
            
            # Notify the next player
            next_player_name = players[next_player_index].name
            await self.send_message(
                chat_id,
                f"It's now {next_player_name}'s turn to act. What would you like to do?"
            )

    def get_character_stats(self, user_id):
        logger.info(f"Attempting to get character stats for user {user_id}")
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                query = "SELECT * FROM characters WHERE user_id = %s"
                cursor.execute(query, (user_id,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"Character stats retrieved from database for user {user_id}")
                    return {
                        "name": result['name'],
                        "class": result['class'],
                        "hp": result['hp'],
                        "max_hp": result['max_hp'],
                        "energy": result['energy'],
                        "max_energy": result['max_energy'],
                        "karma": result['karma'],
                        "abilities": [],  # You might want to store abilities separately
                        "wisdom": result['wisdom'],
                        "intelligence": result['intelligence'],
                        "strength": result['strength'],
                        "dexterity": result['dexterity'],
                        "constitution": result['constitution'],
                        "charisma": result['charisma'],
                    }
                else:
                    logger.info(f"No character found in database for user {user_id}")
            except Error as e:
                logger.error(f"Error retrieving character from database: {e}")
            finally:
                cursor.close()
                connection.close()
        else:
            logger.error("Failed to connect to the database when retrieving character stats")
        
        # If no character is found in the database, check the in-memory storage
        if user_id in self.characters:
            character = self.characters[user_id]
            logger.info(f"Character found in memory for user {user_id}")
            return {
                "name": character.name,
                "class": character.__class__.__name__,
                "hp": character.current_hp,
                "max_hp": character.max_hp,
                "energy": character.current_energy,
                "max_energy": character.max_energy,
                "karma": self.player_karma.get(user_id, 100),
                "abilities": character.abilities,
                "wisdom": character.wisdom,
                "intelligence": character.intelligence,
                "strength": character.strength,
                "dexterity": character.dexterity,
                "constitution": character.constitution,
                "charisma": character.charisma,
            }
        
        # If no character is found, return default data
        logger.info(f"No character found for user {user_id}")
        return {
            "name": "No Active Character",
            "class": "None",
            "hp": 0,
            "max_hp": 0,
            "energy": 0,
            "max_energy": 0,
            "karma": 0,
            "abilities": [],
            "wisdom": 0,
            "intelligence": 0,
            "strength": 0,
            "dexterity": 0,
            "constitution": 0,
            "charisma": 0,
        }


# Instantiate the ZenQuest class
zen_quest = ZenQuest()

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to Zen Warrior Quest! Use /zenquest to start your journey.")


async def zenquest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.start_quest(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.get_quest_status(update, context)


async def meditate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.meditate(update, context)


async def interrupt_quest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not zen_quest.quest_active.get(chat_id, False):
        await update.message.reply_text("You are not currently on a quest.")
        return

    if chat_id in zen_quest.group_quests:
        # Group quest
        if user_id not in zen_quest.group_quests[chat_id]['players']:
            await update.message.reply_text("You are not part of this group quest.")
            return
        await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by a group member.")
    else:
        # Individual quest
        await zen_quest.end_quest(update, context, victory=False, reason="Quest interrupted by user.")


async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.handle_hint(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    if not zen_quest.quest_active.get(chat_id, False):
        if chat_type == "private" and update.message.text and not update.message.text.startswith("/"):
            await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
        return

    if zen_quest.in_combat.get(chat_id, False):
        await zen_quest.handle_combat_input(update, context)
    else:
        await zen_quest.handle_input(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")
    if update and hasattr(update, "effective_message") and update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred while processing your request. Please try again later."
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("class_"):
        await zen_quest.select_character_class(update, context)
    elif data.startswith("combat_"):
        await zen_quest.handle_combat_input(update, context)
    elif data.startswith("group_class_"):
        await zen_quest.select_group_character_class(update, context)
    else:
        await query.answer("Unknown action.")
        await query.edit_message_text("An unknown action was requested.")


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.join_group_quest(update, context)


async def start_journey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.start_group_journey(update, context)


# Update the zenstats_command function
async def zenstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    app_url = "https://zenconnectbot-production.up.railway.app"
    zenstats_url = f"{app_url}/zenstats?user_id={user_id}"
    await update.message.reply_text(
        "View your Zen Warrior stats:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Stats", web_app=WebAppInfo(url=zenstats_url))]]
        ),
    )
    logger.info(f"Zenstats command triggered for user {user_id}")


@app.get("/zenstats", response_class=HTMLResponse)
async def zenstats(request: Request, user_id: int):
    return templates.TemplateResponse("zen_stats.html", {"request": request, "user_id": user_id})


@app.get("/api/stats")
async def get_stats(user_id: int):
    stats = zen_quest.get_character_stats(user_id)
    return JSONResponse(content=stats)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://zenconnectbot-production.up.railway.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def main():
    # Set up the application
    token = os.getenv("TELEGRAM_TOKEN")
    application = Application.builder().token(token).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("zenquest", zenquest_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("meditate", meditate_command))
    application.add_handler(CommandHandler("interrupt", interrupt_quest_command))
    application.add_handler(CommandHandler("hint", hint_command))
    application.add_error_handler(error_handler)

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Add new handlers
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("start_journey", start_journey_command))
    application.add_handler(CommandHandler("zenstats", zenstats_command))

    # Set up the database
    setup_database()

    # Set up FastAPI
    global templates
    templates = Jinja2Templates(directory="templates")

    # Start Uvicorn in a separate process
    import multiprocessing

    def run_uvicorn():
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

    p = multiprocessing.Process(target=run_uvicorn)
    p.start()

    # Run the bot in the main thread
    application.run_polling()

    # When bot stops, terminate the web server
    p.terminate()
    p.join()


if __name__ == "__main__":
    main()