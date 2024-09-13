# zenquest.py

import random
import logging
from typing import Dict, Optional, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

# Ensure you have the escape_markdown_v2 function defined or imported.
# For example:
import re

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Placeholder for generate_response function
# Replace this with your actual implementation or API call
async def generate_response(prompt: str, elaborate: bool = False) -> str:
    # Example implementation using a mock response
    return "This is a generated response based on the provided prompt."

# Placeholder for add_zen_points function
# Implement your logic to add zen points to the player (e.g., update a database)
async def add_zen_points(context: ContextTypes.DEFAULT_TYPE, user_id: int, points: int):
    # Example: Log the Zen points addition
    logging.info(f"User {user_id} has {'earned' if points > 0 else 'lost'} {abs(points)} Zen points.")

# Player class definition
class Player:
    def __init__(self, user_id: int, name: str):
        self.user_id = user_id
        self.name = name
        self.hp: int = 100
        self.karma: int = 100
        self.energy: int = 50
        self.stage: int = 0
        self.total_stages: int = random.randint(30, 50)
        self.state: str = 'beginning'
        self.in_combat: bool = False
        self.current_scene: str = ""
        self.quest_goal: str = ""
        self.riddles: Dict[str, Dict[str, str]] = {}
        self.battle_id: Optional[int] = None
        self.active: bool = True  # Indicates if the player is currently on a quest

