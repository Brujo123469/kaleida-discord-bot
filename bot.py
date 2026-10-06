"""bot.py - the Kaleida Discord bot: first-pass moderation and community answers, with Claude as the reader.

What it does:
  * Moderation - every message from a NEW member (joined < NEW_MEMBER_DAYS), and any message a member flags with the
    flag emoji, gets a Claude verdict: ok / remove / remove_and_timeout / review.
      - shadow mode (default): nothing is touched; anything not "ok" is posted to #mod-queue as "would have ...".
      - active mode: remove deletes, remove_and_timeout deletes and times out (TIMEOUT_MINUTES, max 60); review and
        anything Claude could not read go to #mod-queue. Every automatic action is written to #mod-log.
    The bot NEVER bans. Moderators act on the queue with buttons (delete / timeout 1h / dismiss) or by hand.
  * Answers - when mentioned, or in #ask: Claude answers from knowledge/faq.md only; anything the FAQ does not cover gets
    "the developer will follow up".
  * Posts (posts.py) - staff-approved announcements and devlogs, the rules post, the role menu, the welcome message.
  * Staff commands: "!kaleida help" lists them all.

Run: pip install -r requirements.txt, fill .env (see .env.example), python bot.py. Setup: README.md.
"""
import collections
import datetime
import logging
import os
import re
import time

import discord
from dotenv import load_dotenv

load_dotenv()   # before the local imports: posts.py and util.py read their settings when imported

import posts  # noqa: E402
from brain import Brain  # noqa: E402
from util import excerpt, find_channel, is_staff, link_channels  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("kaleida.bot")

TOKEN = os.environ["DISCORD_TOKEN"]
MODE = os.getenv("MOD_MODE", "shadow").strip().lower()
MONTHLY_BUDGET = float(os.getenv("MONTHLY_BUDGET_USD", "10"))
NEW_MEMBER_DAYS = int(os.getenv("NEW_MEMBER_DAYS", "7"))
ASK_CHANNEL = os.getenv("ASK_CHANNEL", "ask")
QUEUE_CHANNEL = os.getenv("MOD_QUEUE_CHANNEL", "mod-queue")
LOG_CHANNEL = os.getenv("MOD_LOG_CHANNEL", "mod-log")
FLAG_EMOJI = os.getenv("FLAG_EMOJI", "🚩")
TIMEOUT_MINUTES = max(1, min(int(os.getenv("TIMEOUT_MINUTES", "10")), 60))
ASK_COOLDOWN = int(os.getenv("ASK_COOLDOWN_SECONDS", "60"))
# The welcome message needs Discord's "Server Members Intent" switched on in the Developer Portal (Bot tab). Turn this on
# ONLY after switching that on, or Discord refuses the bot's login.
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "0").strip() in ("1", "true", "yes")

FOLLOW_UP = "Good question - that one isn't in my notes, so the developer will follow up here."
RESTING = "I've hit my limit for today - the developer will pick this one up."
FOOTER = "\n-# Spirekeeper (bot) - automated answer from the FAQ"

intents = discord.Intents.default()
intents.message_content = True   # privileged: switch it on in the Developer Portal (README step 1)
intents.members = WELCOME_ON_JOIN  # privileged: only when the welcome message is wanted (see WELCOME_ON_JOIN above)
client = discord.Client(intents=intents)
brain = Brain(MONTHLY_BUDGET)
posts.brain = brain

last_question = collections.defaultdict(float)   # user id -> time of their last answered question
flag_reviewed = set()                            # message ids already sent to the queue from a flag


def is_new_member(member) -> bool:
    joined = getattr(member, "joined_at", None)
    if joined is None:
        return False
    return datetime.datetime.now(datetime.timezone.utc) - joined < datetime.timedelta(days=NEW_MEMBER_DAYS)


async def post_log(guild: discord.Guild, text: str) -> None:
    channel = find_channel(guild, LOG_CHANNEL)
    if channel:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())


