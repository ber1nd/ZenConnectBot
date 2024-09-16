import json
import os
import logging
import random
from dotenv import load_dotenv
import mysql.connector # Ensure this is installed
from mysql.connector import errorcode
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from openai import AsyncOpenAI

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Rate limiting parameters
RATE_LIMIT = 5  # Number of messages
RATE_TIME_WINDOW = 60  # Time window in seconds
chat_message_times = defaultdict(list)

def get_db_connection():
    try:
        connection = mysql.connector.connect(
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            host=os.getenv("MYSQLHOST"),
            database=os.getenv("MYSQLDATABASE"),  # Corrected variable name
            port=int(os.getenv("MYSQLPORT", 3306)),
            raise_on_warnings=True
        )
        logger.info("Database connection established successfully.")
        return connection
    except mysql.connector.Error as err:
        logger.error(f"Database connection error: {err}")
        return None

def setup_database():
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            # Create users table
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
            # Other table creation statements...
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
    def __init__(self, class_name, hp, energy, abilities, strengths, weaknesses):
        self.name = class_name
        self.max_hp = hp
        self.current_hp = hp
        self.max_energy = energy
        self.current_energy = energy
        self.abilities = abilities
        self.strengths = strengths
        self.weaknesses = weaknesses

class CombatSystem:
    def __init__(self):
        self.turn_order = []
        self.current_turn = 0

    def initialize_combat(self, players, opponents):
        self.turn_order = players + opponents
        random.shuffle(self.turn_order)
        self.current_turn = 0

    def next_turn(self):
        self.current_turn = (self.current_turn + 1) % len(self.turn_order)
        return self.turn_order[self.current_turn]

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
            "fly", "teleport", "time travel", "breathe underwater", "become invisible",
            "read minds", "shoot lasers", "transform", "resurrect", "conjure",
            "summon creatures", "control weather", "phase through walls"
        ]
        self.failure_actions = [
            "give up", "abandon quest", "betray", "surrender",
            "destroy sacred artifact", "harm innocent", "break vow", "ignore warning",
            "consume poison", "jump off cliff", "attack ally", "steal from temple"
        ]
        self.character_classes = {
            "Monk": Character(
                "Monk", 100, 100,
                ["Meditate", "Chi Strike", "Healing Touch", "Spirit Ward"],
                ["spiritual challenges", "endurance"],
                ["physical combat", "technology"]
            ),
            "Samurai": Character(
                "Samurai", 120, 80,
                ["Katana Slash", "Bushido Stance", "Focused Strike", "Honor Guard"],
                ["physical combat", "honor-based challenges"],
                ["spiritual challenges", "deception"]
            ),
            "Shaman": Character(
                "Shaman", 90, 110,
                ["Nature's Wrath", "Spirit Link", "Elemental Shield", "Ancestral Guidance"],
                ["nature-based challenges", "spiritual insight"],
                ["urban environments", "technology"]
            )
        }

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        
        if self.quest_active.get(chat_id, False):
            await update.message.reply_text(
                "A quest is already active in this chat. Use /status to check progress or /interrupt to end the current quest."
            )
            return

        keyboard = [[InlineKeyboardButton(class_name, callback_data=f"class_{class_name.lower()}") 
                     for class_name in self.character_classes.keys()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Choose your character class to begin your Zen journey:",
            reply_markup=reply_markup
        )

    async def select_character_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        class_name = query.data.split('_')[1].capitalize()

        if class_name not in self.character_classes:
            await query.edit_message_text("Invalid class selection. Please choose a valid class.")
            return

        self.characters[chat_id] = self.character_classes[class_name]
        self.quest_active[chat_id] = True
        self.current_stage[chat_id] = 0
        self.total_stages[chat_id] = random.randint(10, 20)  # Adjusted for demo purposes
        self.quest_state[chat_id] = "beginning"
        self.in_combat[chat_id] = False
        self.player_karma[chat_id] = 100

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

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # Rate limiting per chat
        current_time = datetime.now()
        chat_times = chat_message_times[chat_id]
        chat_times = [t for t in chat_times if (current_time - t).seconds < RATE_TIME_WINDOW]
        chat_times.append(current_time)
        chat_message_times[chat_id] = chat_times
        if len(chat_times) > RATE_LIMIT:
            await update.message.reply_text("This chat is sending messages too quickly. Please slow down.")
            return

        if update.message and update.message.text:
            user_input = update.message.text.strip()
        else:
            return  # Non-text message received

        if not self.quest_active.get(chat_id, False):
            if update.effective_chat.type == 'private':
                await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
            return

        if self.in_combat.get(chat_id, False):
            await update.message.reply_text("You're in combat! Use the combat options provided.")
            return

        if chat_id in self.riddles and self.riddles[chat_id]['active']:
            await self.handle_riddle_input(update, context, user_input)
            return

        if any(word in user_input.lower() for word in ["hurt myself", "self-harm", "suicide", "kill myself", "cut"]):
            await self.handle_self_harm(update, context, user_input)
            return

        # Check for special commands
        if user_input.startswith('/'):
            command = user_input[1:].split()[0]
            if command == 'meditate':
                await self.meditate(update, context)
            elif command == 'status':
                await self.get_quest_status(update, context)
            elif command == 'interrupt':
                await self.interrupt_quest(update, context)
            elif command == 'hint':
                await self.handle_hint(update, context)
            else:
                await update.message.reply_text(
                    "Unknown command. Available commands: /meditate, /status, /interrupt, /hint"
                )
            return

        # Process action
        action_result = await self.process_action(chat_id, user_input)
        await self.send_message(update, action_result)

        # Handle special events
        if "[COMBAT_START]" in action_result:
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in action_result:
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in action_result:
            await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
        elif "[QUEST_FAIL]" in action_result:
            await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
        elif "[MORAL_CHOICE]" in action_result:
            await self.present_moral_choice(update, context)
        else:
            # Remove the tag if present and send the message
            clean_result = action_result.replace("[COMBAT_START]", "").replace("[RIDDLE_START]", "").replace("[QUEST_COMPLETE]", "").replace("[QUEST_FAIL]", "").replace("[MORAL_CHOICE]", "").strip()
            await self.send_message(update, clean_result)
            self.current_scene[chat_id] = clean_result
            self.current_stage[chat_id] += 1
            await self.update_quest_state(chat_id)

    async def send_message(self, update: Update, text: str, reply_markup=None):
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def process_action(self, chat_id: int, user_input: str):
        character = self.characters[chat_id]
        current_scene = self.current_scene[chat_id]
        quest_state = self.quest_state[chat_id]
        karma = self.player_karma[chat_id]

        if self.is_action_unfeasible(user_input):
            return "That action is not possible in this realm. Please choose a different path."
        elif self.is_action_failure(user_input):
            return "[QUEST_FAIL]: Your choice leads to an unfortunate end."

        prompt = f"""
        Current scene: {current_scene}
        Character class: {character.name}
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
        character = self.characters[chat_id]

        next_scene = await self.generate_next_scene(chat_id, user_input)
        self.current_scene[chat_id] = next_scene

        # Send the scene and handle special events in one step
        if "[COMBAT_START]" in next_scene:
            await self.initiate_combat(update, context)
        elif "[RIDDLE_START]" in next_scene:
            await self.initiate_riddle(update, context)
        elif "[QUEST_COMPLETE]" in next_scene:
            await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
        elif "[QUEST_FAIL]" in next_scene:
            await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
        else:
            # Remove the tag if present and send the message
            clean_scene = next_scene.replace("[COMBAT_START]", "").replace("[RIDDLE_START]", "").replace("[QUEST_COMPLETE]", "").replace("[QUEST_FAIL]", "").strip()
            await self.send_message(update, clean_scene)
            self.current_stage[chat_id] += 1
            await self.update_quest_state(chat_id)

        # Update karma and check for quest failure due to low karma
        self.player_karma[chat_id] = max(0, min(100, self.player_karma[chat_id] + random.randint(-3, 3)))
        if character.current_hp <= 0:
            await self.end_quest(update, context, victory=False, reason="Your life force has been depleted.")
        elif self.player_karma[chat_id] <= 0:
            await self.end_quest(update, context, victory=False, reason="Your karma has fallen too low.")

    async def generate_next_scene(self, chat_id: int, user_input: str):
        character = self.characters[chat_id]
        player_karma = self.player_karma[chat_id]
        current_stage = self.current_stage[chat_id]
        total_stages = self.total_stages[chat_id]
        progress = (current_stage / total_stages) * 100

        event_type = random.choices(
            ["normal", "challenge", "reward", "meditation", "npc_encounter", "moral_dilemma",
             "spiritual_trial", "natural_obstacle", "mystical_phenomenon", "combat", "riddle"],
            weights=[15, 15, 10, 5, 10, 10, 5, 5, 5, 15, 5],
            k=1
        )[0]

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
        
        riddle['attempts'] += 1
        
        if user_input.lower() == riddle['answer'].lower():
            success_message = await self.generate_response(
                f"Generate a brief success message for solving the riddle: {riddle['riddle']}. "
                f"Include a small reward or positive outcome for the {self.characters[chat_id].name}. "
                f"Keep it under 100 words."
            )
            await self.send_message(update, success_message)
            self.riddles[chat_id]['active'] = False
            await self.progress_story(update, context, "solved riddle")
        elif riddle['attempts'] >= self.max_riddle_attempts:
            failure_consequence = await self.generate_riddle_failure_consequence(chat_id)
            await self.send_message(update, failure_consequence)
            self.riddles[chat_id]['active'] = False
            await self.progress_story(update, context, "failed riddle")
        else:
            remaining_attempts = self.max_riddle_attempts - riddle['attempts']
            await self.send_message(update, f"That's not correct. You have {remaining_attempts} attempts remaining. Use /hint for a clue.")

    async def handle_self_harm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        support_message = (
            "I'm concerned about what you've said. Remember, you're valuable and your life matters. "
            "If you're having thoughts of self-harm, please reach out for help. "
            "Here are some resources:\n"
            "- Crisis Text Line: Text HOME to 741741\n"
            "- National Suicide Prevention Lifeline: 1-800-273-8255\n"
            "- International helplines: https://www.befrienders.org"
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
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100
        
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

    async def interrupt_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        await self.end_quest(update, context, victory=False, reason="Quest interrupted by user.")

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        chat_id = update.effective_chat.id
        if not self.quest_active.get(chat_id, False):
            return

        character = self.characters[chat_id]
        progress = (self.current_stage[chat_id] / self.total_stages[chat_id]) * 100
        
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
        
        # Reset user's quest data
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

    async def present_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        prompt = (
            "Generate a moral dilemma for the player's Zen quest. "
            "Present three choices, each with potential consequences. "
            "Format the choices as numbered options (1, 2, 3). "
            "Keep the description and choices under 200 words total."
        )
        dilemma = await self.generate_response(prompt)
        self.moral_dilemmas[chat_id] = {'active': True, 'dilemma': dilemma}
        await self.send_message(update, dilemma)

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]
        
        opponent_prompt = f"Generate a challenging opponent for a {character.name} in a Zen-themed quest. Include name, brief description, HP, and two unique abilities. Format as JSON."
        opponent_json = await self.generate_response(opponent_prompt)
        opponent = json.loads(opponent_json)
        
        self.current_opponent[chat_id] = opponent
        self.in_combat[chat_id] = True
        
        self.combat_system = CombatSystem()
        self.combat_system.initialize_combat([character], [opponent])
        
        combat_start_message = (
            f"You encounter {opponent['name']}!\n"
            f"{opponent['description']}\n"
            f"Prepare for combat!\n"
            f"Combat order: {', '.join([c.name if hasattr(c, 'name') else c['name'] for c in self.combat_system.turn_order])}\n"
        )
        
        await self.send_message(update, combat_start_message)
        await self.present_combat_options(update, context)

    async def present_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        current_character = self.combat_system.turn_order[self.combat_system.current_turn]
        
        if hasattr(current_character, 'name'):  # Player's turn
            character = self.characters[chat_id]
            keyboard = [
                [InlineKeyboardButton("Basic Attack", callback_data="combat_basic_attack")],
                [InlineKeyboardButton("Defend", callback_data="combat_defend")],
                [InlineKeyboardButton(f"Use {character.abilities[0]}", callback_data=f"combat_ability_{character.abilities[0]}")],
                [InlineKeyboardButton(f"Use {character.abilities[1]}", callback_data=f"combat_ability_{character.abilities[1]}")],
                [InlineKeyboardButton("Attempt to flee", callback_data="combat_flee")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.send_message(update, f"{character.name}'s turn!\nChoose your action:", reply_markup=reply_markup)
        else:  # Opponent's turn
            await self.handle_opponent_turn(update, context)

    async def handle_combat_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        action = query.data.split('_', 1)[1]  # Remove 'combat_' prefix
        
        character = self.characters[chat_id]
        opponent = self.current_opponent[chat_id]
        
        action_result = await self.resolve_combat_action(character, opponent, action)
        await query.edit_message_text(action_result)
        
        if "combat ended" in action_result.lower():
            self.in_combat[chat_id] = False
            self.current_opponent.pop(chat_id, None)
            await self.progress_story(update, context, "combat ended")
        else:
            self.combat_system.next_turn()
            await self.present_combat_options(update, context)

    async def handle_opponent_turn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        opponent = self.current_opponent[chat_id]
        character = self.characters[chat_id]
        
        action_prompt = f"Generate a strategic combat action for {opponent['name']} against {character.name}. Consider the opponent's abilities and the character's strengths/weaknesses. Keep it under 50 words."
        action = await self.generate_response(action_prompt)
        
        result = await self.resolve_combat_action(opponent, character, action)
        await self.send_message(update, f"{opponent['name']}'s turn:\n{result}")
        
        if "combat ended" in result.lower():
            self.in_combat[chat_id] = False
            self.current_opponent.pop(chat_id, None)
            await self.progress_story(update, context, "combat ended")
        else:
            self.combat_system.next_turn()
            await self.present_combat_options(update, context)

    async def resolve_combat_action(self, attacker, defender, action):
        prompt = f"""
        Attacker: {attacker.name if hasattr(attacker, 'name') else attacker['name']}
        Attacker's abilities: {', '.join(attacker.abilities) if hasattr(attacker, 'abilities') else ', '.join(attacker['abilities'])}
        Defender: {defender.name if hasattr(defender, 'name') else defender['name']}
        Action: {action}

        Resolve the combat action, considering the attacker's abilities and the defender's strengths/weaknesses.
        Include any damage dealt, status effects applied, or other relevant outcomes.
        If the combat ends, clearly state whether the attacker or defender won.
        Keep the response under 100 words.
        """
        result = await self.generate_response(prompt)
        
        # Update HP based on the result
        if hasattr(attacker, 'current_hp'):
            attacker.current_hp = max(0, attacker.current_hp - random.randint(5, 15))
        else:
            attacker['current_hp'] = max(0, attacker['current_hp'] - random.randint(5, 15))
        
        if hasattr(defender, 'current_hp'):
            defender.current_hp = max(0, defender.current_hp - random.randint(5, 15))
        else:
            defender['current_hp'] = max(0, defender['current_hp'] - random.randint(5, 15))
        
        return result

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        character = self.characters[chat_id]
        prompt = f"Generate a Zen-themed riddle related to {character.name}'s quest. Include the riddle, its answer, and three hints of increasing clarity. Format as JSON."
        riddle_json = await self.generate_response(prompt)
        riddle_data = json.loads(riddle_json)
        
        self.riddles[chat_id] = {
            'active': True,
            'riddle': riddle_data['riddle'],
            'answer': riddle_data['answer'],
            'hints': riddle_data['hints'],
            'attempts': 0,
            'hints_used': 0
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
        if chat_id in self.riddles and self.riddles[chat_id]['active']:
            riddle = self.riddles[chat_id]
            if riddle['hints_used'] < len(riddle['hints']):
                hint = riddle['hints'][riddle['hints_used']]
                riddle['hints_used'] += 1
                await self.send_message(update, f"Hint: {hint}")
            else:
                await self.send_message(update, "You've used all available hints. Try to solve the riddle or face the consequences of failure.")
        else:
            await self.send_message(update, "There is no active riddle to hint for.")

    async def generate_riddle_failure_consequence(self, chat_id: int):
        character = self.characters[chat_id]
        consequence_type = random.choice(["combat", "karma_loss", "hp_loss"])
        
        if consequence_type == "combat":
            self.in_combat[chat_id] = True
            opponent_prompt = f"Generate a challenging opponent for a {character.name} as a consequence of failing to solve a riddle. Include name, brief description, HP, and two unique abilities. Format as JSON."
            opponent_json = await self.generate_response(opponent_prompt)
            self.current_opponent[chat_id] = json.loads(opponent_json)
            return f"Your failure to solve the riddle has summoned {self.current_opponent[chat_id]['name']}! Prepare for combat!"
        elif consequence_type == "karma_loss":
            karma_loss = random.randint(10, 20)
            self.player_karma[chat_id] = max(0, self.player_karma[chat_id] - karma_loss)
            return f"Your failure to solve the riddle has disturbed the cosmic balance. You lose {karma_loss} karma points."
        else:  # hp_loss
            hp_loss = random.randint(10, 20)
            character.current_hp = max(0, character.current_hp - hp_loss)
            return f"The mystical energies of the unsolved riddle lash out at you. You lose {hp_loss} HP."

    def is_action_unfeasible(self, action):
        return any(word in action.lower() for word in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(word in action.lower() for word in self.failure_actions)

    async def generate_response(self, prompt, max_tokens=500):
        try:
            messages = [
                {"role": "system", "content": "You are a wise Zen master guiding a quest. Maintain realism for human capabilities. Actions should have logical consequences. Provide challenging moral dilemmas and opportunities for growth."},
                {"role": "user", "content": prompt}
            ]
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

    async def send_scene(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        scene = self.current_scene[chat_id]
        await self.send_message(update, scene)

    async def update_quest_state(self, chat_id: int):
        progress = self.current_stage[chat_id] / self.total_stages[chat_id]
        if progress < 0.33:
            self.quest_state[chat_id] = "beginning"
        elif progress < 0.66:
            self.quest_state[chat_id] = "middle"
        else:
            self.quest_state[chat_id] = "end"

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
    await zen_quest.interrupt_quest(update, context)

async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await zen_quest.handle_hint(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    if not zen_quest.quest_active.get(chat_id, False):
        if chat_type == 'private' and update.message.text and not update.message.text.startswith('/'):
            await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
        return

    if zen_quest.in_combat.get(chat_id, False):
        await zen_quest.handle_combat_input(update, context, update.message.text)
    else:
        await zen_quest.handle_input(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")
    if update and update.effective_message:
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
    else:
        await query.answer("Unknown action.")

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Set up the database
    setup_database()

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()