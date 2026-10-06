"""posts.py - Spirekeeper's posting jobs. Every public post is either FIXED text you wrote (rules, welcome, role menu) or a
DRAFT you approve: nothing written by Claude goes public until a staff member presses Post.

  * Drafts - "!kaleida announce <notes>" / "!kaleida devlog <notes>" (attach images or clips to the same message).
    Spirekeeper tidies the notes (one Claude call, ~1-2 cents) into a branded post and puts a PREVIEW in the drafts channel
    with Post / Edit / Cancel. Add "raw" ("!kaleida announce raw ...") to skip Claude: the first line is the title, the
    rest the body, posted as written - free.
  * Rules post - "!kaleida post rules" posts knowledge/rules.md as a styled post in the rules channel, or edits its
    earlier one in place (run it again after editing the file).
  * Role menu - "!kaleida post roles" posts buttons from knowledge/roles.json; members tap to add / remove a role.
    Only roles listed there, and never a role with moderator powers.
  * Welcome - a short message from knowledge/welcome.md when someone joins (needs WELCOME_ON_JOIN=1, see README).

Every button here is a "dynamic item": its meaning lives in its custom id, so the buttons keep working after a restart.
"""
import io
import json
import logging
import os
import pathlib
import re
import datetime

import discord

from util import BRAND_COLOR, find_channel, is_staff

log = logging.getLogger("kaleida.posts")
HERE = pathlib.Path(__file__).parent

ANNOUNCE_CHANNEL = os.getenv("ANNOUNCE_CHANNEL", "announcements")
DEVLOG_CHANNEL = os.getenv("DEVLOG_CHANNEL", "devlog")
DRAFTS_CHANNEL = os.getenv("DRAFTS_CHANNEL", os.getenv("MOD_QUEUE_CHANNEL", "mod-queue"))
RULES_CHANNEL = os.getenv("RULES_CHANNEL", "welcome-and-rules")
ROLES_CHANNEL = os.getenv("ROLES_CHANNEL", "roles")
WELCOME_CHANNEL = os.getenv("WELCOME_CHANNEL", "general")

KINDS = {
    "announcement": {"channel": ANNOUNCE_CHANNEL, "footer": "Kaleida - announcement"},
    "devlog": {"channel": DEVLOG_CHANNEL, "footer": "Kaleida - devlog"},
}
DRAFT_FOOTER = "DRAFT - only staff can see this. Post, Edit or Cancel below."
RULES_TITLE = "Server rules"
UPLOAD_LIMIT = 10 * 1024 * 1024   # Discord's upload cap for a server without boosts

# Role permissions that make a role a STAFF role. The role menu refuses any role carrying one of these.
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages", "moderate_members",
             "kick_members", "ban_members", "mention_everyone", "manage_webhooks", "manage_threads")

brain = None   # set by bot.py at startup


# ---------------------------------------------------------------------------------------------------------------- drafts

def _first_image(attachments):
    return next((a for a in attachments if (a.content_type or "").startswith("image/")), None)


async def _files_from(attachments):
    """Re-upload copies: a Discord attachment link expires, so a post must carry its own files."""
    files = []
    for a in attachments:
        if a.size > UPLOAD_LIMIT:
            continue
        files.append(discord.File(io.BytesIO(await a.read()), filename=a.filename))
    return files


def _post_embed(title: str, body: str, footer: str, image_name=None) -> discord.Embed:
    embed = discord.Embed(title=title[:256], description=body[:4000], colour=BRAND_COLOR)
    embed.set_footer(text=footer)
    if image_name:
        embed.set_image(url=f"attachment://{image_name}")
    return embed


class PostButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-post:(?P<kind>[a-z]+)"):
    def __init__(self, kind: str):
        super().__init__(discord.ui.Button(label="Post", style=discord.ButtonStyle.success, custom_id=f"kaleida-post:{kind}"))
        self.kind = kind

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        spec = KINDS.get(self.kind)
        target = find_channel(interaction.guild, spec["channel"]) if spec else None
        if target is None:
            await interaction.response.send_message(f"I can't find #{spec['channel'] if spec else '?'}.", ephemeral=True)
            return
        await interaction.response.defer()
        draft = interaction.message
        embed = draft.embeds[0].copy()
        embed.set_footer(text=spec["footer"])
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        files = await _files_from(draft.attachments)
        image = _first_image(draft.attachments)
        if image:
            embed.set_image(url=f"attachment://{image.filename}")
        try:
            posted = await target.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.followup.send(f"I'm not allowed to post in {target.mention}.", ephemeral=True)
            return
        await draft.edit(content=f"**Posted by {interaction.user.display_name}:** {posted.jump_url}", view=None)
        log.info("%s posted a %s to #%s", interaction.user, self.kind, target.name)


class EditButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-edit"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="kaleida-edit"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        await interaction.response.send_modal(EditModal(embed.title or "", embed.description or ""))


class CancelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-cancel"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="kaleida-cancel"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _staff_only(interaction)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=f"**Cancelled by {interaction.user.display_name}.**", view=None)


class EditModal(discord.ui.Modal, title="Edit the draft"):
    def __init__(self, title_text: str, body_text: str):
        super().__init__()
        self.post_title = discord.ui.TextInput(label="Title", default=title_text[:256], max_length=256)
        self.post_body = discord.ui.TextInput(label="Body", style=discord.TextStyle.paragraph,
                                              default=body_text[:4000], max_length=4000)
        self.add_item(self.post_title)
        self.add_item(self.post_body)

    async def on_submit(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0].copy()
        embed.title = str(self.post_title)
        embed.description = str(self.post_body)
        await interaction.response.edit_message(embed=embed)


def _draft_view(kind: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PostButton(kind))
    view.add_item(EditButton())
    view.add_item(CancelButton())
    return view


