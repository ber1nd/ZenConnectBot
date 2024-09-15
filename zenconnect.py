import asyncio
import os
import logging
import random
import mysql.connector # Ensure this is installed
from mysql.connector import errorcode
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import openai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai.api_key = os.getenv("API_KEY")

# Rate limiting parameters
RATE_LIMIT = 5  # Number of messages
RATE_TIME_WINDOW = 60  # Time window in seconds
user_message_times = defaultdict(list)

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

class ZenQuest:
    def __init__(self):
        self.quest_active = {}
        self.characters = {}
        self.current_stage = {}
        self.total_stages = {}
        self.current_scene = {}
        self.in_combat = {}
        self.quest_state = {}
        self.quest_goal = {}
        self.player_karma = {}
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
        user_id = update.effective_user.id
        
        if self.quest_active.get(user_id, False):
            await update.message.reply_text(
                "You are already on a quest. Use /status to check your progress or /interrupt to end your current quest."
            )
            return

        # Offer character class selection
        keyboard = [[
            InlineKeyboardButton(class_name, callback_data=f"class_{class_name}")
            for class_name in self.character_classes.keys()
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Choose your character class to begin your Zen journey:",
            reply_markup=reply_markup
        )

    async def select_character_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        class_name = query.data.split('_')[1]

        self.characters[user_id] = self.character_classes[class_name]
        self.quest_active[user_id] = True
        self.current_stage[user_id] = 0
        self.total_stages[user_id] = random.randint(10, 20)  # Adjusted for demo purposes
        self.quest_state[user_id] = "beginning"
        self.in_combat[user_id] = False
        self.player_karma[user_id] = 100

        self.quest_goal[user_id] = await self.generate_quest_goal(class_name)
        self.current_scene[user_id] = await self.generate_initial_scene(
            self.quest_goal[user_id], class_name
        )

        start_message = (
            f"Your quest as a {class_name} begins!\n\n"
            f"{self.quest_goal[user_id]}\n\n"
            f"{self.current_scene[user_id]}"
        )
        await query.edit_message_text(start_message)

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        # Rate limiting
        current_time = datetime.now()
        user_times = user_message_times[user_id]
        user_times = [t for t in user_times if (current_time - t).seconds < RATE_TIME_WINDOW]
        user_times.append(current_time)
        user_message_times[user_id] = user_times
        if len(user_times) > RATE_LIMIT:
            await update.message.reply_text("You're sending messages too quickly. Please slow down.")
            return

        if update.message and update.message.text:
            user_input = update.message.text.strip()
        else:
            return  # Non-text message received

        if not self.quest_active.get(user_id, False):
            await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
            return

        if self.in_combat.get(user_id, False):
            await update.message.reply_text("You're in combat! Use the combat options provided.")
            return

        if user_id in self.riddles and self.riddles[user_id]['active']:
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
        action_result = await self.process_action(user_id, user_input)
        await self.send_message(update, action_result)

        # Handle special events
        if "QUEST_COMPLETE" in action_result:
            await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
        elif "QUEST_FAIL" in action_result:
            await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
        elif "MORAL_CHOICE" in action_result:
            await self.present_moral_choice(update, context)
        elif "COMBAT_START" in action_result:
            await self.initiate_combat(update, context)
        elif "RIDDLE_START" in action_result:
            await self.initiate_riddle(update, context)
        else:
            await self.progress_story(update, context, user_input)

    async def send_message(self, update: Update, text: str, reply_markup=None):
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def process_action(self, user_id: int, user_input: str):
        character = self.characters[user_id]
        current_scene = self.current_scene[user_id]
        quest_state = self.quest_state[user_id]
        karma = self.player_karma[user_id]

        if self.is_action_unfeasible(user_input):
            return "That action is not possible in this realm. Please choose a different path."
        elif self.is_action_failure(user_input):
            return "QUEST_FAIL: Your choice leads to an unfortunate end."

        prompt = f"""
        Current scene: {current_scene}
        Character class: {character.name}
        Quest state: {quest_state}
        Player karma: {karma}
        Player action: "{user_input}"

        Generate a brief result (3-4 sentences) for the player's action. Include:
        1. The immediate outcome of the action
        2. Any changes to the environment or situation
        3. A new challenge, opportunity, or decision point
        4. A subtle Zen teaching or insight related to the action and its consequences

        If the action leads to combat, include "COMBAT_START" in the response.
        If the action triggers a riddle or puzzle, include "RIDDLE_START" in the response.
        If the action completes the quest, include "QUEST_COMPLETE" in the response.
        If the action fails the quest, include "QUEST_FAIL" in the response.
        If the action presents a significant moral choice, include "MORAL_CHOICE" in the response.

        Consider the player's karma when determining outcomes. Lower karma should increase the likelihood of negative consequences.

        Keep the response under 150 words.
        """
        action_result = await self.generate_response(prompt)

        # Update karma based on the action
        karma_change = await self.evaluate_karma_change(user_input, action_result)
        self.player_karma[user_id] = max(0, min(100, self.player_karma[user_id] + karma_change))

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
        user_id = update.effective_user.id
        character = self.characters[user_id]

        next_scene = await self.generate_next_scene(user_id, user_input)
        self.current_scene[user_id] = next_scene

        # Check for special events in the next scene
        if "COMBAT_START" in next_scene:
            await self.initiate_combat(update, context)
        elif "RIDDLE_START" in next_scene:
            await self.initiate_riddle(update, context)
        elif "QUEST_COMPLETE" in next_scene:
            await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
        elif "QUEST_FAIL" in next_scene:
            await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
        else:
            self.current_stage[user_id] += 1
            await self.update_quest_state(user_id)
            await self.send_scene(update, context)

        # Update karma and check for quest failure due to low karma
        self.player_karma[user_id] = max(0, min(100, self.player_karma[user_id] + random.randint(-3, 3)))
        if character.current_hp <= 0:
            await self.end_quest(update, context, victory=False, reason="Your life force has been depleted.")
        elif self.player_karma[user_id] <= 0:
            await self.end_quest(update, context, victory=False, reason="Your karma has fallen too low.")

    async def generate_next_scene(self, user_id: int, user_input: str):
        character = self.characters[user_id]
        player_karma = self.player_karma[user_id]
        current_stage = self.current_stage[user_id]
        total_stages = self.total_stages[user_id]
        progress = (current_stage / total_stages) * 100

        event_type = random.choices(
            ["normal", "challenge", "reward", "meditation", "npc_encounter", "moral_dilemma",
             "spiritual_trial", "natural_obstacle", "mystical_phenomenon", "combat", "riddle"],  # Added missing items
            weights=[30, 15, 5, 5, 5, 10, 5, 5, 5, 10, 5],
            k=1
        )[0]

        prompt = f"""
        Previous scene: {self.current_scene[user_id]}
        User's action: "{user_input}"
        Character Class: {character.name}
        Character strengths: {', '.join(character.strengths)}
        Character weaknesses: {', '.join(character.weaknesses)}
        Current quest state: {self.quest_state[user_id]}
        Quest goal: {self.quest_goal[user_id]}
        Player karma: {player_karma}
        Current stage: {current_stage}
        Total stages: {total_stages}
        Progress: {progress:.2f}%
        Event type: {event_type}

        Generate the next scene of the Zen-themed quest. Include:
        1. A vivid description of the new situation or environment (2-3 sentences)
        2. The outcome of the user's previous action and its impact (1-2 sentences)
        3. A new challenge, obstacle, or decision point (1-2 sentences)
        4. Three distinct, non-trivial choices for the player (1 sentence each)
        5. A brief Zen-like insight relevant to the situation (1 sentence)

        Ensure the scene:
        - Progresses the quest towards its goal, reflecting the current progress
        - Presents a real possibility of failure or setback
        - Incorporates the character's class abilities, strengths, or weaknesses
        - Maintains a balance between physical adventure and spiritual growth
        - Incorporates Zen teachings or principles subtly

        If the event type is "combat", include "COMBAT_START" in the scene.
        If it's a riddle event, include "RIDDLE_START" in the scene.
        If it's a moral dilemma, include "MORAL_CHOICE" in the scene.
        If the quest is nearing completion (progress > 90%), hint at a final challenge.

        Keep the total response under 200 words.
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
        user_id = update.effective_user.id
        riddle = self.riddles[user_id]
        
        if user_input.lower() == riddle['answer'].lower():
            await self.send_message(update, "Correct! You have solved the riddle.")
            self.riddles[user_id]['active'] = False
            await self.progress_story(update, context, "solved riddle")
        else:
            await self.send_message(update, "That's not correct. Try again or use /hint for a clue.")

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
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            await self.send_message(update, "You must be on a quest to meditate.")
            return

        character = self.characters[user_id]
        meditation_result = await self.generate_response(
            f"Generate a brief meditation outcome for a {character.name} in their current quest state. "
            "Include a small health and energy boost, and a Zen insight. Keep it under 100 words."
        )
        
        character.current_hp = min(character.max_hp, character.current_hp + 10)
        character.current_energy = min(character.max_energy, character.current_energy + 10)
        
        await self.send_message(update, meditation_result)

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        character = self.characters[user_id]
        progress = (self.current_stage[user_id] / self.total_stages[user_id]) * 100
        
        status_message = (
            f"Quest Progress: {progress:.1f}%\n"
            f"Character: {character.name}\n"
            f"HP: {character.current_hp}/{character.max_hp}\n"
            f"Energy: {character.current_energy}/{character.max_energy}\n"
            f"Karma: {self.player_karma[user_id]}\n"
            f"Current State: {self.quest_state[user_id].capitalize()}\n"
            f"Goal: {self.quest_goal[user_id]}"
        )
        await self.send_message(update, status_message)

    async def interrupt_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            await self.send_message(update, "You are not currently on a quest.")
            return

        await self.end_quest(update, context, victory=False, reason="Quest interrupted by user.")

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            return

        character = self.characters[user_id]
        progress = (self.current_stage[user_id] / self.total_stages[user_id]) * 100
        
        end_message = (
            f"Your quest has ended.\n"
            f"Reason: {reason}\n"
            f"Victory: {'Yes' if victory else 'No'}\n"
            f"Progress: {progress:.1f}%\n"
            f"Final Karma: {self.player_karma[user_id]}\n"
            f"Character: {character.name}\n"
            f"HP: {character.current_hp}/{character.max_hp}\n"
            f"Energy: {character.current_energy}/{character.max_energy}\n"
        )
        
        await self.send_message(update, end_message)
        
        # Reset user's quest data
        self.quest_active[user_id] = False
        self.characters.pop(user_id, None)
        self.current_stage.pop(user_id, None)
        self.total_stages.pop(user_id, None)
        self.current_scene.pop(user_id, None)
        self.in_combat.pop(user_id, None)
        self.quest_state.pop(user_id, None)
        self.quest_goal.pop(user_id, None)
        self.player_karma.pop(user_id, None)
        self.current_opponent.pop(user_id, None)
        self.riddles.pop(user_id, None)
        self.moral_dilemmas.pop(user_id, None)

    async def present_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        prompt = (
            "Generate a moral dilemma for the player's Zen quest. "
            "Present two choices, each with potential consequences. "
            "Keep the description and choices under 150 words total."
        )
        dilemma = await self.generate_response(prompt)
        self.moral_dilemmas[user_id] = {'active': True, 'dilemma': dilemma}
        await self.send_message(update, dilemma)

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        
        opponent_prompt = f"Generate a challenging opponent for a {character.name} in a Zen-themed quest. Include name, brief description, and two unique abilities. Keep it under 50 words."
        opponent = await self.generate_response(opponent_prompt)
        
        self.current_opponent[user_id] = opponent
        self.in_combat[user_id] = True
        
        combat_start_message = (
            f"You encounter {opponent}\n"
            f"Prepare for combat!\n"
            f"Your options:\n"
            f"1. Attack\n"
            f"2. Defend\n"
            f"3. Use ability\n"
            f"4. Attempt to resolve peacefully"
        )
        
        keyboard = [
            [InlineKeyboardButton("Attack", callback_data="combat_attack"),
             InlineKeyboardButton("Defend", callback_data="combat_defend")],
            [InlineKeyboardButton("Use ability", callback_data="combat_ability"),
             InlineKeyboardButton("Resolve peacefully", callback_data="combat_peace")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await self.send_message(update, combat_start_message, reply_markup=reply_markup)

    async def handle_combat_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        opponent = self.current_opponent[user_id]
        
        prompt = f"""
        Character: {character.name} (HP: {character.current_hp}/{character.max_hp}, Energy: {character.current_energy}/{character.max_energy})
        Opponent: {opponent}
        Action: {action}

        Generate a brief combat result (2-3 sentences) based on the character's action.
        Include any changes to HP or energy, and the opponent's reaction.
        If the combat ends, state whether the character won or lost.
        Keep the response under 100 words.
        """
        
        combat_result = await self.generate_response(prompt)
        await self.send_message(update, combat_result)
        
        if "won" in combat_result.lower() or "lost" in combat_result.lower():
            self.in_combat[user_id] = False
            self.current_opponent.pop(user_id, None)
            await self.progress_story(update, context, "combat ended")
        else:
            # Present combat options again
            keyboard = [
                [InlineKeyboardButton("Attack", callback_data="combat_attack"),
                 InlineKeyboardButton("Defend", callback_data="combat_defend")],
                [InlineKeyboardButton("Use ability", callback_data="combat_ability"),
                 InlineKeyboardButton("Resolve peacefully", callback_data="combat_peace")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.send_message(update, "What will you do next?", reply_markup=reply_markup)

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        prompt = "Generate a Zen-themed riddle with its answer. The riddle should be challenging but solvable. Keep it under 50 words."
        riddle_and_answer = await self.generate_response(prompt)
        
        # Assuming the response is in the format "Riddle: ... Answer: ..."
        riddle, answer = riddle_and_answer.split("Answer:")
        riddle = riddle.replace("Riddle:", "").strip()
        answer = answer.strip()
        
        self.riddles[user_id] = {'active': True, 'riddle': riddle, 'answer': answer}
        await self.send_message(update, f"Solve this riddle:\n\n{riddle}")

    def is_action_unfeasible(self, action):
        return any(word in action.lower() for word in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(word in action.lower() for word in self.failure_actions)

    async def generate_response(self, prompt, max_tokens=150):
        try:
            client = openai.OpenAI(api_key=os.getenv("API_KEY"))
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model="gpt-4o-mini",  # or "gpt-3.5-turbo", depending on your preference and access
                    messages=[
                        {"role": "system", "content": "You are a wise Zen master guiding a quest. Maintain realism for human capabilities. Actions should have logical consequences. Provide challenging moral dilemmas and opportunities for growth."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=max_tokens,
                    temperature=0.7
                )
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

    async def send_scene(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        scene = self.current_scene[user_id]
        await self.send_message(update, scene)

    async def update_quest_state(self, user_id: int):
        progress = self.current_stage[user_id] / self.total_stages[user_id]
        if progress < 0.33:
            self.quest_state[user_id] = "beginning"
        elif progress < 0.66:
            self.quest_state[user_id] = "middle"
        else:
            self.quest_state[user_id] = "end"

    async def handle_hint(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in self.riddles and self.riddles[user_id]['active']:
            hint = await self.generate_response(f"Generate a hint for the riddle: {self.riddles[user_id]['riddle']}. Keep it subtle and under 30 words.")
            await self.send_message(update, f"Hint: {hint}")
        else:
            await self.send_message(update, "There is no active riddle to hint for.")

    async def handle_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str):
        user_id = update.effective_user.id
        if user_id in self.moral_dilemmas and self.moral_dilemmas[user_id]['active']:
            consequence = await self.generate_response(f"Generate a consequence for the moral choice: {choice}. Relate it to the dilemma: {self.moral_dilemmas[user_id]['dilemma']}. Keep it under 100 words.")
            self.moral_dilemmas[user_id]['active'] = False
            await self.send_message(update, consequence)
            await self.progress_story(update, context, f"made moral choice: {choice}")
        else:
            await self.send_message(update, "There is no active moral dilemma to respond to.")

    async def use_ability(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if self.in_combat.get(user_id, False):
            character = self.characters[user_id]
            abilities = ", ".join(character.abilities)
            await self.send_message(update, f"Choose an ability to use: {abilities}")
            # You'd need to implement a way for the user to select an ability,
            # perhaps using inline keyboard buttons
        else:
            await self.send_message(update, "You're not in combat right now.")

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await zen_quest.handle_combat_input(update, context, data)
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_error_handler(error_handler)

    # Set up the database
    setup_database()

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()