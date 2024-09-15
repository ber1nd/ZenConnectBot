import os
import asyncio
import logging
import random
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from openai import AsyncOpenAI
import mysql.connector
from mysql.connector import Error, errorcode
from aiohttp import web
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

# Rate limiting
RATE_LIMIT = 5
rate_limit_dict = defaultdict(list)

def get_db_connection():
    try:
        connection = mysql.connector.connect(
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            host=os.getenv("MYSQLHOST"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQLPORT", 3306)),
            raise_on_warnings=True
        )
        logger.info("Database connection established successfully.")
        return connection
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            logger.error("Something is wrong with your user name or password.")
        elif err.errno == errorcode.ER_BAD_DB_ERROR:
            logger.error("Database does not exist.")
        else:
            logger.error(err)
        return None

def setup_database():
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
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
                
                # Create user_memory table
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT,
                    group_id BIGINT,
                    memory TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                
                # Create subscriptions table
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
                
                connection.commit()
                logger.info("Database setup completed successfully.")
        except mysql.connector.Error as e:
            logger.error(f"Error setting up database: {e}")
        finally:
            connection.close()
    else:
        logger.error("Failed to connect to the database for setup.")

class Character:
    def __init__(self, class_name, hp, energy, abilities):
        self.class_name = class_name
        self.max_hp = hp
        self.current_hp = hp
        self.max_energy = energy
        self.current_energy = energy
        self.abilities = abilities

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
            "Monk": Character("Monk", 100, 100, ["Meditate", "Chi Strike", "Healing Touch", "Spirit Ward"]),
            "Samurai": Character("Samurai", 120, 80, ["Katana Slash", "Bushido Stance", "Focused Strike", "Honor Guard"]),
            "Shaman": Character("Shaman", 90, 110, ["Nature's Wrath", "Spirit Link", "Elemental Shield", "Ancestral Guidance"])
        }

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        if self.quest_active.get(user_id, False):
            await update.message.reply_text("You are already on a quest. Use /status to check your progress or /interrupt to end your current quest.")
            return

        # Offer character class selection
        keyboard = [[InlineKeyboardButton(class_name, callback_data=f"class_{class_name}") for class_name in self.character_classes.keys()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Choose your character class to begin your Zen journey:", reply_markup=reply_markup)

    async def select_character_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        class_name = query.data.split('_')[1]

        self.characters[user_id] = self.character_classes[class_name]
        self.quest_active[user_id] = True
        self.current_stage[user_id] = 0
        self.total_stages[user_id] = random.randint(30, 50)
        self.quest_state[user_id] = "beginning"
        self.in_combat[user_id] = False
        self.player_karma[user_id] = 100

        self.quest_goal[user_id] = await self.generate_quest_goal()
        self.current_scene[user_id] = await self.generate_initial_scene(self.quest_goal[user_id], class_name)

        start_message = f"Your quest as a {class_name} begins!\n\n{self.quest_goal[user_id]}\n\n{self.current_scene[user_id]}"
        await query.edit_message_text(start_message)

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        user_input = update.message.text.lower()

        if not self.quest_active.get(user_id, False):
            await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")
            return

        if self.in_combat.get(user_id, False):
            if user_input == '/surrender':
                await self.surrender(update, context)
            else:
                await update.message.reply_text("You are in combat. Use the provided buttons to choose your action.")
            return

        if user_id in self.riddles and self.riddles[user_id]['active']:
            await self.handle_riddle_input(update, context, user_input)
            return

        if any(word in user_input for word in ["hurt myself", "self-harm", "suicide", "kill myself", "cut"]):
            await self.handle_self_harm(update, context, user_input)
            return

        if self.is_action_unfeasible(user_input):
            await self.handle_unfeasible_action(update, context)
            return
        elif self.is_action_failure(user_input):
            await self.handle_failure_action(update, context)
            return

        await self.progress_story(update, context, user_input)

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

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
        self.current_opponent[user_id] = await self.generate_opponent()

        combat_intro = f"You encounter {self.current_opponent[user_id]}! Prepare for battle!"
        await update.message.reply_text(combat_intro)

        await self.send_combat_options(update, context)

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
        elif not self.in_combat[user_id]:
            await self.progress_story(update, context, "defeated the opponent")
        else:
            await self.send_combat_options(update, context)

    async def resolve_combat_action(self, user_id: int, action: str):
        character = self.characters[user_id]
        opponent_hp = 100  # Simplified opponent HP

        if action in character.abilities:
            ability = action
            energy_cost = 20  # Example energy cost
            if character.current_energy >= energy_cost:
                character.current_energy -= energy_cost
                damage = random.randint(15, 25)
                opponent_hp -= damage
                result = f"You use {ability}, dealing {damage} damage to the opponent."
            else:
                result = f"You don't have enough energy to use {ability}."
        elif action == "attack":
            damage = random.randint(10, 20)
            opponent_hp -= damage
            result = f"You attack and deal {damage} damage to the opponent."
        elif action == "defend":
            character.current_hp = min(character.max_hp, character.current_hp + 10)
            character.current_energy = min(character.max_energy, character.current_energy + 15)
            result = f"You defend, recovering 10 HP and 15 energy."
        else:
            result = "Invalid action."

        # Opponent's turn
        if opponent_hp > 0:
            opponent_damage = random.randint(5, 15)
            character.current_hp = max(0, character.current_hp - opponent_damage)
            result += f"\n\nThe opponent strikes back, dealing {opponent_damage} damage to you."

        if opponent_hp <= 0:
            self.in_combat[user_id] = False
            result += f"\n\nYou have defeated {self.current_opponent[user_id]}!"
        elif character.current_hp <= 0:
            result += "\n\nYou have been defeated in combat."

        result += f"\n\nYour HP: {character.current_hp}/{character.max_hp}"
        result += f"\nYour Energy: {character.current_energy}/{character.max_energy}"

        return result

    async def send_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        character = self.characters[user_id]
        
        keyboard = [
            [InlineKeyboardButton("Attack", callback_data="combat_attack")],
            [InlineKeyboardButton("Defend", callback_data="combat_defend")]
        ]
        for ability in character.abilities:
            keyboard.append([InlineKeyboardButton(ability, callback_data=f"combat_{ability}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = f"Your HP: {character.current_hp}/{character.max_hp}\n"
        status_message += f"Your Energy: {character.current_energy}/{character.max_energy}\n"
        status_message += "Choose your combat action:"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(status_message, reply_markup=reply_markup)
        else:
            await update.message.reply_text(status_message, reply_markup=reply_markup)

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        riddle = await self.generate_riddle()
        self.riddles[user_id] = {'riddle': riddle['riddle'], 'answer': riddle['answer'], 'active': True, 'attempts': 0}
        await update.message.reply_text(f"Solve this riddle:\n\n{riddle['riddle']}\n\nYou have 3 attempts. Type your answer.")

    async def handle_riddle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id
        riddle_info = self.riddles[user_id]
        riddle_info['attempts'] += 1

        if user_input.lower() == riddle_info['answer'].lower():
            await update.message.reply_text("Correct! You have solved the riddle.")
            self.player_karma[user_id] = min(100, self.player_karma[user_id] + 5)
            await self.progress_story(update, context, "solved the riddle")
        elif riddle_info['attempts'] >= 3:
            await update.message.reply_text(f"You've used all your attempts. The correct answer was: {riddle_info['answer']}")
            self.player_karma[user_id] = max(0, self.player_karma[user_id] - 5)
            await self.progress_story(update, context, "failed to solve the riddle")
        else:
            remaining_attempts = 3 - riddle_info['attempts']
            await update.message.reply_text(f"That's not correct. You have {remaining_attempts} attempts left.")

        riddle_info['active'] = riddle_info['attempts'] < 3

    async def generate_riddle(self):
        prompt = """
        Generate a Zen-themed riddle with its answer. The riddle should be challenging but solvable.
        Format:
        Riddle: [Your riddle here]
        Answer: [The answer to the riddle]
        """
        response = await self.generate_response(prompt)
        riddle_parts = response.split("Answer:")
        return {'riddle': riddle_parts[0].replace("Riddle:", "").strip(), 'answer': riddle_parts[1].strip()}

    async def end_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str):
        user_id = update.effective_user.id
        self.quest_active[user_id] = False
        self.in_combat[user_id] = False

        conclusion = await self.generate_quest_conclusion(victory, self.current_stage.get(user_id, 0))
        message = f"{reason}\n\n{conclusion}"
        await update.message.reply_text(message)

        zen_points = random.randint(30, 50) if victory else -random.randint(10, 20)
        await update.message.reply_text(f"You have {'earned' if victory else 'lost'} {abs(zen_points)} Zen points!")
        await self.add_zen_points(context, user_id, zen_points)

        # Clean up user-specific data
        for attr in ['characters', 'current_stage', 'total_stages', 'current_scene', 'quest_state', 'quest_goal', 'player_karma', 'current_opponent', 'riddles']:
            self.__dict__[attr].pop(user_id, None)

    async def generate_quest_conclusion(self, victory: bool, stage: int):
        prompt = f"""
        Generate a brief, zen-like conclusion for a {'successful' if victory else 'failed'} quest that ended at stage {stage}.
        Include:
        1. A reflection on the journey and {'growth' if victory else 'lessons from failure'}
        2. A subtle zen teaching or insight gained
        3. {'Encouragement for future quests' if victory else 'Gentle encouragement to try again'}
        Keep it concise, around 3-4 sentences.
        """
        return await self.generate_response(prompt)

    async def interrupt_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if self.quest_active.get(user_id, False):
            self.quest_active[user_id] = False
            self.in_combat[user_id] = False
            await update.message.reply_text("Your quest has been interrupted. You can start a new one with /zenquest.")
            for attr in ['characters', 'current_stage', 'current_scene', 'quest_state', 'quest_goal', 'riddles', 'total_stages']:
                self.__dict__[attr].pop(user_id, None)
        else:
            await update.message.reply_text("You don't have an active quest to interrupt.")

    def is_action_unfeasible(self, action):
        return any(unfeasible in action.lower() for unfeasible in self.unfeasible_actions)

    def is_action_failure(self, action):
        return any(failure in action.lower() for failure in self.failure_actions)

    async def handle_unfeasible_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("That action is not possible in this realm. Please choose a different path.")

    async def handle_failure_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        await update.message.reply_text("Your choice leads to an unfortunate end.")
        await self.end_quest(update, context, victory=False, reason="You have chosen a path that ends your journey prematurely.")

    async def handle_self_harm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        support_message = (
            "I'm sorry you're feeling this way. Please reach out to a mental health professional or someone you trust for support. "
            "Remember, you are valued and your life matters. If you need immediate help, please contact a suicide prevention hotline."
        )
        await update.message.reply_text(support_message)
        await self.end_quest(update, context, victory=False, reason="Your journey has been paused for your well-being.")

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            await update.message.reply_text("You are not currently on a quest. Use /zenquest to start a new journey.")
            return

        character = self.characters[user_id]
        current_stage = self.current_stage.get(user_id, 0)
        total_stages = self.total_stages.get(user_id, 1)
        progress = (current_stage / total_stages) * 100

        status_message = f"""
        Quest Status:
        Character Class: {character.class_name}
        Goal: {self.quest_goal.get(user_id, 'Unknown')}
        Progress: {progress:.1f}% complete
        Current Stage: {current_stage}/{total_stages}
        HP: {character.current_hp}/{character.max_hp}
        Energy: {character.current_energy}/{character.max_energy}
        Karma: {self.player_karma.get(user_id, 0)}
        Quest State: {self.quest_state.get(user_id, 'Unknown')}
        In Combat: {'Yes' if self.in_combat.get(user_id, False) else 'No'}
        """
        await update.message.reply_text(status_message)

    async def meditate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self.quest_active.get(user_id, False):
            await update.message.reply_text("You can only meditate during an active quest. Use /zenquest to start a journey.")
            return

        character = self.characters[user_id]
        meditation_prompt = f"""
        The {character.class_name} decides to meditate in their current situation:
        Current scene: {self.current_scene.get(user_id, 'Unknown')}
        Quest state: {self.quest_state.get(user_id, 'Unknown')}

        Generate a brief meditation experience (2-3 sentences) that:
        1. Provides a moment of insight or clarity
        2. Slightly improves the player's spiritual state
        3. Hints at a possible path forward in the quest
        """
        meditation_result = await self.generate_response(meditation_prompt)
        
        self.player_karma[user_id] = min(100, self.player_karma.get(user_id, 0) + 5)
        character.current_hp = min(character.max_hp, character.current_hp + 10)
        character.current_energy = min(character.max_energy, character.current_energy + 15)

        await update.message.reply_text(f"{meditation_result}\n\nYour karma, HP, and energy have slightly improved.")

    async def generate_quest_goal(self):
        prompt = """
        Create a brief Zen-themed quest goal (max 50 words). Include:
        1. A journey of self-discovery or helping others
        2. Exploration of a mystical or natural location
        3. A search for wisdom or a symbolic artifact
        4. A hint at physical and spiritual challenges
        """
        return await self.generate_response(prompt, elaborate=False)

    async def generate_initial_scene(self, quest_goal, class_name):
        prompt = f"""
        Create a concise opening scene (max 100 words) for this Zen quest:
        Quest Goal: {quest_goal}
        Character Class: {class_name}

        Include:
        1. Brief description of the starting location
        2. Introduction to the quest's purpose
        3. Three distinct, non-trivial choices for the player to begin their journey
        4. A hint of challenges ahead
        5. A subtle reference to the character's unique abilities as a {class_name}
        """
        return await self.generate_response(prompt, elaborate=True)

    async def generate_next_scene(self, user_id: int, user_input: str):
        character = self.characters[user_id]
        player_karma = self.player_karma.get(user_id, 100)
        current_stage = self.current_stage.get(user_id, 0)
        total_stages = self.total_stages.get(user_id, 1)
        progress = current_stage / total_stages

        event_type = random.choices(
            ["normal", "challenge", "reward", "meditation", "npc_encounter", "moral_dilemma",
             "spiritual_trial", "natural_obstacle", "mystical_phenomenon", "combat", "riddle", "quest_fail"],
            weights=[30, 15, 5, 5, 5, 10, 5, 5, 5, 10, 3, 2],
            k=1
        )[0]

        prompt = f"""
        Previous scene: {self.current_scene[user_id]}
        User's action: "{user_input}"
        Character Class: {character.class_name}
        Current quest state: {self.quest_state[user_id]}
        Quest goal: {self.quest_goal[user_id]}
        Player karma: {player_karma}
        Current stage: {current_stage}
        Total stages: {total_stages}
        Progress: {progress:.2%}
        Event type: {event_type}

        Generate the next scene of the Zen-themed quest based on the event type. Include:
        1. A vivid description of the new situation or environment (2-3 sentences)
        2. The outcome of the user's previous action and its impact (1-2 sentences)
        3. A new challenge, obstacle, or decision point (1-2 sentences)
        4. Three distinct, non-trivial choices for the player (1 sentence each)
        5. A brief Zen-like insight relevant to the situation (1 sentence)
        6. If applicable, include "HP_CHANGE: X" where X is the amount of HP gained or lost
        7. If the event type is "combat", include "COMBAT_START" in the scene
        8. If it's a riddle event, include "RIDDLE_START" in the scene

        Ensure the scene:
        - Progresses the quest towards its goal, reflecting the current progress
        - Presents a real possibility of failure or setback
        - Incorporates the character's class abilities or traits subtly
        - Maintains a balance between physical adventure and spiritual growth
        - Incorporates Zen teachings or principles subtly

        Keep the total response under 200 words.
        """

        try:
            next_scene = await self.generate_response(prompt, elaborate=True)
            return next_scene
        except Exception as e:
            logger.error(f"Error generating next scene: {e}")
            return "An error occurred while generating the next scene. Please try again."

    async def generate_response(self, prompt, elaborate=False):
        try:
            max_tokens = 300 if elaborate else 150
            response = await client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a wise Zen master guiding a quest. Maintain realism for human capabilities. Actions should have logical consequences. Provide challenging moral dilemmas and opportunities for growth."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error generating response: {type(e).__name__}: {str(e)}")
            return "I apologize, I'm having trouble connecting to my wisdom source right now. Please try again later."

    async def add_zen_points(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, points: int):
        db = get_db_connection()
        if db:
            try:
                with db.cursor() as cursor:
                    cursor.execute("""
                        UPDATE users 
                        SET zen_points = GREATEST(0, LEAST(100, zen_points + %s)) 
                        WHERE user_id = %s
                    """, (points, user_id))
                    db.commit()
                    logger.info(f"User {user_id}'s Zen points updated by {points}.")
            except mysql.connector.Error as e:
                logger.error(f"Database error in add_zen_points for User {user_id}: {e}")
                await context.bot.send_message(chat_id=user_id, text="An error occurred while updating your Zen points.")
            finally:
                db.close()
        else:
            logger.error(f"Database connection failed while updating Zen points for User {user_id}.")

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
    user_id = update.effective_user.id
    if zen_quest.quest_active.get(user_id, False):
        await zen_quest.handle_input(update, context)
    else:
        await update.message.reply_text("You're not on a quest. Use /zenquest to start one!")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=True)
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An error occurred while processing your request. Please try again later.")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("class_"):
        await zen_quest.select_character_class(update, context)
    elif data.startswith("combat_"):
        await zen_quest.handle_combat_callback(update, context)
    else:
        await query.answer("Unknown callback query")

def main():
    # Use environment variable to determine webhook or polling
    use_webhook = os.getenv('USE_WEBHOOK', 'false').lower() == 'true'

    token = os.getenv("TELEGRAM_TOKEN")
    port = int(os.environ.get('PORT', 8080))

    # Initialize bot
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

    # Set up database
    setup_database()

    if use_webhook:
        # Webhook settings
        webhook_url = os.getenv('WEBHOOK_URL')
        if not webhook_url:
            logger.error("Webhook URL not set. Please set the WEBHOOK_URL environment variable.")
            return

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=webhook_url
        )
    else:
        # Start the Bot using polling
        application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()