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
        
        if update.message and update.message.text:
            user_input = update.message.text.strip()
        else:
            return  # Non-text message received

        if not self.quest_active.get(chat_id, False):
            await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
            return

        if self.in_combat.get(chat_id, False):
            await self.handle_combat_action(update, context, user_input)
        else:
            await self.progress_quest(update, context, user_input)

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

        # Generate an opponent based on the character's level and quest progress
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

        Format the response as a JSON object.
        """
        opponent_json = await self.generate_response(prompt)
        opponent_data = json.loads(opponent_json)
        
        return Character(
            opponent_data['name'],
            opponent_data['hp'],
            opponent_data['hp'],  # max_hp same as current_hp
            opponent_data['abilities'],
            [],  # strengths
            [],  # weaknesses
        )

    # ... (other existing methods)


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