class ZenQuest:
    def __init__(self):
        self.players: Dict[int, Player] = {}
        self.unfeasible_actions = [
            "fly", "teleport", "time travel", "breathe underwater", "become invisible",
            "read minds", "shoot lasers", "transform", "resurrect", "conjure",
            "summon creatures", "control weather", "phase through walls"
        ]
        self.failure_actions = [
            "give up", "abandon quest", "betray", "destroy sacred artifact",
            "harm innocent", "break vow", "ignore warning",
            "consume poison", "jump off cliff", "attack ally", "steal from temple"
        ]
        # Initialize logger
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        self.logger = logging.getLogger(__name__)

    async def start_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        if update.message.chat.type != 'private':
            await update.message.reply_text("Zen quests can only be started in private chats with the bot.")
            return

        try:
            # Initialize Player instance
            player_name = update.effective_user.first_name or "Player"
            self.players[user_id] = Player(user_id, player_name)

            player = self.players[user_id]

            # Generate quest goal and initial scene
            player.quest_goal = await self.generate_quest_goal()
            player.current_scene = await self.generate_initial_scene(player.quest_goal)

            start_message = f"🌀 **Your Zen Quest Begins!** 🌀\n\n**Quest Goal:** {player.quest_goal}\n\n{player.current_scene}"
            start_message = escape_markdown_v2(start_message)  # Ensure message is escaped

            await self.send_split_message(update, start_message)
        except Exception as e:
            self.logger.error(f"Error starting quest for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text("An error occurred while starting the quest. Please try again.")
            if user_id in self.players:
                self.players[user_id].active = False

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_input = update.message.text.lower()

        player = self.players.get(user_id)
        if not player or not player.active or update.message.chat.type != 'private':
            return

        # Handle combat state
        if player.in_combat:
            if user_input == '/surrender':
                await self.surrender(update, context)
            else:
                await update.message.reply_text("⚔️ You are currently in combat. Please use the provided buttons to make your move or use /surrender to give up.")
            return

        # Handle active riddle
        if player.riddles.get('active'):
            await self.handle_riddle_input(update, context, user_input)
            return

        # Handle unfeasible or failure actions
        if self.is_action_unfeasible(user_input):
            await self.handle_unfeasible_action(update, context)
            return
        elif self.is_action_failure(user_input):
            await self.handle_failure_action(update, context)
            return

        # Progress the story based on user input
        await self.progress_story(update, context, user_input)

    def is_action_unfeasible(self, action: str) -> bool:
        return any(unfeasible in action for unfeasible in self.unfeasible_actions)

    def is_action_failure(self, action: str) -> bool:
        return any(failure in action for failure in self.failure_actions)

    async def handle_unfeasible_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ That action is not possible in this realm. Please choose a different path.")

    async def handle_failure_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        await update.message.reply_text("⚠️ Your choice leads to an unfortunate end.")
        await self.end_quest(update, context, victory=False, reason="⚡ You have chosen a path that ends your journey prematurely. ⚡")

    async def progress_story(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id
        player = self.players[user_id]

        try:
            # Check action morality
            morality_check = await self.check_action_morality(user_input)

            if morality_check['is_immoral']:
                consequence = await self.generate_severe_consequence(morality_check['reason'], player.current_scene)
                consequence_description = escape_markdown_v2(consequence['description'])
                await context.bot.send_message(chat_id=user_id, text=consequence_description, parse_mode='MarkdownV2')

                # Adjust karma
                player.karma = max(0, player.karma - 20)

                # Apply consequences based on type
                if consequence['type'] == 'quest_fail':
                    await self.end_quest(update, context, victory=False, reason=consequence['description'])
                    return
                elif consequence['type'] == 'combat':
                    await self.initiate_combat(update, context, opponent_id=7283636452, opponent_name="Zen Opponent")
                    return
                elif consequence['type'] == 'affliction':
                    await self.apply_affliction(update, context, consequence['description'])
                    return

            # Generate the next scene
            next_scene = await self.generate_next_scene(user_id, user_input)
            player.current_scene = next_scene

            # Update HP based on scene
            hp_change = self.extract_hp_change(next_scene)
            player.hp = max(0, min(100, player.hp + hp_change))

            # Handle special events in the scene
            if "COMBAT_START" in next_scene:
                await self.initiate_combat(update, context, opponent_id=7283636452, opponent_name="Zen Opponent")
                return
            elif "RIDDLE_START" in next_scene:
                await self.initiate_riddle(update, context)
                return
            elif "QUEST_COMPLETE" in next_scene:
                await self.end_quest(update, context, victory=True, reason="🌟 You have completed your journey! 🌟")
                return
            elif "QUEST_FAIL" in next_scene:
                await self.end_quest(update, context, victory=False, reason="⚡ Your quest has come to an unfortunate end. ⚡")
                return
            else:
                # Progress to the next stage
                player.stage += 1
                await self.update_quest_state(user_id)

                # Send the updated scene to the player
                await self.send_scene(update, context, user_id)

                # Randomly adjust karma slightly
                player.karma = max(0, min(100, player.karma + random.randint(-3, 3)))

                # Check for end conditions
                if player.hp <= 0:
                    await self.end_quest(update, context, victory=False, reason="💀 Your life force has been depleted. Your journey ends here. 💀")
                    return
                elif player.karma < 10:
                    await self.end_quest(update, context, victory=False, reason="🌀 Your actions have led you far astray from the path of enlightenment. 🌀")
                    return

        except Exception as e:
            self.logger.error(f"Error progressing story for User {user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=user_id, text="❗ An error occurred while processing your action. Please try again.")

    async def generate_next_scene(self, user_id: int, user_input: str) -> str:
        player = self.players[user_id]
        player_karma = player.karma
        current_stage = player.stage
        total_stages = player.total_stages
        progress = current_stage / total_stages

        # Define event types with weights
        event_types = [
            "normal", "challenge", "reward", "meditation", "npc_encounter",
            "moral_dilemma", "spiritual_trial", "natural_obstacle",
            "mystical_phenomenon", "combat", "riddle", "quest_fail"
        ]
        weights = [30, 15, 5, 5, 5, 10, 5, 5, 5, 10, 3, 2]
        event_type = random.choices(event_types, weights=weights, k=1)[0]

        # Create a prompt for scene generation
        prompt = f"""
        Previous scene: {player.current_scene}
        User's action: "{user_input}"
        Current quest state: {player.state}
        Quest goal: {player.quest_goal}
        Player karma: {player_karma}
        Current stage: {current_stage}
        Total stages: {total_stages}
        Progress: {progress:.2%}
        Event type: {event_type}

        Generate the next scene of the Zen-themed quest based on the event type. Include:
        1. A vivid description of the new situation or environment (2-3 sentences)
        2. The outcome of the user's previous action and its impact (1-2 sentences)
        3. A new challenge, obstacle, or decision point (1-2 sentences)
        4. Three distinct, non-trivial choices for the player (1 sentence each). At least one choice should lead to potential quest failure or significant setback.
        5. A brief Zen-like insight relevant to the situation (1 sentence)
        6. If applicable, include "HP_CHANGE: X" where X is the amount of HP gained or lost (integer only, no symbols)
        7. If the event type is "combat", one of the choices should explicitly lead to combat using the phrase "COMBAT_START"

        Ensure the scene:
        - Progresses the quest towards its goal, reflecting the current progress
        - Presents a real possibility of failure or setback
        - Maintains a balance between physical adventure and spiritual growth
        - Incorporates Zen teachings or principles subtly
        - Includes more challenging scenarios and consequences

        If the event type is "quest_fail", incorporate an appropriate indicator in the scene.

        If the progress is over 90%, start building towards a climactic final challenge.

        Keep the total response under 200 words.

        IMPORTANT: If you include an HP_CHANGE, it must follow this exact format:
        HP_CHANGE: X (where X is a positive or negative integer without any symbols)
        For example: HP_CHANGE: 5 or HP_CHANGE: -3
        """

        # Generate scene using AI
        next_scene = await generate_response(prompt, elaborate=True)

        # Ensure correct HP_CHANGE format
        next_scene = self.correct_hp_change_format(next_scene)

        return next_scene

    def correct_hp_change_format(self, scene: str) -> str:
        if "HP_CHANGE:" in scene:
            try:
                hp_change_str = scene.split("HP_CHANGE:")[1].split()[0]
                int(hp_change_str)  # Validate integer
            except (ValueError, IndexError):
                # Correct the format
                scene = scene.replace("HP_CHANGE:", "HP_CHANGE: 0")
        return scene

    def extract_hp_change(self, scene: str) -> int:
        if "HP_CHANGE:" in scene:
            try:
                hp_change_str = scene.split("HP_CHANGE:")[1].split()[0]
                hp_change = int(hp_change_str)
                return hp_change
            except (ValueError, IndexError):
                self.logger.warning(f"Invalid HP_CHANGE format in scene: {scene}")
        return 0

    async def update_quest_state(self, user_id: int):
        player = self.players[user_id]
        progress = player.stage / player.total_stages

        if progress >= 0.9:
            player.state = "final_challenge"
        elif progress >= 0.7:
            player.state = "nearing_end"
        elif progress >= 0.3:
            player.state = "middle"
        else:
            player.state = "beginning"

    async def send_scene(self, update: Optional[Update] = None, context: ContextTypes.DEFAULT_TYPE = None, user_id: Optional[int] = None):
        if update:
            user_id = update.effective_user.id

        player = self.players.get(user_id)
        if not player or not player.current_scene:
            message = "An error occurred. The quest cannot continue."
            if update:
                await update.message.reply_text(message)
            elif context and user_id:
                await context.bot.send_message(chat_id=user_id, text=message)
            return

        scene = player.current_scene
        description, choices = self.process_scene(scene)

        if update:
            description = escape_markdown_v2(description)
            await self.send_split_message(update, description)
            if choices:
                choices = escape_markdown_v2(f"🔮 **Your Choices:** 🔮\n{choices}")
                await self.send_split_message(update, choices)
        elif context and user_id:
            description = escape_markdown_v2(description)
            await self.send_split_message_context(context, user_id, description)
            if choices:
                choices = escape_markdown_v2(f"🔮 **Your Choices:** 🔮\n{choices}")
                await self.send_split_message_context(context, user_id, choices)

    def process_scene(self, scene: str) -> Tuple[str, str]:
        if "Your Choices:" in scene:
            parts = scene.split("Your Choices:")
            description = parts[0].strip()
            choices = parts[1].strip()
            return description, choices
        else:
            return scene, ""

    async def send_split_message(self, update: Update, message: str):
        max_length = 4000  # Telegram's message limit is 4096 characters
        while len(message) > max_length:
            part = message[:max_length]
            await update.message.reply_text(part, parse_mode='MarkdownV2')
            message = message[max_length:]
        if message:
            await update.message.reply_text(message, parse_mode='MarkdownV2')

    async def send_split_message_context(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
        max_length = 4000
        while len(message) > max_length:
            part = message[:max_length]
            await context.bot.send_message(chat_id=user_id, text=part, parse_mode='MarkdownV2')
            message = message[max_length:]
        if message:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='MarkdownV2')

    async def generate_quest_goal(self) -> str:
        prompt = """
        Create a brief Zen-themed quest goal (max 50 words). Include:
        1. A journey of self-discovery or helping others
        2. Exploration of a mystical or natural location
        3. A search for wisdom or a symbolic artifact
        4. A hint at physical and spiritual challenges
        """
        return await generate_response(prompt, elaborate=False)

    async def generate_initial_scene(self, quest_goal: str) -> str:
        prompt = f"""
        Create a concise opening scene (max 100 words) for this Zen quest:
        {quest_goal}

        Include:
        1. Brief description of the starting location
        2. Introduction to the quest's purpose
        3. Three distinct, non-trivial choices for the player to begin their journey
        4. A hint of challenges ahead
        """
        return await generate_response(prompt, elaborate=True)

    async def check_action_morality(self, action: str) -> Dict[str, str]:
        prompt = f"""
        Evaluate the following action in the context of Zen teachings and general morality:
        "{action}"
        Is this action against Zen principles or morally wrong? Respond with 'Yes' or 'No' and provide a brief explanation (1-2 sentences).
        Consider not just violence, but also actions that promote greed, hatred, or delusion.
        """
        response = await generate_response(prompt)
        is_immoral = response.lower().startswith("yes")
        reason = response.split(":", 1)[1].strip() if ":" in response else response
        return {"is_immoral": is_immoral, "reason": reason}

    async def generate_severe_consequence(self, reason: str, current_scene: str) -> Dict[str, str]:
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
        response = await generate_response(prompt)
        if "quest_fail" in response.lower():
            consequence_type = "quest_fail"
        elif "combat" in response.lower():
            consequence_type = "combat"
        else:
            consequence_type = "affliction"
        return {"type": consequence_type, "description": response}

    async def apply_affliction(self, update: Update, context: ContextTypes.DEFAULT_TYPE, affliction_description: str):
        user_id = update.effective_user.id
        player = self.players[user_id]
        player.karma = max(0, player.karma - 10)

        consequence_prompt = f"""
        The player has been afflicted: {affliction_description}
        Current Karma: {player.karma}

        Describe the immediate consequences and how it affects the current scene in 2-3 sentences.
        Integrate the affliction smoothly into the narrative, maintaining the tone and context of the quest.
        """

        integrated_consequence = await generate_response(consequence_prompt)
        player.current_scene += f"\n\n{integrated_consequence}"

        await self.send_scene(update, context, user_id)

    async def end_quest(self, update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, victory: bool, reason: str, user_id: Optional[int] = None):
        if not user_id:
            user_id = update.effective_user.id if update else None

        if not user_id or user_id not in self.players:
            self.logger.error("Unable to determine user_id in end_quest")
            return

        player = self.players[user_id]
        player.active = False
        player.in_combat = False

        conclusion = await self.generate_quest_conclusion(victory, player.stage)
        message = f"{reason}\n\n{conclusion}"
        message = escape_markdown_v2(message)  # Ensure message is escaped

        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='MarkdownV2')

        zen_points = random.randint(30, 50) if victory else -random.randint(10, 20)
        zen_message = f"You have {'earned' if victory else 'lost'} {abs(zen_points)} Zen points!"
        zen_message = escape_markdown_v2(zen_message)

        await context.bot.send_message(chat_id=user_id, text=zen_message, parse_mode='MarkdownV2')

        await add_zen_points(context, user_id, zen_points)

        # Remove player data
        self.players.pop(user_id, None)

    async def generate_quest_conclusion(self, victory: bool, stage: int) -> str:
        prompt = f"""
        Generate a brief, zen-like conclusion for a {'successful' if victory else 'failed'} quest that ended at stage {stage}.
        Include:
        1. A reflection on the journey and {'growth' if victory else 'lessons from failure'}
        2. A subtle zen teaching or insight gained
        3. {'Encouragement for future quests' if victory else 'Gentle encouragement to try again'}
        Keep it concise, around 3-4 sentences.
        """
        return await generate_response(prompt)

    async def meditate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        player = self.players.get(user_id)

        if not player or not player.active:
            await update.message.reply_text("You can only meditate during an active quest. Use /zenquest to start a journey.")
            return

        meditation_prompt = f"""
        The player decides to meditate in their current situation:
        Current scene: {player.current_scene}
        Quest state: {player.state}

        Generate a brief meditation experience (2-3 sentences) that:
        1. Provides a moment of insight or clarity
        2. Slightly improves the player's spiritual state
        3. Hints at a possible path forward in the quest
        """
        meditation_result = await generate_response(meditation_prompt)

        player.karma = min(100, player.karma + 5)
        player.hp = min(100, player.hp + 10)

        meditation_message = f"{meditation_result}\n\nYour karma and HP have slightly improved."
        meditation_message = escape_markdown_v2(meditation_message)

        await update.message.reply_text(meditation_message, parse_mode='MarkdownV2')

    async def get_quest_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        player = self.players.get(user_id)

        if not player or not player.active:
            await update.message.reply_text("You are not currently on a quest. Use /zenquest to start a new journey.")
            return

        progress = (player.stage / player.total_stages) * 100

        status_message = (
            f"📜 **Quest Status:** 📜\n\n"
            f"**Goal:** {player.quest_goal}\n"
            f"**Progress:** {progress:.1f}% complete\n"
            f"**Current Stage:** {player.stage}/{player.total_stages}\n"
            f"**HP:** {player.hp}\n"
            f"**Karma:** {player.karma}\n"
            f"**Quest State:** {player.state}\n"
            f"**In Combat:** {'Yes' if player.in_combat else 'No'}"
        )
        status_message = escape_markdown_v2(status_message)

        await update.message.reply_text(status_message, parse_mode='MarkdownV2')

    async def interrupt_quest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        player = self.players.get(user_id)

        if player and player.active:
            self.players.pop(user_id, None)
            await update.message.reply_text("🔚 Your quest has been interrupted. You can start a new one with /zenquest.")
        else:
            await update.message.reply_text("❗ You don't have an active quest to interrupt.")

    # Combat Integration Methods
    async def initiate_combat(self, update: Update, context: ContextTypes.DEFAULT_TYPE, opponent_id: int, opponent_name: str):
        player = self.players.get(update.effective_user.id)
        if player:
            player.in_combat = True
            player.battle_id = random.randint(1000, 9999)  # Example battle ID

            combat_message = f"⚔️ **Combat Initiated!** ⚔️\n\nYou are now in a battle against {opponent_name}.\nUse the provided buttons to make your move or use /surrender to give up."
            combat_message = escape_markdown_v2(combat_message)
            await context.bot.send_message(chat_id=player.user_id, text=combat_message, parse_mode='MarkdownV2')

            # Trigger your existing PvP combat flow by sending combat move buttons
            await context.bot.send_message(chat_id=player.user_id, text="Choose your move:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Strike", callback_data=f"move_strike_{player.user_id}")],
                [InlineKeyboardButton("Defend", callback_data=f"move_defend_{player.user_id}")],
                [InlineKeyboardButton("Focus", callback_data=f"move_focus_{player.user_id}")],
                [InlineKeyboardButton("Zen Strike", callback_data=f"move_zenstrike_{player.user_id}")],
                [InlineKeyboardButton("Mind Trap", callback_data=f"move_mindtrap_{player.user_id}")]
            ]))

    async def surrender(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        player = self.players.get(user_id)
        if player and player.in_combat:
            player.in_combat = False
            battle_id = player.battle_id
            player.battle_id = None

            surrender_message = f"🕊️ You have surrendered in battle. Your quest ends here."
            surrender_message = escape_markdown_v2(surrender_message)
            await context.bot.send_message(chat_id=user_id, text=surrender_message, parse_mode='MarkdownV2')

            await self.end_quest(update, context, victory=False, reason="🕊️ You chose to surrender, ending your quest.")
        else:
            await update.message.reply_text("❗ You are not currently in combat.")

    # Riddle Handling Methods
    async def handle_riddle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
        user_id = update.effective_user.id
        player = self.players.get(user_id)
        if not player or not player.riddles.get('active'):
            return

        riddle = player.riddles.get('current_riddle')
        if not riddle:
            await update.message.reply_text("❗ No active riddle found.")
            return

        if user_input.lower() == riddle['answer'].lower():
            # Correct answer
            player.karma = min(100, player.karma + 10)
            player.riddles['active'] = False
            correct_message = "✅ Correct! Your wisdom has been affirmed."
            correct_message = escape_markdown_v2(correct_message)
            await update.message.reply_text(correct_message, parse_mode='MarkdownV2')
            await self.progress_story(update, context, user_input)
        else:
            # Incorrect answer
            player.karma = max(0, player.karma - 10)
            incorrect_message = f"❌ Incorrect. The correct answer was: {riddle['answer']}"
            incorrect_message = escape_markdown_v2(incorrect_message)
            await update.message.reply_text(incorrect_message, parse_mode='MarkdownV2')
            await self.progress_story(update, context, user_input)

    async def initiate_riddle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        player = self.players.get(user_id)
        if not player:
            return

        # Generate a riddle (replace with actual riddle generation logic)
        riddle_prompt = """
        Provide a challenging yet solvable Zen-themed riddle for the player. Include the riddle question and the answer separately.
        """
        riddle_response = await generate_response(riddle_prompt, elaborate=True)

        # Assuming the AI returns riddle in "Question: ... Answer: ..." format
        try:
            question, answer = riddle_response.split("Answer:")
            question = question.replace("Question:", "").strip()
            answer = answer.strip()
        except ValueError:
            # Fallback riddle
            question = "I speak without a mouth and hear without ears. I have nobody, but I come alive with the wind. What am I?"
            answer = "Echo"

        player.riddles = {
            'active': True,
            'current_riddle': {
                'question': question,
                'answer': answer
            }
        }

        riddle_message = f"🔍 **Riddle:**\n{question}\n\n💡 Provide your answer:"
        riddle_message = escape_markdown_v2(riddle_message)
        await context.bot.send_message(chat_id=user_id, text=riddle_message, parse_mode='MarkdownV2')

    # Additional PvP Integration Methods
    async def end_pvp_battle(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, victory: bool, battle_id: int):
        # Integrate with your existing PvP system's end battle logic
        conclusion = await self.generate_pvp_conclusion(victory, self.players[user_id].stage)
        message = f"{conclusion}\n\n**Battle ID:** {battle_id}\n**Outcome:** {'Victory' if victory else 'Defeat'}"
        message = escape_markdown_v2(message)

        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='MarkdownV2')

        # Adjust Zen points based on battle outcome
        zen_points = random.randint(30, 50) if victory else -random.randint(10, 20)
        zen_message = f"You have {'earned' if victory else 'lost'} {abs(zen_points)} Zen points!"
        zen_message = escape_markdown_v2(zen_message)

        await context.bot.send_message(chat_id=user_id, text=zen_message, parse_mode='MarkdownV2')

        await add_zen_points(context, user_id, zen_points)

        # Clean up player data if needed
        self.players.pop(user_id, None)

    async def generate_pvp_conclusion(self, victory: bool, stage: int) -> str:
        prompt = f"""
        Generate a brief, zen-like conclusion for a {'successful' if victory else 'failed'} PvP combat that ended at stage {stage}.
        Include:
        1. A reflection on the battle and {'victory\'s enlightenment' if victory else 'the lessons learned from defeat'}
        2. A subtle zen teaching or insight gained
        3. {'Encouragement for future battles' if victory else 'Gentle encouragement to train and try again'}
        Keep it concise, around 3-4 sentences.
        """
        return await generate_response(prompt)