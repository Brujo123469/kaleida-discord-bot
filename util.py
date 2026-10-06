"""util.py - small helpers shared by bot.py and posts.py (who is staff, finding channels by name, the brand colour)."""
import os

import discord

# The colour bar on every post Spirekeeper makes. Hex without '#'. PLACEHOLDER until the logo exists - match it then.
BRAND_COLOR = discord.Colour(int(os.getenv("BRAND_COLOR", "6C5CE7"), 16))


def is_staff(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.manage_messages or perms.moderate_members or perms.administrator))


def find_channel(guild: discord.Guild, name: str):
    return discord.utils.get(guild.text_channels, name=name) if name else None


def excerpt(text: str, limit: int = 600) -> str:
    text = text or "(no text - attachment or embed only)"
    return text if len(text) <= limit else text[:limit] + "..."
