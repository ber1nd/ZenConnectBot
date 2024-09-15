import asyncio
import os
import logging
import random
import re
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
            else:
                await update.message.reply_text(
                    "Unknown command. Available commands: /meditate, /status, /interrupt"
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
             "spiritual_trial", "natural_obstacle", "mystical_phenomenon", "combat", "riddle"],
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

    # Implement other methods as needed (combat, riddles, etc.)

    def is_action_unfeasible(self, action):
        return any(word in action.lower() for word in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(word in action.lower() for word in self.failure_actions)

    async def generate_response(self, prompt, max_tokens=150):
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                openai.ChatCompletion.create,
                {
                    "model": "gpt-3.5-turbo",
                    "messages": [
                        {"role": "system", "content": "You are a wise Zen master guiding a quest. Maintain realism for human capabilities. Actions should have logical consequences. Provide challenging moral dilemmas and opportunities for growth."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.7
                }
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

    # Other methods (combat, riddles, meditation, etc.) should be implemented similarly.

    # For brevity, I'm not including all methods here, but ensure they're properly implemented.

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

    async def present_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_scene = self.current_scene[user_id]
        
        prompt = f"""
        Based on the current scene:
        "{current_scene}"

        Generate a moral dilemma for the player. Include:
        1. A brief description of the situation (2-3 sentences)
        2. Two distinct choices, each with potential positive and negative consequences
        3. How each choice relates to Zen principles or teachings

        Format the response as:
        Situation: [situation description]
        Choice A: [description of choice A]
        Choice B: [description of choice B]
        """
        dilemma = await self.generate_response(prompt)
        
        # Parse the dilemma
        parts = dilemma.split('\n')
        situation = parts[0].split(': ', 1)[1]
        choice_a = parts[1].split(': ', 1)[1]
        choice_b = parts[2].split(': ', 1)[1]
        
        # Present the choice to the player
        message = f"{situation}\n\nYou must choose:\nA) {choice_a}\nB) {choice_b}\n\nReply with 'A' or 'B' to make your choice."
        await update.message.reply_text(message)
        
        # Store the dilemma for later reference
        self.moral_dilemmas[user_id] = {
            'situation': situation,
            'choice_a': choice_a,
            'choice_b': choice_b
        }

    async def handle_moral_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        choice = update.message.text.upper()
        
        if choice not in ['A', 'B']:
            await update.message.reply_text("Please choose 'A' or 'B' for your moral decision.")
            return
        
        dilemma = self.moral_dilemmas.get(user_id)
        if not dilemma:
            await update.message.reply_text("There is no active moral dilemma to resolve.")
            return
        
        chosen_option = dilemma['choice_a'] if choice == 'A' else dilemma['choice_b']
        
        prompt = f"""
        Situation: {dilemma['situation']}
        Player's choice: {chosen_option}

        Generate the outcome of this moral choice (3-4 sentences). Include:
        1. The immediate consequences of the choice
        2. How it affects the player's quest and surrounding environment
        3. A Zen teaching or insight related to this decision
        4. A hint at future implications of this choice

        Also, provide a karma change value between -10 and 10 based on the choice and its alignment with Zen principles.
        """
        outcome = await self.generate_response(prompt)
        
        # Extract karma change from the outcome
        karma_change_match = re.search(r'Karma change: (-?\d+)', outcome)
        karma_change = int(karma_change_match.group(1)) if karma_change_match else 0
        
        # Update player's karma
        self.player_karma[user_id] = max(0, min(100, self.player_karma[user_id] + karma_change))
        
        # Send the outcome to the player
        await update.message.reply_text(outcome)
        
        # Clear the stored dilemma
        del self.moral_dilemmas[user_id]
        
        # Continue the quest
        await self.progress_story(update, context, f"made a moral choice: {chosen_option}")

    async def generate_action_result(self, user_id: int, user_input: str):
        character = self.characters[user_id]
        current_scene = self.current_scene[user_id]
        quest_state = self.quest_state[user_id]
        
        prompt = f"""
        Current scene: {current_scene}
        Character class: {character.class_name}
        Quest state: {quest_state}
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

        Keep the response under 100 words.
        """
        return await self.generate_response(prompt)

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id

        try:
            morality_check = await self.check_action_morality(user_input)
            
            if morality_check['is_immoral']:
                consequence = await self.generate_severe_consequence(morality_check['reason'], self.current_scene[user_id])
                await update.message.reply_text(consequence['description'])
                
                self.player_karma[user_id] = max(0, self.player_karma[user_id] - 20)

                if consequence['type'] == 'quest_fail':
                    await self.end_quest(update, context, victory=False, reason=consequence['description'])
                    return
                elif consequence['type'] == 'combat':
                    await self.initiate_combat(update, context)
                    return
                elif consequence['type'] == 'affliction':
                    await self.apply_affliction(update, context, consequence['description'])

            next_scene = await self.generate_next_scene(user_id, user_input)
            self.current_scene[user_id] = next_scene

            if "HP_CHANGE:" in next_scene:
                hp_change_str = next_scene.split("HP_CHANGE:")[1].split()[0]
                hp_change = int(hp_change_str)
                self.characters[user_id].current_hp = max(0, min(self.characters[user_id].max_hp, self.characters[user_id].current_hp + hp_change))

            if "COMBAT_START" in next_scene:
                await self.initiate_combat(update, context)
                return
            elif "RIDDLE_START" in next_scene:
                await self.initiate_riddle(update, context)
                return
            elif "QUEST_COMPLETE" in next_scene:
                await self.end_quest(update, context, victory=True, reason="You have completed your journey!")
                return
            elif "QUEST_FAIL" in next_scene:
                await self.end_quest(update, context, victory=False, reason="Your quest has come to an unfortunate end.")
                return
            else:
                self.current_stage[user_id] += 1
                await self.update_quest_state(user_id)
                await self.send_scene(update, context)

            self.player_karma[user_id] = max(0, min(100, self.player_karma[user_id] + random.randint(-3, 3)))

            if self.characters[user_id].current_hp <= 0:
                await self.end_quest(update, context, victory=False, reason="Your life force has been depleted. Your journey ends here.")
            elif self.player_karma[user_id] < 10:
                await self.end_quest(update, context, victory=False, reason="Your actions have led you far astray from the path of enlightenment.")

        except Exception as e:
            logger.error(f"Error progressing story: {e}")
            await update.message.reply_text("An error occurred while processing your action. Please try again.")

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if not self.quest_active.get(user_id):
            await update.message.reply_text("You are not currently on an active quest. Use /zenquest to start a new journey.")
            return

        if self.in_combat.get(user_id, False):
            await update.message.reply_text("You are already engaged in combat. Please finish your current battle before starting a new one.")
            return

        self.in_combat[user_id] = True
        opponent = await self.generate_opponent(self.current_scene[user_id])
        self.current_opponent[user_id] = opponent

        combat_intro = await self.generate_combat_intro(self.current_scene[user_id], opponent['name'])
        await update.message.reply_text(combat_intro)

        await self.send_combat_options(update, context)

    async def send_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        opponent = self.current_opponent[user_id]
        
        keyboard = [
            [InlineKeyboardButton("Attack", callback_data="combat_attack")],
            [InlineKeyboardButton("Defend", callback_data="combat_defend")]
        ]
        for ability in character.abilities:
            keyboard.append([InlineKeyboardButton(ability, callback_data=f"combat_{ability}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = f"Your HP: {character.current_hp}/{character.max_hp}\n"
        status_message += f"Your Energy: {character.current_energy}/{character.max_energy}\n"
        status_message += f"Opponent HP: {opponent['hp']}/{opponent['max_hp']}\n"
        status_message += "Choose your combat action:"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(status_message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(status_message, reply_markup=reply_markup)

    async def handle_combat_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        action = query.data.split('_')[1]

        result = await self.resolve_combat_action(user_id, action)
        await query.edit_message_text(text=result)

        if self.characters[user_id].current_hp <= 0:
            await self.end_quest(update, context, victory=False, reason="You have been defeated in combat.")
        elif self.current_opponent[user_id]['hp'] <= 0:
            victory_message = await self.generate_combat_victory(self.current_scene[user_id], self.current_opponent[user_id]['name'])
            await update.message.reply_text(victory_message)
            self.in_combat[user_id] = False
            await self.progress_story(update, context, "defeated the opponent")
        else:
            await self.send_combat_options(update, context)

    async def resolve_combat_action(self, user_id: int, action: str):
        character = self.characters[user_id]
        opponent = self.current_opponent[user_id]

        if action in character.abilities:
            ability = action
            energy_cost = 20  # Example energy cost
            if character.current_energy >= energy_cost:
                character.current_energy -= energy_cost
                damage = random.randint(15, 25)
                opponent['hp'] -= damage
                result = f"You use {ability}, dealing {damage} damage to the opponent."
            else:
                result = f"You don't have enough energy to use {ability}."
        elif action == "attack":
            damage = random.randint(10, 20)
            opponent['hp'] -= damage
            result = f"You attack and deal {damage} damage to the opponent."
        elif action == "defend":
            character.current_hp = min(character.max_hp, character.current_hp + 10)
            character.current_energy = min(character.max_energy, character.current_energy + 15)
            result = f"You defend, recovering 10 HP and 15 energy."
        else:
            result = "Invalid action."

        # Opponent's turn
        if opponent['hp'] > 0:
            opponent_damage = random.randint(5, 15)
            character.current_hp = max(0, character.current_hp - opponent_damage)
            result += f"\n\nThe opponent strikes back, dealing {opponent_damage} damage to you."

        if opponent['hp'] <= 0:
            result += f"\n\nYou have defeated {opponent['name']}!"
        elif character.current_hp <= 0:
            result += "\n\nYou have been defeated in combat."

        result += f"\n\nYour HP: {character.current_hp}/{character.max_hp}"
        result += f"\nYour Energy: {character.current_energy}/{character.max_energy}"
        result += f"\nOpponent HP: {opponent['hp']}/{opponent['max_hp']}"

        return result

    async def generate_opponent(self, current_scene):
        prompt = f"""
        Based on the current scene:
        "{current_scene}"
        
        Generate a suitable opponent for combat. Provide:
        1. Name: A name for the opponent
        2. Description: A brief description of the opponent (1-2 sentences)
        3. Difficulty: Easy, Medium, or Hard
        4. HP: A number between 50 and 200 based on the difficulty
        5. Abilities: A list of 2-3 special abilities the opponent can use
        
        The opponent should fit thematically with the current scene and the overall Zen quest theme.
        """
        response = await self.generate_response(prompt)
        opponent_data = {}
        try:
            for line in response.split('\n'):
                key, value = line.split(': ', 1)
                opponent_data[key.lower()] = value
            opponent_data['max_hp'] = int(opponent_data['hp'])
            opponent_data['hp'] = int(opponent_data['hp'])
            opponent_data['energy'] = 100  # Add initial energy
            opponent_data['max_energy'] = 100  # Add max energy
        except (ValueError, KeyError) as e:
            logger.error(f"Error parsing opponent data: {e}")
            return None
        return opponent_data

    async def handle_combat_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        opponent = self.current_opponent[user_id]

        if user_input.startswith('combat_'):
            action = user_input.split('_')[1]
        else:
            await self.send_message(update, "Invalid combat action. Please use the provided buttons.")
            await self.send_combat_options(update, context)
            return

        # Process player's action
        player_result = await self.resolve_combat_action(character, opponent, action)
        
        # Process opponent's action
        opponent_action = self.generate_opponent_action(opponent)
        opponent_result = await self.resolve_combat_action(opponent, character, opponent_action, is_opponent=True)

        # Combine results
        combat_result = f"{player_result}\n\n{opponent_result}"

        # Check for combat end conditions
        if character.current_hp <= 0:
            combat_result += "\n\nYou have been defeated in combat."
            await self.send_message(update, combat_result)
            await self.end_quest(update, context, victory=False, reason="You have fallen in battle.")
        elif opponent['hp'] <= 0:
            combat_result += f"\n\nYou have defeated {opponent['name']}!"
            await self.send_message(update, combat_result)
            self.in_combat[user_id] = False
            victory_message = await self.generate_combat_victory(self.current_scene[user_id], opponent['name'])
            await self.send_message(update, victory_message)
            await self.progress_story(update, context, f"defeated {opponent['name']} in combat")
        else:
            await self.send_message(update, combat_result)
            await self.send_combat_options(update, context)

    async def resolve_combat_action(self, attacker, defender, action, is_opponent=False):
        if isinstance(attacker, dict):
            attacker_abilities = attacker.get('abilities', [])
            attacker_energy = attacker.get('energy', 0)
            attacker_max_energy = attacker.get('max_energy', 100)
        else:
            attacker_abilities = attacker.abilities
            attacker_energy = attacker.current_energy
            attacker_max_energy = attacker.max_energy

        if action in attacker_abilities:
            ability = action
            energy_cost = 20  # Example energy cost
            if attacker_energy >= energy_cost:
                if isinstance(attacker, dict):
                    attacker['energy'] -= energy_cost
                else:
                    attacker.current_energy -= energy_cost
                
                # Class-specific ability effects
                if ability == "Meditate":
                    heal_amount = random.randint(10, 20)
                    attacker.current_hp = min(attacker.max_hp, attacker.current_hp + heal_amount)
                    result = f"You meditate, recovering {heal_amount} HP."
                elif ability == "Katana Slash":
                    damage = random.randint(20, 30)
                    defender['hp'] -= damage
                    result = f"You perform a powerful Katana Slash, dealing {damage} damage."
                elif ability == "Nature's Wrath":
                    damage = random.randint(15, 25)
                    defender['hp'] -= damage
                    result = f"You summon Nature's Wrath, dealing {damage} damage and weakening the opponent."
                else:
                    # Default ability effect
                    damage = random.randint(15, 25)
                    defender['hp'] -= damage
                    result = f"You use {ability}, dealing {damage} damage."
            else:
                result = f"You don't have enough energy to use {ability}."
        elif action == "attack":
            damage = random.randint(10, 20)
            defender['hp'] = max(0, defender['hp'] - damage)
            result = f"{'The opponent attacks' if is_opponent else 'You attack'}, dealing {damage} damage."
        elif action == "defend":
            heal_amount = random.randint(5, 15)
            energy_restore = random.randint(10, 20)
            if isinstance(attacker, dict):
                attacker['hp'] = min(attacker['max_hp'], attacker['hp'] + heal_amount)
                attacker['energy'] = min(attacker['max_energy'], attacker['energy'] + energy_restore)
            else:
                attacker.current_hp = min(attacker.max_hp, attacker.current_hp + heal_amount)
                attacker.current_energy = min(attacker.max_energy, attacker.current_energy + energy_restore)
            result = f"{'The opponent defends' if is_opponent else 'You defend'}, recovering {heal_amount} HP and {energy_restore} energy."
        else:
            result = "Invalid action."

        return result

    def generate_opponent_action(self, opponent):
        actions = ['attack', 'defend'] + opponent.get('abilities', [])
        return random.choice(actions)

    async def send_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        opponent = self.current_opponent[user_id]
        
        keyboard = [
            [InlineKeyboardButton("Attack", callback_data="combat_attack")],
            [InlineKeyboardButton("Defend", callback_data="combat_defend")]
        ]
        for ability in character.abilities:
            keyboard.append([InlineKeyboardButton(ability, callback_data=f"combat_{ability}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = f"Your HP: {character.current_hp}/{character.max_hp}\n"
        status_message += f"Your Energy: {character.current_energy}/{character.max_energy}\n"
        status_message += f"Opponent ({opponent['name']}) HP: {opponent['hp']}/{opponent['max_hp']}\n"
        status_message += "Choose your combat action:"
        
        await self.send_message(update, status_message, reply_markup=reply_markup)

    async def generate_combat_victory(self, current_scene, opponent_name):
        prompt = f"""
        Based on the current scene:
        "{current_scene}"
        
        Generate a brief victory message (3-4 sentences) for defeating {opponent_name} in combat. The message should:
        1. Describe the final moments of the battle
        2. Reflect on the lessons learned or wisdom gained from the encounter
        3. Hint at how this victory contributes to the overall quest
        4. Include a small Zen insight related to the nature of conflict and resolution
        """
        return await self.generate_response(prompt)