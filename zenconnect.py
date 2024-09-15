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

class ZenQuest:
    def __init__(self):
        self.quest_active = {}
        self.player_hp = {}
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

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if update.message.chat.type != 'private':
            await update.message.reply_text("Zen quests can only be started in private chats with the bot.")
            return

        try:
            self.quest_active[user_id] = True
            self.player_hp[user_id] = 100
            self.current_stage[user_id] = 0
            self.total_stages[user_id] = random.randint(30, 50)
            self.quest_state[user_id] = "beginning"
            self.in_combat[user_id] = False
            self.player_karma[user_id] = 100

            self.quest_goal[user_id] = await self.generate_quest_goal()
            self.current_scene[user_id] = await self.generate_initial_scene(self.quest_goal[user_id])

            start_message = f"Your quest begins!\n\n{self.quest_goal[user_id]}\n\n{self.current_scene[user_id]}"
            await self.send_split_message(update, start_message)
        except Exception as e:
            logger.error(f"Error starting quest: {e}")
            await update.message.reply_text("An error occurred while starting the quest. Please try again.")
            self.quest_active[user_id] = False

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_input = update.message.text.lower()

        if not self.quest_active.get(user_id, False) or update.message.chat.type != 'private':
            return

        if self.in_combat.get(user_id, False):
            if user_input == '/surrender':
                await self.surrender(update, context)
            else:
                await self.handle_combat_input(update, context)
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

            hp_change = 0
            if "HP_CHANGE:" in next_scene:
                hp_change_str = next_scene.split("HP_CHANGE:")[1].split()[0]
                hp_change = int(hp_change_str)

            self.player_hp[user_id] = max(0, min(100, self.player_hp[user_id] + hp_change))

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

            if self.player_hp[user_id] <= 0:
                await self.end_quest(update, context, victory=False, reason="Your life force has been depleted. Your journey ends here.")
            elif self.player_karma[user_id] < 10:
                await self.end_quest(update, context, victory=False, reason="Your actions have led you far astray from the path of enlightenment.")

        except Exception as e:
            logger.error(f"Error progressing story: {e}")
            await update.message.reply_text("An error occurred while processing your action. Please try again.")

    async def generate_next_scene(self, user_id: int, user_input: str):
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

        Keep the total response under 200 words.
        """

        try:
            next_scene = await self.generate_response(prompt, elaborate=True)
            return next_scene
        except Exception as e:
            logger.error(f"Error generating next scene: {e}")
            return "An error occurred while generating the next scene. Please try again."

    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
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

    async def handle_combat_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_input = update.message.text.lower()

        if user_input not in ["attack", "defend", "use special ability"]:
            await update.message.reply_text("Invalid combat action. Please choose 'attack', 'defend', or 'use special ability'.")
            return

        result = await self.resolve_combat_action(user_id, user_input)
        await update.message.reply_text(result)

        if self.player_hp[user_id] <= 0:
            await self.end_quest(update, context, victory=False, reason="You have been defeated in combat.")
        elif not self.in_combat[user_id]:
            await self.progress_story(update, context, "defeated the opponent")
        else:
            await self.send_combat_options(update, context)

    async def resolve_combat_action(self, user_id: int, action: str):
        player_damage = random.randint(10, 20)
        opponent_damage = random.randint(5, 15)

        if action == "attack":
            self.player_hp[user_id] -= opponent_damage
            opponent_hp = 100 - player_damage
            result = f"You attack and deal {player_damage} damage. The opponent strikes back, dealing {opponent_damage} damage to you."
        elif action == "defend":
            opponent_damage //= 2
            self.player_hp[user_id] -= opponent_damage
            result = f"You take a defensive stance. The opponent's attack deals only {opponent_damage} damage to you."
        else:  # use special ability
            special_damage = random.randint(15, 25)
            self.player_hp[user_id] -= opponent_damage
            opponent_hp = 100 - special_damage
            result = f"You use a special ability, dealing {special_damage} damage. The opponent strikes back, dealing {opponent_damage} damage to you."

        if opponent_hp <= 0:
            self.in_combat[user_id] = False
            result += f"\n\nYou have defeated {self.current_opponent[user_id]}!"
        elif self.player_hp[user_id] <= 0:
            result += "\n\nYou have been defeated in combat."

        return result

    async def send_combat_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("Attack", callback_data="combat_attack")],
            [InlineKeyboardButton("Defend", callback_data="combat_defend")],
            [InlineKeyboardButton("Use Special Ability", callback_data="combat_special")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Choose your combat action:", reply_markup=reply_markup)

    async def handle_combat_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        action = query.data.split('_')[1]

        result = await self.resolve_combat_action(user_id, action)
        await query.edit_message_text(text=result)

        if self.player_hp[user_id] <= 0:
            await self.end_quest(update, context, victory=False, reason="You have been defeated in combat.")
        elif not self.in_combat[user_id]:
            await self.progress_story(update, context, "defeated the opponent")
        else:
            await self.send_combat_options(update, context)

    async def surrender(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if self.in_combat.get(user_id, False):
            self.in_combat[user_id] = False
            await self.end_quest(update, context, victory=False, reason="You have surrendered from combat.")
        else:
            await update.message.reply_text("You are not currently in combat.")

    async def generate_opponent(self):
        opponents = ["Shadow Warrior", "Zen Master", "Mountain Spirit", "River Sage", "Forest Guardian"]
        return random.choice(opponents)

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        riddle = await self.generate_riddle()
        self.riddles[user_id] = {'riddle': riddle['riddle'], 'answer': riddle['answer'], 'active': True, 'attempts': 0}
        await update.message.reply_text(f"Solve this riddle:\n\n{riddle['riddle']}\n\nYou have 3 attempts.")

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
        for attr in ['player_hp', 'current_stage', 'total_stages', 'current_scene', 'quest_state', 'quest_goal', 'player_karma', 'current_opponent', 'riddles']:
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
            for attr in ['player_hp', 'current_stage', 'current_scene', 'quest_state', 'quest_goal', 'riddles', 'total_stages']:
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

        current_stage = self.current_stage.get(user_id, 0)
        total_stages = self.total_stages.get(user_id, 1)
        progress = (current_stage / total_stages) * 100

        status_message = f"""
        Quest Status:
        Goal: {self.quest_goal.get(user_id, 'Unknown')}
        Progress: {progress:.1f}% complete
        Current Stage: {current_stage}/{total_stages}
        HP: {self.player_hp.get(user_id, 100)}
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

        meditation_prompt = f"""
        The player decides to meditate in their current situation:
        Current scene: {self.current_scene.get(user_id, 'Unknown')}
        Quest state: {self.quest_state.get(user_id, 'Unknown')}

        Generate a brief meditation experience (2-3 sentences) that:
        1. Provides a moment of insight or clarity
        2. Slightly improves the player's spiritual state
        3. Hints at a possible path forward in the quest
        """
        meditation_result = await self.generate_response(meditation_prompt)
        
        self.player_karma[user_id] = min(100, self.player_karma.get(user_id, 0) + 5)
        self.player_hp[user_id] = min(100, self.player_hp.get(user_id, 100) + 10)

        await update.message.reply_text(f"{meditation_result}\n\nYour karma and HP have slightly improved.")

    async def generate_quest_goal(self):
        prompt = """
        Create a brief Zen-themed quest goal (max 50 words). Include:
        1. A journey of self-discovery or helping others
        2. Exploration of a mystical or natural location
        3. A search for wisdom or a symbolic artifact
        4. A hint at physical and spiritual challenges
        """
        return await self.generate_response(prompt, elaborate=False)

    async def generate_initial_scene(self, quest_goal):
        prompt = f"""
        Create a concise opening scene (max 100 words) for this Zen quest:
        {quest_goal}

        Include:
        1. Brief description of the starting location
        2. Introduction to the quest's purpose
        3. Three distinct, non-trivial choices for the player to begin their journey
        4. A hint of challenges ahead
        """
        return await self.generate_response(prompt, elaborate=True)

    async def check_action_morality(self, action):
        prompt = f"""
        Evaluate the following action in the context of Zen teachings and general morality:
        "{action}"
        Is this action against Zen principles or morally wrong? Respond with 'Yes' or 'No' and provide a brief explanation (1-2 sentences).
        Consider not just violence, but also actions that promote greed, hatred, or delusion.
        """
        response = await self.generate_response(prompt)
        is_immoral = response.lower().startswith("yes")
        reason = response.split(":", 1)[1].strip() if ":" in response else response
        return {"is_immoral": is_immoral, "reason": reason}

    async def generate_severe_consequence(self, reason, current_scene):
        prompt = f"""
        The player has committed a severely immoral or unethical act: {reason}
        Current scene: {current_scene}

        Generate a severe consequence for this action. It should be one of:
        1. Immediate quest failure due to a complete violation of Zen principles
        2. Confrontation with powerful spiritual guardians leading to combat
        3. A karmic curse or spiritual affliction that greatly hinders the player's progress

        Provide a vivid description of the consequence (3-4 sentences) and specify the type ('quest_fail', 'combat', or 'affliction').
        The consequence should be severe and directly tied to the player's action, emphasizing the importance of moral choices in the quest.
        It should also fit within the mystical and spiritual theme of the quest.
        """
        response = await self.generate_response(prompt)
        if "quest_fail" in response.lower():
            type = "quest_fail"
        elif "combat" in response.lower():
            type = "combat"
        else:
            type = "affliction"
        return {"type": type, "description": response}

    async def apply_affliction(self, update: Update, context: ContextTypes.DEFAULT_TYPE, affliction_description):
        user_id = update.effective_user.id
        self.player_karma[user_id] = max(0, self.player_karma[user_id] - 10)
        
        consequence_prompt = f"""
        The player has been afflicted: {affliction_description}
        Current Karma: {self.player_karma[user_id]}

        Describe the immediate consequences and how it affects the current scene in 2-3 sentences. 
        Integrate the affliction smoothly into the narrative, maintaining the tone and context of the quest.
        """
        
        integrated_consequence = await self.generate_response(consequence_prompt)
        
        self.current_scene[user_id] += f"\n\n{integrated_consequence}"
        
        await self.send_scene(update, context)

    async def send_split_message(self, update: Update, message: str):
        max_length = 4000
        messages = [message[i:i+max_length] for i in range(0, len(message), max_length)]
        for msg in messages:
            await update.message.reply_text(msg)

    async def send_scene(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.current_scene.get(user_id):
            await update.message.reply_text("An error occurred. The quest cannot continue.")
            return

        scene = self.current_scene[user_id]
        description, choices = self.process_scene(scene)

        await self.send_split_message(update, description)
        if choices:
            await self.send_split_message(update, f"Your choices:\n{choices}")

    def process_scene(self, scene):
        parts = scene.split("Your choices:")
        description = parts[0].strip()
        choices = parts[1].strip() if len(parts) > 1 else ""
        return description, choices

    async def update_quest_state(self, user_id):
        current_stage = self.current_stage.get(user_id, 0)
        total_stages = self.total_stages.get(user_id, 1)
        progress = current_stage / total_stages

        if progress >= 0.9:
            self.quest_state[user_id] = "final_challenge"
        elif progress >= 0.7:
            self.quest_state[user_id] = "nearing_end"
        elif progress >= 0.3:
            self.quest_state[user_id] = "middle"
        else:
            self.quest_state[user_id] = "beginning"

    async def generate_response(self, prompt, elaborate=False):
        try:
            max_tokens = 300 if elaborate else 150
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
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
    application.add_handler(CallbackQueryHandler(zen_quest.handle_combat_callback, pattern="^combat_"))

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
        # Start the Bot
        application.run_polling()

if __name__ == '__main__':
    main()