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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
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
            # Check if the table exists before trying to create it
            cursor.execute("SHOW TABLES LIKE 'characters'")
            result = cursor.fetchone()
            if not result:
                cursor.execute(
                    """
                    CREATE TABLE characters (
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
                logger.info("Characters table created successfully.")
            else:
                logger.info("Characters table already exists.")
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
        self.group_turn_locks = {}
        self.group_turn_orders = {}
        self.current_group_turns = {}

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

    async def save_character_to_db(self, user_id, character):
        connection = await asyncio.to_thread(get_db_connection)
        if connection:
            try:
                cursor = connection.cursor()
                query = """
                INSERT INTO characters (user_id, name, class, hp, max_hp, energy, max_energy, karma, 
                                        wisdom, intelligence, strength, dexterity, constitution, charisma)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                AS new_values
                ON DUPLICATE KEY UPDATE
                name = new_values.name, class = new_values.class, hp = new_values.hp, max_hp = new_values.max_hp,
                energy = new_values.energy, max_energy = new_values.max_energy, karma = new_values.karma,
                wisdom = new_values.wisdom, intelligence = new_values.intelligence, strength = new_values.strength,
                dexterity = new_values.dexterity, constitution = new_values.constitution, charisma = new_values.charisma
                """
                values = (user_id, character.name, character.__class__.__name__, character.current_hp, 
                          character.max_hp, character.current_energy, character.max_energy, 
                          self.player_karma.get(user_id, 100), character.wisdom, character.intelligence, 
                          character.strength, character.dexterity, character.constitution, character.charisma)
                await asyncio.to_thread(cursor.execute, query, values)
                await asyncio.to_thread(connection.commit)
                logger.info(f"Character saved to database for user {user_id}")
            except Error as e:
                logger.error(f"Error saving character to database: {e}")
            finally:
                await asyncio.to_thread(cursor.close)
                await asyncio.to_thread(connection.close)
        else:
            logger.error("Failed to connect to the database when saving character")

    async def get_character_stats(self, user_id):
        logger.info(f"Attempting to get character stats for user {user_id}")
        connection = await asyncio.to_thread(get_db_connection)
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                query = "SELECT * FROM characters WHERE user_id = %s"
                await asyncio.to_thread(cursor.execute, query, (user_id,))
                result = await asyncio.to_thread(cursor.fetchone)
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
                await asyncio.to_thread(cursor.close)
                await asyncio.to_thread(connection.close)
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
        
        if update.message and update.message.text:
            user_input = update.message.text.strip()
        else:
            return  # Non-text message received

        # Only process input if there's an active quest for this chat
        if self.quest_active.get(chat_id, False):
            if chat_id in self.group_quests:
                await self.handle_group_input(update, context, user_input)
            elif self.in_combat.get(chat_id, False):
                await self.handle_combat_action(update, context, user_input)
            else:
                await self.progress_quest(update, context, user_input)
        # Don't respond if there's no active quest

    async def handle_group_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if chat_id not in self.group_turn_locks:
            self.group_turn_locks[chat_id] = asyncio.Lock()
            self.group_turn_orders[chat_id] = list(self.group_quests[chat_id]["players"].keys())
            self.current_group_turns[chat_id] = 0

        async with self.group_turn_locks[chat_id]:
            current_player = self.group_turn_orders[chat_id][self.current_group_turns[chat_id]]
            if user_id != current_player:
                await update.message.reply_text("It's not your turn yet. Please wait.")
                return

            # Process the player's action
            await self.progress_quest(update, context, user_input)

            # Move to the next player's turn
            self.current_group_turns[chat_id] = (self.current_group_turns[chat_id] + 1) % len(self.group_turn_orders[chat_id])
            next_player = self.group_turn_orders[chat_id][self.current_group_turns[chat_id]]
            await update.message.reply_text(f"It's now <@{next_player}>'s turn.")

    async def generate_response(self, prompt):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant for a Zen-themed D&D-style game."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=300,
                n=1,
                temperature=0.7,
            )
            return response.choices[0].message.content.strip()
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {e}")
            return "An error occurred while generating the response. Please try again."
        except Exception as e:
            logger.error(f"Unexpected error in generate_response: {e}")
            return "An unexpected error occurred. Please try again later."

    async def generate_opponent(self, character):
        level = self.current_stage[character.user_id] // 3 + 1  # Every 3 stages increase opponent level
        prompt = f"""
        Generate a challenging opponent for a level {level} {character.__class__.__name__} in a Zen-themed D&D-style quest.
        Include:
        1. Name
        2. Brief description (1-2 sentences)
        3. HP (between 20-50)
        4. Two unique abilities
        5. Two strengths
        6. Two weaknesses

        Format the response as a JSON object.
        """
        opponent_json = await self.generate_response(prompt)
        try:
            opponent_data = json.loads(opponent_json)
            return Character(
                opponent_data['name'],
                opponent_data['hp'],
                opponent_data['hp'],  # max_hp same as current_hp
                opponent_data['abilities'],
                opponent_data['strengths'],
                opponent_data['weaknesses']
            )
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON: {e}")
            return None
        except KeyError as e:
            logger.error(f"Missing key in opponent data: {e}")
            return None

    async def progress_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]
        
        # Generate the next scene based on user input and character
        next_scene = await self.generate_next_scene(chat_id, user_input, character)
        
        # Update quest state
        self.current_scene[chat_id] = next_scene
        self.current_stage[chat_id] += 1
        
        # Check for special events
        if "[COMBAT_START]" in next_scene:
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in next_scene:
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in next_scene:
            await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
        elif "[QUEST_FAIL]" in next_scene:
            await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
        else:
            # Send the scene to the user
            await self.send_message(update, next_scene)
        
        # Update quest state and check for quest completion
        await self.update_quest_state(chat_id)
        if self.current_stage[chat_id] >= self.total_stages[chat_id]:
            await self.end_quest(update, context, victory=True, reason="You have reached the end of your journey!")

    async def generate_next_scene(self, chat_id: int, user_input: str, character):
        current_scene = self.current_scene[chat_id]
        quest_state = self.quest_state[chat_id]
        karma = self.player_karma[chat_id]
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100

        prompt = f"""
        Current scene: {current_scene}
        User action: "{user_input}"
        Character class: {character.__class__.__name__}
        Character stats: Strength {character.strength}, Dexterity {character.dexterity}, 
                         Constitution {character.constitution}, Intelligence {character.intelligence}, 
                         Wisdom {character.wisdom}, Charisma {character.charisma}
        Quest state: {quest_state}
        Karma: {karma}
        Progress: {progress:.2f}%

        Generate the next scene of the Zen-themed D&D-style quest. Include:
        1. A brief description of the new situation (2-3 sentences).
        2. The outcome of the user's action, considering their character's stats and abilities.
        3. A new challenge or decision point related to the quest goal.
        4. A subtle Zen teaching or insight.
        5. Three numbered options for the player's next action.

        If appropriate, include one of these tags: [COMBAT_START], [RIDDLE_START], [QUEST_COMPLETE], [QUEST_FAIL]

        Keep the entire response under 200 words and maintain an engaging, D&D with Zen vibes style.
        """
        return await self.generate_response(prompt)

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]

        opponent = await self.generate_opponent(character)
        self.current_opponent[chat_id] = opponent
        self.in_combat[chat_id] = True

        combat_start_message = (
            f"You encounter {opponent.name}!\n"
            f"{opponent.description}\n"
            f"Prepare for combat!\n"
            f"Your HP: {character.current_hp}/{character.max_hp}\n"
            f"Opponent HP: {opponent.current_hp}/{opponent.max_hp}\n"
            f"What will you do?\n"
            f"1. Attack\n"
            f"2. Use ability\n"
            f"3. Attempt to flee"
        )
        await self.send_message(update, combat_start_message)

    async def handle_combat_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]
        opponent = self.current_opponent[chat_id]

        if user_input.lower() == "attack" or user_input == "1":
            damage = self.calculate_damage(character, opponent)
            opponent.current_hp -= damage
            result = f"You attack {opponent.name} for {damage} damage!"
        elif user_input.lower() == "use ability" or user_input == "2":
            ability = random.choice(character.abilities)
            damage = self.calculate_damage(character, opponent, is_ability=True)
            opponent.current_hp -= damage
            result = f"You use {ability} on {opponent.name} for {damage} damage!"
        elif user_input.lower() == "flee" or user_input == "3":
            if random.random() < 0.5:  # 50% chance to flee
                self.in_combat[chat_id] = False
                await self.send_message(update, "You successfully flee from combat!")
                await self.progress_quest(update, context, "fled from combat")
                return
            else:
                result = "You fail to flee!"
        else:
            await self.send_message(update, "Invalid combat action. Please choose Attack, Use ability, or Flee.")
            return

        # Opponent's turn
        if opponent.current_hp > 0:
            opponent_damage = self.calculate_damage(opponent, character)
            character.current_hp -= opponent_damage
            result += f"\n{opponent.name} attacks you for {opponent_damage} damage!"

        # Check combat result
        if opponent.current_hp <= 0:
            result += f"\nYou have defeated {opponent.name}!"
            self.in_combat[chat_id] = False
            await self.send_message(update, result)
            await self.progress_quest(update, context, "won combat")
        elif character.current_hp <= 0:
            result += "\nYou have been defeated!"
            await self.send_message(update, result)
            await self.end_quest(update, context, victory=False, reason="You have been defeated in combat.")
        else:
            result += f"\nYour HP: {character.current_hp}/{character.max_hp}"
            result += f"\n{opponent.name}'s HP: {opponent.current_hp}/{opponent.max_hp}"
            result += "\nWhat will you do next?"
            await self.send_message(update, result)

    def calculate_damage(self, attacker, defender, is_ability=False):
        base_damage = random.randint(1, 8)
        if isinstance(attacker, Character):
            stat_bonus = (attacker.strength - 10) // 2
        else:
            stat_bonus = 0
        
        if is_ability:
            base_damage += random.randint(1, 6)
        
        total_damage = base_damage + stat_bonus
        return max(1, total_damage)  # Minimum 1 damage

    async def generate_opponent(self, character):
        level = self.current_stage[character.user_id] // 3 + 1  # Every 3 stages increase opponent level
        prompt = f"""
        Generate a challenging opponent for a level {level} {character.__class__.__name__} in a Zen-themed D&D-style quest.
        Include:
        1. Name
        2. Brief description (1-2 sentences)
        3. HP (between 20-50)
        4. Two unique abilities
        5. Two strengths
        6. Two weaknesses

        Format the response as a JSON object.
        """
        opponent_json = await self.generate_response(prompt)
        try:
            opponent_data = json.loads(opponent_json)
            return Character(
                opponent_data['name'],
                opponent_data['hp'],
                opponent_data['hp'],  # max_hp same as current_hp
                opponent_data['abilities'],
                opponent_data['strengths'],
                opponent_data['weaknesses']
            )
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON: {e}")
            return None
        except KeyError as e:
            logger.error(f"Missing key in opponent data: {e}")
            return None

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

    async def generate_response(self, prompt):
        try:
            response = await client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant for a Zen-themed D&D-style game."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=300,
                n=1,
                temperature=0.7,
            )
            return response.choices[0].message.content.strip()
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {e}")
            return "An error occurred while generating the response. Please try again."
        except Exception as e:
            logger.error(f"Unexpected error in generate_response: {e}")
            return "An unexpected error occurred. Please try again later."

    async def send_message(self, update: Update, message: str):
        try:
            await update.message.reply_text(message)
        except Exception as e:
            logger.error(f"Error sending message: {e}")

    async def update_quest_state(self, chat_id: int):
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100
        if progress < 33:
            self.quest_state[chat_id] = "beginning"
        elif progress < 66:
            self.quest_state[chat_id] = "middle"
        else:
            self.quest_state[chat_id] = "nearing_end"

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        chat_id = update.effective_chat.id
        self.quest_active[chat_id] = False
        
        if victory:
            message = f"Congratulations! {reason}\nYour quest has come to a successful end."
        else:
            message = f"Quest failed. {reason}\nBetter luck on your next journey."
        
        await self.send_message(update, message)
        # Reset quest-related data for this chat
        self.current_stage.pop(chat_id, None)
        self.total_stages.pop(chat_id, None)
        self.current_scene.pop(chat_id, None)
        self.quest_state.pop(chat_id, None)
        self.quest_goal.pop(chat_id, None)

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        riddle_data = await self.generate_riddle()
        self.riddles[chat_id] = {
            "riddle": riddle_data["riddle"],
            "answer": riddle_data["answer"],
            "hint": riddle_data["hint"],
            "attempts": 0,
            "active": True
        }
        await self.send_message(update, f"Riddle: {riddle_data['riddle']}")

    async def generate_riddle(self):
        prompt = """
        Generate a Zen-themed riddle with the following:
        1. The riddle itself
        2. The answer
        3. A hint

        Format the response as a JSON object.
        """
        riddle_json = await self.generate_response(prompt)
        try:
            return json.loads(riddle_json)
        except json.JSONDecodeError:
            logger.error("Error decoding riddle JSON")
            return {"riddle": "What is the sound of one hand clapping?", "answer": "Silence", "hint": "Listen carefully to nothing"}

    async def generate_riddle_failure_consequence(self, chat_id: int):
        character = self.characters[chat_id]
        prompt = f"""
        Generate a brief consequence for a {character.__class__.__name__} failing to solve a riddle in a Zen-themed quest.
        The consequence should be minor but impactful. Keep it under 100 words.
        """
        return await self.generate_response(prompt)

    async def handle_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id in self.riddles and self.riddles[chat_id]["active"]:
            hint = self.riddles[chat_id]["hint"]
            await self.send_message(update, f"Hint: {hint}")
        else:
            await self.send_message(update, "There is no active riddle to hint for.")

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        if not self.quest_active.get(chat_id, False):
            await update.message.reply_text("You're not on an active quest. Use /zenquest to start one!")
            return

        character_stats = await self.get_character_stats(user_id)
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100

        status_message = (
            f"Quest Progress: {progress:.2f}%\n"
            f"Current Stage: {self.current_stage[chat_id]}/{self.total_stages[chat_id]}\n"
            f"Character: {character_stats['name']} ({character_stats['class']})\n"
            f"HP: {character_stats['hp']}/{character_stats['max_hp']}\n"
            f"Energy: {character_stats['energy']}/{character_stats['max_energy']}\n"
            f"Karma: {character_stats['karma']}\n"
            f"Quest State: {self.quest_state[chat_id]}\n"
            f"\nAbilities: {', '.join(character_stats['abilities'])}\n"
            f"\nStats:\n"
            f"Strength: {character_stats['strength']}\n"
            f"Dexterity: {character_stats['dexterity']}\n"
            f"Constitution: {character_stats['constitution']}\n"
            f"Intelligence: {character_stats['intelligence']}\n"
            f"Wisdom: {character_stats['wisdom']}\n"
            f"Charisma: {character_stats['charisma']}"
        )

        await update.message.reply_text(status_message)

    async def handle_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        
        if not self.quest_active.get(chat_id, False):
            await update.message.reply_text("There's no active quest to interrupt.")
            return

        await self.end_quest(update, context, victory=False, reason="Quest interrupted by user.")
        await update.message.reply_text("Your quest has been interrupted and ended.")

    async def handle_quest_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if self.quest_active.get(chat_id, False):
            if update.message and update.message.text:
                user_input = update.message.text.strip()
                await self.handle_input(update, context)
            else:
                await update.message.reply_text("Please provide a text input for your action.")

    async def handle_zenstats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        if context.args and len(context.args) > 0:
            try:
                target_user_id = int(context.args[0])
                if chat_id in self.group_quests and target_user_id in self.group_quests[chat_id]["players"]:
                    user_id = target_user_id
                else:
                    await update.message.reply_text("Invalid user ID or user not in this group quest.")
                    return
            except ValueError:
                await update.message.reply_text("Invalid user ID format.")
                return
        
        character_stats = await self.get_character_stats(user_id)
        
        if character_stats['name'] == "No Active Character":
            await update.message.reply_text("This user doesn't have an active character.")
            return

        # Encode character stats as a JSON string and then URL-encode it
        stats_json = json.dumps(character_stats)
        encoded_stats = urllib.parse.quote(stats_json)

        # Use the GitHub Pages URL
        html_url = "https://ber1nd.github.io/ZenConnectBot/templates/zen_stats.html"
        
        webapp_button = InlineKeyboardButton(
            text="View Character Sheet",
            web_app=WebAppInfo(url=f"{html_url}?stats={encoded_stats}")
        )
        keyboard = InlineKeyboardMarkup([[webapp_button]])

        await update.message.reply_text(
            f"Click the button below to view the character sheet for user {user_id}:",
            reply_markup=keyboard
        )

    async def handle_group_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if chat_id not in self.group_turn_locks:
            self.group_turn_locks[chat_id] = asyncio.Lock()
            self.group_turn_orders[chat_id] = list(self.group_quests[chat_id]["players"].keys())
            self.current_group_turns[chat_id] = 0

        async with self.group_turn_locks[chat_id]:
            current_player = self.group_turn_orders[chat_id][self.current_group_turns[chat_id]]
            if user_id != current_player:
                await update.message.reply_text("It's not your turn yet. Please wait.")
                return

            # Process the player's action
            await self.progress_quest(update, context, user_input)

            # Move to the next player's turn
            self.current_group_turns[chat_id] = (self.current_group_turns[chat_id] + 1) % len(self.group_turn_orders[chat_id])
            next_player = self.group_turn_orders[chat_id][self.current_group_turns[chat_id]]
            await update.message.reply_text(f"It's now <@{next_player}>'s turn.")

    async def list_group_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id not in self.group_quests:
            await update.message.reply_text("There is no active group quest in this chat.")
            return

        players = self.group_quests[chat_id]["players"]
        player_list = "\n".join([f"Player {i+1}: {player.name} (ID: {user_id})" 
                                 for i, (user_id, player) in enumerate(players.items())])
        await update.message.reply_text(f"Players in this group quest:\n{player_list}\n\n"
                                        f"Use /zenstats <player_id> to view a specific player's stats.")

# Main function to set up and run the bot
def main():
    setup_database()
    
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable is not set!")
        return

    logger.info(f"Token: {token[:5]}...{token[-5:]}")  # Log first and last 5 characters of the token
    
    try:
        application = Application.builder().token(token).build()
    except InvalidToken:
        logger.error("Invalid Telegram Bot Token. Please check your TELEGRAM_TOKEN environment variable.")
        return
    
    zen_quest = ZenQuest()
    
    application.add_handler(CommandHandler("start", zen_quest.start_quest))
    application.add_handler(CommandHandler("zenquest", zen_quest.start_quest))
    application.add_handler(CommandHandler("join", zen_quest.join_group_quest))
    application.add_handler(CommandHandler("start_journey", zen_quest.start_group_journey))
    application.add_handler(CommandHandler("status", zen_quest.handle_status))
    application.add_handler(CommandHandler("hint", zen_quest.handle_hint))
    application.add_handler(CommandHandler("interrupt", zen_quest.handle_interrupt))
    application.add_handler(CommandHandler("zenstats", zen_quest.handle_zenstats))
    application.add_handler(CommandHandler("groupplayers", zen_quest.list_group_players))
    
    application.add_handler(CallbackQueryHandler(zen_quest.select_character_class, pattern="^class_"))
    application.add_handler(CallbackQueryHandler(zen_quest.select_group_character_class, pattern="^group_class_"))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, zen_quest.handle_quest_message))
    
    application.run_polling()


if __name__ == "__main__":
    main()