async def _staff_only(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    await interaction.response.send_message("Staff only.", ephemeral=True)
    return False


async def make_draft(message: discord.Message, kind: str, notes: str, raw: bool) -> None:
    """FLOW: 1. tidy the notes (Claude) unless raw -> 2. preview in the drafts channel with Post / Edit / Cancel ->
    3. delete the command if it was typed somewhere public."""
    drafts = find_channel(message.guild, DRAFTS_CHANNEL)
    if drafts is None:
        await message.reply(f"I need a #{DRAFTS_CHANNEL} channel for drafts.", mention_author=False)
        return
    notes = notes.strip()
    if not notes:
        await message.reply(f"Write your notes after the command, e.g. `!kaleida {('devlog' if kind == 'devlog' else 'announce')} "
                            "new sword clips are in...`", mention_author=False)
        return

    title, body, note = None, None, ""
    if not raw:
        async with message.channel.typing():
            result = await brain.draft_post(kind, notes)
        if result and result.get("title") and result.get("body"):
            title, body = result["title"].strip(), result["body"].strip()
        else:
            note = " (I couldn't tidy it just now - it's your notes as written.)"
    if title is None:
        first, _, rest = notes.partition("\n")
        title, body = first.strip(), (rest.strip() or first.strip())

    files = await _files_from(message.attachments)
    skipped = [a.filename for a in message.attachments if a.size > UPLOAD_LIMIT]
    image = _first_image([a for a in message.attachments if a.size <= UPLOAD_LIMIT])
    embed = _post_embed(title, body, DRAFT_FOOTER, image.filename if image else None)
    target = KINDS[kind]["channel"]
    header = f"**{kind.title()} draft for #{target}** from {message.author.display_name}{note}"
    if skipped:
        header += f"\nToo big to re-post (over 10 MB): {', '.join(skipped)} - post those by hand."
    await drafts.send(header, embed=embed, files=files, view=_draft_view(kind),
                      allowed_mentions=discord.AllowedMentions.none())

    if message.channel != drafts:
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
    else:
        await message.add_reaction("✅")


# ------------------------------------------------------------------------------------------------------------ rules post

async def _own_post(channel: discord.TextChannel, title: str, me: discord.ClientUser):
    async for old in channel.history(limit=100):
        if old.author.id == me.id and old.embeds and old.embeds[0].title == title:
            return old
    return None


async def post_rules(message: discord.Message, me: discord.ClientUser) -> None:
    channel = find_channel(message.guild, RULES_CHANNEL)
    if channel is None:
        await message.reply(f"I can't find #{RULES_CHANNEL}.", mention_author=False)
        return
    text = (HERE / "knowledge" / "rules.md").read_text(encoding="utf-8")
    text = re.sub(r"^#\s.*\n+", "", text)   # the file's own heading becomes the post title
    embed = _post_embed(RULES_TITLE, text.strip(), "Kaleida - read these before posting")
    old = await _own_post(channel, RULES_TITLE, me)
    if old:
        await old.edit(embed=embed)
        await message.reply(f"Updated the rules post: {old.jump_url}", mention_author=False)
    else:
        new = await channel.send(embed=embed)
        await message.reply(f"Posted the rules: {new.jump_url}", mention_author=False)


# -------------------------------------------------------------------------------------------------------------- role menu

def _load_roles():
    return json.loads((HERE / "knowledge" / "roles.json").read_text(encoding="utf-8"))


def _is_dangerous(role: discord.Role) -> bool:
    return any(getattr(role.permissions, p, False) for p in DANGEROUS)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kaleida-role:(?P<role_id>[0-9]+)"):
    def __init__(self, role_id: int, label: str = "Role", emoji=None):
        super().__init__(discord.ui.Button(label=label, emoji=emoji or None, style=discord.ButtonStyle.secondary,
                                           custom_id=f"kaleida-role:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        allowed = {r.get("name") for r in _load_roles().get("roles", [])}
        # The menu file is re-read on every press, so taking a role out of roles.json switches its old button off.
        if role is None or role.name not in allowed or _is_dangerous(role):
            await interaction.response.send_message("That role isn't self-assignable any more.", ephemeral=True)
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role menu")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Role menu")
                await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't change that role - a moderator needs to put my role above it.", ephemeral=True)


async def post_roles(message: discord.Message, me: discord.ClientUser) -> None:
    channel = find_channel(message.guild, ROLES_CHANNEL)
    if channel is None:
        await message.reply(f"I can't find #{ROLES_CHANNEL}.", mention_author=False)
        return
    config = _load_roles()
    view = discord.ui.View(timeout=None)
    lines, problems = [], []
    for entry in config.get("roles", [])[:25]:
        role = discord.utils.get(message.guild.roles, name=entry.get("name"))
        if role is None:
            problems.append(f"no role named **{entry.get('name')}**")
            continue
        if _is_dangerous(role):
            problems.append(f"**{role.name}** has moderator powers - never self-assignable")
            continue
        view.add_item(RoleButton(role.id, role.name, entry.get("emoji")))
        lines.append(f"{entry.get('emoji', '')} **{role.name}** - {entry.get('about', '')}".strip())
    if not lines:
        await message.reply("No usable roles in knowledge/roles.json: " + "; ".join(problems), mention_author=False)
        return
    title = config.get("title", "Pick your roles")
    embed = _post_embed(title, config.get("intro", "") + "\n\n" + "\n".join(lines), "Kaleida - tap again to remove")
    old = await _own_post(channel, title, me)
    if old:
        await old.edit(embed=embed, view=view)
        done = f"Updated the role menu: {old.jump_url}"
    else:
        new = await channel.send(embed=embed, view=view)
        done = f"Posted the role menu: {new.jump_url}"
    if problems:
        done += "\nSkipped: " + "; ".join(problems)
    await message.reply(done, mention_author=False)


# ---------------------------------------------------------------------------------------------------------------- welcome

async def welcome(member: discord.Member) -> None:
    channel = find_channel(member.guild, WELCOME_CHANNEL)
    path = HERE / "knowledge" / "welcome.md"
    if channel is None or not path.exists():
        return
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return
    rules = find_channel(member.guild, RULES_CHANNEL)
    text = text.replace("{member}", member.mention).replace("{rules}", rules.mention if rules else f"#{RULES_CHANNEL}")
    # Ping only the newcomer, never anyone else the text might name.
    await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False))


DYNAMIC_ITEMS = (PostButton, EditButton, CancelButton, RoleButton)
