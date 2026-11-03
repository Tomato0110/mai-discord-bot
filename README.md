# Mai — Discord Bot

**Application ID:** 1222381695911854091 · **Bot user:** Mai #4520 · **Installed in:** 28 servers
**Implementation:** Python (discord.py), a single production source file (`mein.py`, 1,701 lines), maintained by an individual developer.

Mai is a multi-purpose bot built for an Albion Online guild community.

## Features

- **Virtual-currency economy** — balances, transfers between members, admin grants, tip totals and VIP ranks (stored locally as JSON)
- **Mini-games** — baccarat, high-and-low, coin-flip style wagering against the bot economy
- **Albion Online data** — player search, item icons, and guild Fame rankings (daily / weekly / monthly) via the official Albion Online game-info API
- **Moderation** — timeout, untimeout, ban, channel purge ("nuke"), and a ticket feature
- **Trade coordination** — a multi-step flow in dedicated channels: the bot reads a numeric amount typed by the buyer, then verifies a payment link posted by the seller
- **Favorites by reaction** — reacting to any message with 👍 makes the bot DM that message's content to the reacting user
- **Utilities** — invite tracking, server list, rotating status showing server and member totals

## Privileged Intent usage

This repository supports the bot's Privileged Gateway Intent application. See:

- [Feature → Intent map (image)](evidence/feature-map.svg)
- [Code excerpts from the production source (image)](evidence/code-excerpts.svg)

### Message Content Intent — required
Core features are operated from ordinary channel messages: classic `!` text-prefix commands; the trade flow's `wait_for("message")` steps that read a typed amount (`content.isdigit()`) and verify a posted payment link; and the reaction-favorites feature that fetches and DMs message content. Without this intent the content arrives empty and these features cannot work at all.

### Server Members Intent — required
Economy and moderation commands act on `discord.Member` targets (give / tip / add_coins, timeout / ban). The favorites feature resolves members with `guild.get_member(...)`, and the bot's status aggregates `guild.member_count`. Reliable member resolution requires this intent.

### Presence Intent — NOT requested
The source sets the presence flag, but the activity-monitoring feature it was reserved for was never implemented (only placeholder variables exist). We do not request intents the bot does not actually use.

## Why the bot is currently offline

The bot logs in with `intents=discord.Intents.all()`. After the app's privileged intent access was removed (the review-request deadline was missed), discord.py refuses to log in at all (`PrivilegedIntentsRequired`). Restoring the two intents above through this review will bring the bot back.

## Privacy

See [PRIVACY.md](PRIVACY.md).
