# Spirekeeper - the Kaleida Discord bot

An always-on helper for the Kaleida Discord until there are human moderators:

- **First-pass moderation.** Every message from a new member (first 7 days), and anything a member flags with 🚩, gets a
  read from Claude: ok / remove / remove + short timeout / needs a human. It **never bans**. Anything it is unsure about
  goes to `#mod-queue` with buttons (Delete / Timeout 1h / Dismiss) that only moderators can press. Every automatic action
  is written to `#mod-log`.
- **Shadow mode first.** By default it touches nothing and only posts "would have removed this" to `#mod-queue`. Read its
  calls for a week, then set `MOD_MODE=active`.
- **Community answers.** Mention it, or ask in `#ask`: it answers from `knowledge/faq.md` and nothing else. Anything the
  FAQ doesn't cover gets "the developer will follow up".
- **Budget.** It paces itself to `MONTHLY_BUDGET_USD` ($10) a day at a time (~$0.33/day). When the day's share is spent it
  stops calling Claude (Discord's AutoMod keeps working; flagged messages still reach the queue) and resumes the next day.

- **Posts, staff-approved.** Styled announcements and devlogs from your rough notes (preview first, nothing goes public
  until you press Post), the rules post, a self-assign role menu, and a welcome message. See "Posting" below.

Claude never takes an action itself: it returns a verdict, and `bot.py` decides - with limits it cannot argue past (no
bans, timeouts capped at 60 minutes, shadow mode).

Staff commands, typed in any channel: `!kaleida help` lists them all - `status` (mode and today's spend), `reload` (after
editing the `knowledge/` files), `announce`, `devlog`, `post rules`, `post roles`.

## Posting

Every public post is either fixed text you wrote or a draft you approved. Claude never posts on its own.

**Announcements and devlogs.** Type `!kaleida announce` (or `!kaleida devlog`) followed by your rough notes - several
lines are fine - and attach images or clips to the same message. Best typed in `#mod-queue`; typed anywhere else, the bot
deletes your command so the notes don't sit in public. Spirekeeper tidies the notes into a clean post (one Claude call,
about 1-2 cents; it may not add any fact you didn't write) and puts a **preview** in `#mod-queue` with three buttons:
- **Post** - publishes it to `#announcements` (or `#devlog`) with your images re-attached;
- **Edit** - opens a box with the title and text so you can change anything, then Post;
- **Cancel** - drops it.

Add `raw` to skip the tidy-up and the cost: `!kaleida announce raw Demo date` + new line + the text. The first line is the
title, the rest is posted exactly as written. Files over 10 MB can't be re-posted by a bot - post big clips by hand.

**The rules post.** `!kaleida post rules` posts `knowledge/rules.md` as a styled post in `#welcome-and-rules`. Edit the
file later and run it again: it updates the same post instead of adding a new one.

**The role menu.** List the roles members may give themselves in `knowledge/roles.json` (role names exactly as in your
server, an emoji, one line about each), then `!kaleida post roles` in any channel. It posts buttons in `#roles`; members
tap to add or remove a role. Run it again after editing the file. Safety: only roles in that file, and **never a role with
moderator powers** (Manage Messages, Kick, Ban, Administrator...) - those are refused even if listed.

**The welcome message.** Off until you turn it on, because it needs one more Discord switch:
1. Developer Portal -> Spirekeeper -> **Bot** -> switch on **Server Members Intent** -> Save.
2. Railway -> Variables -> `WELCOME_ON_JOIN` = `1` (and `WELCOME_CHANNEL` if your chat channel isn't `general`).
Do step 1 first: with the variable on and the switch off, Discord refuses the bot's login. The text is
`knowledge/welcome.md` (`{member}` = the newcomer, `{rules}` = the rules channel).

**What the bot needs for posting** (Server Settings -> Roles -> Spirekeeper): **Attach Files** and **Manage Roles** on top
of the original six, and the Spirekeeper role dragged **above** every role in the menu. Channels: `#announcements`,
`#devlog`, `#roles`, `#welcome-and-rules` (Spirekeeper needs Send Messages there - members don't). Other names can be set
in the variables (`.env.example`).

**Brand colour.** The colour bar on every post is `BRAND_COLOR` (hex). A placeholder violet until the logo exists.

## Before it goes live: edit the two knowledge files

- `knowledge/rules.md` - the server rules. Paste the same text into your `#welcome-and-rules` channel.
- `knowledge/faq.md` - the bot's ONLY source for answers. Replace every `[Khai: ...]` placeholder or delete that question.
  Treat it as public: nothing internal, no date you are not ready to keep.

## Setup (about 30 minutes, once)

### 1. Create the Discord bot
1. Go to the Discord Developer Portal (discord.com/developers/applications) -> **New Application** -> name it "Spirekeeper" (the bot's display name; changeable later).
2. **Bot** tab -> **Reset Token** -> copy the token (this is a password - store it in a password manager).
3. Same tab, **Privileged Gateway Intents** -> switch on **Message Content Intent**.
4. **OAuth2 -> URL Generator**: scope `bot`; permissions **View Channels, Send Messages, Read Message History, Manage
   Messages, Moderate Members, Embed Links**. Open the generated link and add the bot to your server.
5. In your server's **Roles**, drag the bot's role **above** the member roles - it can only time out people below it.
6. Create the channels `#mod-queue` and `#mod-log` (visible to moderators only - and to the bot), and `#ask` (public).

### 2. Create the Anthropic API key with a $10 cap
1. console.anthropic.com -> sign up -> **Billing**: add $10 of credit.
2. **Limits** (in the console settings): set the **monthly spend limit to $10**. This is the hard cap; the bot's own
   pacing is a second, softer guard.
3. **API keys** -> create a key named "kaleida-discord" -> copy it (also a password).

### 3. Host it - recommended: Railway (simple, about $5/month)
Railway runs a small always-on program from a GitHub repository; no server to look after. (Check its current Hobby plan
price - it has been about $5/month, which covers a bot this size.)

1. **Give the bot its own GitHub repository** (for example `kaleida-discord-bot`) and copy this folder's files into it.
   Don't point a host at the game repo: it is many gigabytes and full of things that have no business on a server.
2. railway.com -> sign in with GitHub -> **New Project -> Deploy from GitHub repo** -> pick `kaleida-discord-bot`.
3. **Variables**: add `DISCORD_TOKEN` and `ANTHROPIC_API_KEY` (from steps 1 and 2), and optionally the other settings from
   `.env.example` (`MOD_MODE=shadow` is the default). Never put these in a file in the repository.
4. It starts with `python bot.py` (the `Procfile`). The **Deployments -> Logs** view should show
   `logged in as Spirekeeper#... - mode shadow`.
5. In Discord, type `!kaleida status` - it answers with the mode and today's spend.

**Cheaper alternative:** a small Linux VPS (Hetzner's smallest is a few euros a month) running the bot as a systemd
service. Cheaper, but you maintain the machine. Railway is the right call while you are short on time.

Note: on Railway the bot's `spend.json` resets when it redeploys, so the daily pacing starts fresh after an update. The
console's $10 limit still holds.

### Run it on your own PC (for testing)
```
pip install -r requirements.txt
copy .env.example .env      (then fill in the two keys)
python bot.py
```

## What it costs (estimates)

The model is Claude Sonnet 5.5 at low effort (Khai 2026-10-06: Sonnet for the bot), and the rules and FAQ are cached between calls. These are estimates; check
real numbers in the Anthropic console after the first week:
- a moderation read: roughly 0.3-1 cent (cheaper once the cache is warm);
- a community answer: roughly 0.5-1 cent;
- $10/month covers roughly 30-60 calls a day, plenty for an early community.

If spend runs high, check `!kaleida status`, lower `NEW_MEMBER_DAYS` so fewer messages are read, or ask Claude Code to add
a cheaper model for the moderation pass - that is your call to make.

## Files

| File | What it is |
|---|---|
| `bot.py` | The Discord side: which messages get read, what happens to a verdict, the queue buttons, the commands |
| `posts.py` | The posting jobs: drafts and their Post / Edit / Cancel buttons, the rules post, the role menu, the welcome |
| `util.py` | Small shared helpers: who counts as staff, finding a channel, the brand colour |
| `brain.py` | The Claude side: the two prompts, the structured verdict / answer, the budget guard |
| `knowledge/rules.md`, `knowledge/faq.md` | What the bot knows. Edit these, then `!kaleida reload` |
| `knowledge/roles.json`, `knowledge/welcome.md` | The role menu and the welcome text. Edit, then `!kaleida post roles` |
| `.env.example` | Every setting, with comments |
| `Procfile` | Tells the host how to start the bot |

## Known limits (v1)
- Buttons survive restarts (the mod-queue buttons too, since the posting update), but an Edit box left open across a
  restart has to be reopened.
- It reads text only - images and attachments are not checked (Discord's AutoMod and explicit-media filter cover those).
- Messages from established members are read only when someone flags them.
