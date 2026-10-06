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
  * Staff command: "!kaleida reload" re-reads knowledge/*.md after an edit; "!kaleida status" shows mode and spend.

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

from brain import Brain

load_dotenv()
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

FOLLOW_UP = "Good question - that one isn't in my notes, so the developer will follow up here."
RESTING = "I've hit my limit for today - the developer will pick this one up."
FOOTER = "\n-# Spirekeeper (bot) - automated answer from the FAQ"

intents = discord.Intents.default()
intents.message_content = True   # privileged: switch it on in the Developer Portal (README step 1)
client = discord.Client(intents=intents)
brain = Brain(MONTHLY_BUDGET)

last_question = collections.defaultdict(float)   # user id -> time of their last answered question
flag_reviewed = set()                            # message ids already sent to the queue from a flag


def is_staff(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.manage_messages or perms.moderate_members or perms.administrator))


def is_new_member(member) -> bool:
    joined = getattr(member, "joined_at", None)
    if joined is None:
        return False
    return datetime.datetime.now(datetime.timezone.utc) - joined < datetime.timedelta(days=NEW_MEMBER_DAYS)


def find_channel(guild: discord.Guild, name: str):
    return discord.utils.get(guild.text_channels, name=name)


def excerpt(text: str, limit: int = 600) -> str:
    text = text or "(no text - attachment or embed only)"
    return text if len(text) <= limit else text[:limit] + "..."


async def post_log(guild: discord.Guild, text: str) -> None:
    channel = find_channel(guild, LOG_CHANNEL)
    if channel:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())


class QueueView(discord.ui.View):
    """The buttons under a #mod-queue post. Moderators only. (After a bot restart, old posts' buttons stop working -
    act on those by hand.)"""

    def __init__(self, guild_id: int, channel_id: int, message_id: int, author_id: int):
        super().__init__(timeout=None)
        self.guild_id, self.channel_id, self.message_id, self.author_id = guild_id, channel_id, message_id, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Moderators only.", ephemeral=True)
        return False

    async def _close(self, interaction: discord.Interaction, outcome: str) -> None:
        content = (interaction.message.content or "") + f"\n**Handled by {interaction.user.display_name}: {outcome}**"
        await interaction.response.edit_message(content=content, view=None)
        await post_log(interaction.guild, f"{interaction.user.display_name} - {outcome} (queue item, message {self.message_id})")

    @discord.ui.button(label="Delete message", style=discord.ButtonStyle.danger)
    async def delete_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            channel = interaction.guild.get_channel(self.channel_id) or await interaction.guild.fetch_channel(self.channel_id)
            target = await channel.fetch_message(self.message_id)
            await target.delete()
            await self._close(interaction, "deleted the message")
        except discord.NotFound:
            await self._close(interaction, "message was already gone")
        except discord.Forbidden:
            await interaction.response.send_message("I lack Manage Messages in that channel.", ephemeral=True)

    @discord.ui.button(label="Timeout 1h", style=discord.ButtonStyle.secondary)
    async def timeout_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            member = await interaction.guild.fetch_member(self.author_id)
            await member.timeout(datetime.timedelta(hours=1), reason=f"Mod queue, by {interaction.user.display_name}")
            await self._close(interaction, f"timed out {member.display_name} for 1 hour")
        except discord.NotFound:
            await self._close(interaction, "the member has left")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't time that member out (Moderate Members missing, or their role is above mine).", ephemeral=True)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.success)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close(interaction, "dismissed (no action)")


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
        view=QueueView(message.guild.id, message.channel.id, message.id, message.author.id),
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
        text = result["answer"].strip()[:1800]
        await message.reply(text + FOOTER, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    else:
        await message.reply(FOLLOW_UP, mention_author=False)


async def staff_command(message: discord.Message) -> bool:
    content = message.content.strip().lower()
    if content == "!kaleida reload":
        brain.reload_knowledge()
        await message.reply("Reloaded the rules and the FAQ.", mention_author=False)
        return True
    if content == "!kaleida status":
        await message.reply(
            f"Mode: **{MODE}** - spent today ~${brain.budget.today_spent():.3f} of ${brain.budget.daily_cap:.2f} "
            f"(${MONTHLY_BUDGET:.0f}/month pace).", mention_author=False)
        return True
    return False


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