async def _queue_staff_only(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    await interaction.response.send_message("Moderators only.", ephemeral=True)
    return False


async def _close(interaction: discord.Interaction, outcome: str) -> None:
    content = (interaction.message.content or "") + f"\n**Handled by {interaction.user.display_name}: {outcome}**"
    await interaction.response.edit_message(content=content, view=None)
    await post_log(interaction.guild, f"{interaction.user.display_name} - {outcome} (queue item)")


# The buttons under a #mod-queue post. Each one carries what it acts on in its custom id ("dynamic items"), so the
# buttons keep working after the bot restarts. Moderators only.
class QueueDelete(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-qdel:(?P<channel>[0-9]+):(?P<message>[0-9]+)"):
    def __init__(self, channel_id: int, message_id: int):
        super().__init__(discord.ui.Button(label="Delete message", style=discord.ButtonStyle.danger,
                                           custom_id=f"kaleida-qdel:{channel_id}:{message_id}"))
        self.channel_id, self.message_id = channel_id, message_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["channel"]), int(match["message"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _queue_staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        try:
            channel = interaction.guild.get_channel(self.channel_id) or await interaction.guild.fetch_channel(self.channel_id)
            target = await channel.fetch_message(self.message_id)
            await target.delete()
            await _close(interaction, "deleted the message")
        except discord.NotFound:
            await _close(interaction, "message was already gone")
        except discord.Forbidden:
            await interaction.response.send_message("I lack Manage Messages in that channel.", ephemeral=True)


class QueueTimeout(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-qto:(?P<author>[0-9]+)"):
    def __init__(self, author_id: int):
        super().__init__(discord.ui.Button(label="Timeout 1h", style=discord.ButtonStyle.secondary,
                                           custom_id=f"kaleida-qto:{author_id}"))
        self.author_id = author_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["author"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _queue_staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        try:
            member = await interaction.guild.fetch_member(self.author_id)
            await member.timeout(datetime.timedelta(hours=1), reason=f"Mod queue, by {interaction.user.display_name}")
            await _close(interaction, f"timed out {member.display_name} for 1 hour")
        except discord.NotFound:
            await _close(interaction, "the member has left")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't time that member out (Moderate Members missing, or their role is above mine).", ephemeral=True)


class QueueDismiss(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-qdis"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Dismiss", style=discord.ButtonStyle.success, custom_id="kaleida-qdis"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _queue_staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        await _close(interaction, "dismissed (no action)")


def queue_view(message: discord.Message) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(QueueDelete(message.channel.id, message.id))
    view.add_item(QueueTimeout(message.author.id))
    view.add_item(QueueDismiss())
    return view


client.add_dynamic_items(QueueDelete, QueueTimeout, QueueDismiss, *posts.DYNAMIC_ITEMS)


async def send_to_queue(message: discord.Message, headline: str, verdict) -> None:
    channel = find_channel(message.guild, QUEUE_CHANNEL)
    if not channel:
        log.warning("no #%s channel - cannot queue: %s", QUEUE_CHANNEL, headline)
        return
    lines = [
        f"**{headline}**",
        f"Author: {message.author.mention} ({message.author.display_name}) in {message.channel.mention}",
        f"Link: {message.jump_url}",
    ]
    if verdict:
        lines.append(f"Bot's read: `{verdict.get('verdict')}` / `{verdict.get('category')}` - {verdict.get('reason')}")
    lines.append(f"> {excerpt(message.content)}".replace("\n", "\n> "))
    await channel.send(
        "\n".join(lines),
        view=queue_view(message),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def act_on_verdict(message: discord.Message, verdict, flagged: bool) -> bool:
    """Returns True when the message was removed (so nothing else should answer it)."""
    if verdict is None:
        if flagged:
            await send_to_queue(message, "Flagged by a member - the bot could not read it, needs a human", None)
        return False
    kind = verdict.get("verdict")
    if kind == "ok":
        if flagged:
            await send_to_queue(message, "Flagged by a member - the bot reads it as OK", verdict)
        return False
    if kind == "review" or MODE != "active":
        prefix = "Needs review" if kind == "review" else f"[shadow] Would have: {kind}"
        await send_to_queue(message, prefix, verdict)
        return False

    # ACTIVE mode, remove / remove_and_timeout
    author = message.author
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        await send_to_queue(message, f"Wanted to {kind} but could not delete", verdict)
        return False
    outcome = "deleted a message"
    if kind == "remove_and_timeout":
        try:
            member = await message.guild.fetch_member(author.id)
            await member.timeout(datetime.timedelta(minutes=TIMEOUT_MINUTES), reason=f"Auto-mod: {verdict.get('reason')}")
            outcome += f" and timed out for {TIMEOUT_MINUTES} min"
        except (discord.Forbidden, discord.NotFound):
            outcome += " (timeout failed - check role order)"
    await post_log(
        message.guild,
        f"Auto-mod {outcome}: {author.display_name} in #{message.channel.name} - "
        f"`{verdict.get('category')}`: {verdict.get('reason')}\n> {excerpt(message.content, 300)}",
    )
    return True


async def answer_question(message: discord.Message) -> None:
    now = time.monotonic()
    if now - last_question[message.author.id] < ASK_COOLDOWN and not is_staff(message.author):
        return
    last_question[message.author.id] = now
    question = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not question:
        return
    async with message.channel.typing():
        if not brain.budget.can_spend():
            await message.reply(RESTING, mention_author=False)
            return
        result = await brain.answer(question)
    if result and result.get("answerable") and result.get("answer"):
        text = link_channels(message.guild, result["answer"].strip()[:1800])
        await message.reply(text + FOOTER, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    else:
        await message.reply(FOLLOW_UP, mention_author=False)


HELP = """**Spirekeeper staff commands**
`!kaleida announce <notes>` - tidy your notes into an announcement draft (attach images to the same message)
`!kaleida devlog <notes>` - the same for #devlog (attach clips / screenshots)
add `raw` (`!kaleida announce raw ...`) to skip the tidy-up: first line = title, posted as you wrote it, no API cost
`!kaleida post rules` - post or update the rules post from knowledge/rules.md
`!kaleida post roles` - post or update the role menu from knowledge/roles.json
`!kaleida post ai` - post or update the About AI page in #about from knowledge/ai.md
`!kaleida status` - mode and today's spend
`!kaleida reload` - re-read the knowledge files after an edit"""


async def staff_command(message: discord.Message) -> bool:
    content = message.content.strip()
    lower = content.lower()
    if not lower.startswith("!kaleida"):
        return False
    words = lower.split()
    verb = words[1] if len(words) > 1 else "help"
    if verb == "reload":
        brain.reload_knowledge()
        await message.reply("Reloaded the rules and the FAQ.", mention_author=False)
    elif verb == "status":
        await message.reply(
            f"Mode: **{MODE}** - spent today ~${brain.budget.today_spent():.3f} of ${brain.budget.daily_cap:.2f} "
            f"(${MONTHLY_BUDGET:.0f}/month pace).", mention_author=False)
    elif verb in ("announce", "devlog"):
        kind = "announcement" if verb == "announce" else "devlog"
        rest = content[len("!kaleida"):].lstrip()[len(verb):]
        raw = rest.lstrip().lower().startswith("raw")
        if raw:
            rest = rest.lstrip()[3:]
        await posts.make_draft(message, kind, rest, raw)
    elif verb == "post" and len(words) > 2 and words[2] == "rules":
        await posts.post_rules(message, client.user)
    elif verb == "post" and len(words) > 2 and words[2] == "roles":
        await posts.post_roles(message, client.user)
    elif verb == "post" and len(words) > 2 and words[2] == "ai":
        await posts.post_ai_page(message, client.user)
    else:
        await message.reply(HELP, mention_author=False)
    return True


@client.event
async def on_ready():
    log.info("logged in as %s - mode %s, budget $%.2f/day", client.user, MODE, brain.budget.daily_cap)
    for guild in client.guilds:
        for name in (QUEUE_CHANNEL, LOG_CHANNEL, ASK_CHANNEL):
            if not find_channel(guild, name):
                log.warning("guild %s has no #%s channel", guild.name, name)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if is_staff(message.author):
        if await staff_command(message):
            return
    elif is_new_member(message.author):
        verdict = await brain.moderate(message.content, message.channel.name, True, False)
        if await act_on_verdict(message, verdict, flagged=False):
            return
    mentioned = client.user in message.mentions
    if mentioned or message.channel.name == ASK_CHANNEL:
        await answer_question(message)


@client.event
async def on_member_join(member: discord.Member):
    if WELCOME_ON_JOIN and not member.bot:
        await posts.welcome(member)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != FLAG_EMOJI or payload.guild_id is None or payload.message_id in flag_reviewed:
        return
    if client.user and payload.user_id == client.user.id:
        return
    guild = client.get_guild(payload.guild_id)
    channel = guild.get_channel(payload.channel_id) if guild else None
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if message.author.bot:
        return
    flag_reviewed.add(payload.message_id)
    try:
        member = await guild.fetch_member(message.author.id)
    except (discord.NotFound, discord.Forbidden):
        member = None
    if member is not None and is_staff(member):
        return
    verdict = await brain.moderate(message.content, channel.name, is_new_member(member), True)
    await act_on_verdict(message, verdict, flagged=True)


if __name__ == "__main__":
    if MODE not in ("shadow", "active"):
        raise SystemExit("MOD_MODE must be 'shadow' or 'active'")
    client.run(TOKEN, log_handler=None)
