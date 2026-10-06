"""util.py - small helpers shared by bot.py and posts.py (who is staff, finding channels by name, the brand colour)."""
import os
import re

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


_BLOCK_START = re.compile(r"^\s*(?:[-*+>#]|\d+[.)])\s")


def unwrap(text: str) -> str:
    """Join hard-wrapped lines back into one line per paragraph or list item.

    Text files are wrapped for reading in an editor, but Discord shows every line break - so a wrapped list item turns
    into a break plus a run of spaces mid-sentence. A line is glued to the one before it unless it is blank, starts a
    new list item / heading / quote, or the line before it is blank."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if out and stripped and out[-1].strip() and not _BLOCK_START.match(line):
            out[-1] = out[-1].rstrip() + " " + stripped
        else:
            out.append(line.rstrip() if stripped else "")
    return "\n".join(out)


_CHANNEL_NAME = re.compile(r"(?<![<\w])#([a-z0-9][a-z0-9_-]*)")


def link_channels(guild: discord.Guild, text: str) -> str:
    """Turn plain '#channel-name' text into a clickable channel link, for channels that exist in this server.
    Unknown names are left as plain text (so '#1' or a hashtag stays as written)."""
    def swap(match):
        channel = find_channel(guild, match.group(1))
        return channel.mention if channel else match.group(0)
    return _CHANNEL_NAME.sub(swap, text)
