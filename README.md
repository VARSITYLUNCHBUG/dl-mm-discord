## Acknowledgments
Thank you to \_bacon_ for the base build. This project started from their Bebot draft bot. Without their work made, PUGs would in rigor mortis and none of our current community would be possible.

A big shout out to the ZA Deadlock community for playing in the inhouses, giving feedback and helping shape how the bot works.

---
# dl-mm-discord

A Discord bot that runs a 6v6 Deadlock inhouse from queue to results: FIFO queue, ready check,
captains, player draft, champion pick/ban, lobby code, screenshot reporting and Elo.

Python, Django and Postgres. One codebase and one image, running as two processes: `bot`
(discord.py) and `web` (Django admin only).

The [wiki](https://github.com/VARSITYLUNCHBUG/dl-mm-discord/wiki) has the longer guides: setup,
match flow, draft modes, subs, Elo, commands, architecture, operations and development.

## Run it

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications). No privileged intents
   are needed.
2. Invite it, replacing `YOUR_APP_ID`:
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=281475298741265
   ```
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `DISCORD_GUILD_ID`,
   `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`. Use the same password in `DATABASE_URL`.
   Generate a secret key with:
   ```bash
   uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```
4. Start everything. This also runs migrations and loads the presets:
   ```bash
   docker compose up -d --build
   ```
5. In Discord, run `/setup` (optionally `staff_role:@YourStaffRole`). It creates the queue, Waiting
   Room, results and archive channels, and it is safe to run again.
6. Create an admin login for `http://127.0.0.1:8000/admin/`:
   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

The admin binds to localhost. Put a TLS reverse proxy in front of it before exposing it, and set
`BEHIND_TLS_PROXY=true`, `SECURE_COOKIES=true` and `CSRF_TRUSTED_ORIGINS`.

## How a match runs

| Step | What happens | Clock and fallback |
|---|---|---|
| Queue | Join and Leave buttons, FIFO. Voice isn't required | Blocked by a cooldown or a live match |
| Pop at 12 | Creates a private text channel and lobby voice channel | Queue waits at `max_concurrent_matches` |
| Gather | In the Waiting Room: moved. Anywhere else: DM with invite and Ready button. DMs closed: mention in the lobby channel | Joining the Waiting Room later moves them |
| Ready | Ready button on the board and in the DM | 180 s. A miss brings in the next in queue. No replacement: cancel and requeue |
| Captain vote | Everyone votes for up to two captains, themselves included. Most votes wins; Elo breaks ties and fills any gap | 45 s, or closes early once everyone has voted |
| Player draft | `FSSFFSSFFS`, lower-rated captain first. Last player is assigned | 45 s, then highest rated |
| Team voice | Creates team channels and moves anyone in the lobby voice. Everyone else gets a DM invite | Never blocks the draft |
| Sides | A captain presses Become Hidden King captain or Become Archmother captain. The first press decides both sides | 20 s, then a coin flip |
| Champion draft | The mode's ban and pick order. Hidden King bans and picks first. 🎲 Random takes a champion on purpose | 40 s a turn. A timeout takes a random champion, for bans too |
| Draft summary | Named like "Crimson Wolf", with a GIF from `presets/draft_gifs.json` and GLHF, above the lobby code prompt | |
| Lobby code | A random captain enters it; it's posted with the settings checklist | 3 min, then the other captain, then cancel |
| Report | Press the team that won, then add the screenshot and optional Deadlock match ID | 3 h without a report goes to staff |
| Confirm | The other captain confirms, disputes or adds a screenshot | 5 min. Silence confirms |
| Results | First screenshot and the Elo table go to the results channel. Every screenshot goes to the archive | Effects retry |

This is the flow the server used with Bebot and NeatQueue, and it is the only one: every match
runs it. The timers and the ban and pick order live in the draft mode. These values are starting
judgements, not published standards.

| Mode | Champion draft |
|---|---|
| `bebot_2ban` (default) | Bebot's 16-step, 2-ban order, 40 s a turn |
| `dcs_s3_2ban` | The same 16 steps, 30 s a turn |
| `threeban_1win_2026` | The 1win 18-step table, 3 bans a team, 30 s a turn |

Switch with `/mode activate slug:<slug>`. Bebot's custom hero emojis are in
`presets/champion_emojis.yaml`. Load them with `manage.py load_champion_emojis`. The bot must be in
the server that owns them.

### Subs

Any player can press **Request a sub** on the board from the ready check until a result is
reported. There are two separate flows:

| Flow | What happens | Clock and fallback |
|---|---|---|
| Sub out, yourself | Opens the slot straight away | The match pauses until the slot is filled |
| Sub out, a teammate | A captain asks for one of their own players or an undrafted one. Not another captain | The other captain approves within 60 s, or the player stays |
| Sub in, named | The leaving player or their captain picks someone, who gets a DM to accept | 45 s to name someone, 60 s for them to accept |
| Sub in, queue | If nobody is named, or the named person says no, the queue is asked in order | 30 s each, and the queue is retried as people join |

While any request is pending, the timer is parked and the board hides the phase's buttons (except
the result buttons once the game is on). The sub takes the leaving player's team, captaincy,
lobby-host role and result. The timer resumes where it stopped, with at least 15 s. Nobody in
3 minutes means the match is cancelled with no rating change, and everyone except the leaving
player goes back into the queue. Subbing out once the game is on gives a leaver cooldown;
cancelling the request doesn't. At most 4 requests can be pending at once. All of these timers
live in the draft mode's `subs` section.

## The match channel

Players can chat freely, and the board stays at the bottom:

- **Pings** sit just above the board. A status change deletes the earlier ones, and so does a
  new lobby host or a repeat staff request. Chat is never deleted.
- **A state change** edits the board if it's the last message. Otherwise a new board is posted
  at the bottom and the old one deleted.
- **Chat** moves the board once three messages sit below it and chat has been quiet for 5 s, or
  20 s after the third message if chat doesn't stop. A board that moves mid-click could cost a
  captain a wrong ban, so it doesn't move on every message. Change this with the `BOARD_MOVE_*`
  values in `league/services/effects.py`.
- **Choices are buttons**, so everything is visible at once. Drafted players and taken champions
  stay in place, greyed out, so nothing shifts under a click. The champion grid has messages of
  its own directly below the board, split evenly over two because Discord allows 40 components per
  message, with Random at the end. They are posted afresh each turn rather than edited, so no
  "(edited)" tag splits the grid. A roster above 55 falls back to dropdowns.

The bot only learns that a message was sent. It doesn't have the Message Content intent, so it
can't read chat.

## Adding things without code

**A champion.** In admin, add a row under Champions, using the
[Deadlock API](https://api.deadlock-api.com/v1/assets/heroes?only_active=true) hero ID. Or run
`/roster sync`, which shows the differences and asks before applying them. Disable champions rather
than deleting them. New lobbies use the change; live matches keep the pool they started with.

**A draft mode.** In admin, open a mode under Draft mode versions, edit the JSON and save. Saving
always writes a new version, and it is refused with a readable error if the format can't work. Then
run `/mode activate slug:<slug>`. Live matches keep the version they started with. To ship one with
the code, add a YAML file to `presets/`; `presets/bebot_2ban.yaml` is the reference.

A mode sets the timers, the team size, the player draft order and the champion ban and pick order,
and can exclude champions. The flow itself isn't configurable. In the steps, `first` is the Hidden
King team.

Version 1 supports sequential bans and picks, with counts and per-step timers. Simultaneous and blind
steps are rejected until the engine implements them.

**Night Shift's real turn order.** `threeban_1win_2026` is a labelled candidate: Night Shift's exact
order is unconfirmed. If a #62 or #63 VOD shows a different order, publish a new version and set
`night_shift_status: confirmed`. The board drops its "unconfirmed" note by itself.

## Elo

`elo_v1`, in `league/domain/elo.py`. Team-average Elo with one shared integer delta:

```
E_A = 1 / (1 + 10^((avg_B - avg_A) / 400))
d   = floor(K * (S_A - E_A) + 0.5)
Team A += d, Team B -= d
```

Start 1000 and K 32, set per season. K = 32 is a judgement, so review it after about 200 matches.
Use `/rating seed` for players of known skill.

Ratings are a ledger, not a column. `rating_events` is append-only and enforced by database
triggers, and `player_ratings` is a projection that can be rebuilt from it. A correction appends
differences and never rewrites history. `/match explain` shows the trail.

## Commands

**Players:** `/leaderboard`, `/stats`, `/rating history`, `/queue status`, `/submit` (fallback for
when the report form's upload fails).

**Staff:** `/setup`, `/match status|void|rescore|sub|explain`, `/rating seed|adjust`,
`/queue kick|clear`, `/penalty add|revoke`, `/mode list|activate`, `/roster sync`, `/ledger_check`.
Anything that rewrites past ratings shows a preview and needs Confirm.

## Tables

| Group | Tables |
|---|---|
| Players and ratings | `players`, `seasons`, `player_ratings`, `rating_runs`, `rating_events` |
| Champions and config | `champions`, `draft_mode_versions`, `communities` |
| Queue | `queue_entries` |
| Matches | `matches`, `match_participants`, `match_resources`, `match_transitions`, `draft_actions`, `captain_votes`, `sub_requests`, `sub_offers` |
| Results | `result_submissions`, `match_results` |
| Operations | `penalties`, `outbox_effects` |

For analytics, start at `result_submissions`. Screenshots are in the archive channel: use
`archive_message_id` and `attachment_id` with the Discord API, because CDN URLs expire. Join
`draft_actions` for picks and bans, and `rating_events` for Elo. `deadlock_match_id`, when given,
works with `GET https://api.deadlock-api.com/v1/matches/{id}/metadata`.

## Code layout

```
league/domain/    Pure rules, no Django or Discord: elo, mode schema, draft plan, engine
league/models/    The tables
league/services/  Transactions: queue, lobby, draft, results, ratings, corrections, deadlines
league/admin/     Django admin
bot/handlers/     Clicks and modals: validate, call a service, acknowledge
bot/commands/     Slash commands
bot/renderers/    State to Discord components, with no I/O
bot/effects/      Runs Discord side effects from the outbox, with retries
scoreocr/         Standalone scoreboard screenshot reader (not used by the bot yet)
```

The rules that keep it reliable:

- **Handlers are stateless.** A `custom_id` holds the action, match and step, and the rest comes from
  the database. Old buttons survive a restart, and a stale click gets a polite refusal.
- **Races end in the database.** Row locks, `version` and `step_index` checks, and unique keys.
- **Timers are rows.** A poller applies timeouts through the same path as a click. After downtime,
  past-due deadlines get 60 s of grace.
- **Discord calls happen after commit.** State changes write `outbox_effects` in the same transaction,
  and a worker runs them in order per match, retrying on server errors.

## Operations

```bash
docker compose logs -f bot
```

```bash
docker compose exec bot python manage.py check_ledger
```

```bash
docker compose exec db pg_dump -U draftbot draftbot > backup.sql
```

`rebuild_ratings` recreates `player_ratings` from the ledger. It never writes to the ledger itself.
Back up the database daily: the volume is the only copy of match history and ratings.

Check the Outbox effects page in admin, filtered to `failed`, if channels aren't being created or
cleaned up. The bot's healthcheck fails if its loops stop updating the heartbeat file.

## Development

Postgres runs in Docker on port 55432. System Python isn't needed; `uv` manages it.

```bash
docker compose up -d db
```

```bash
DEBUG=true uv run pytest
```

```bash
uv run ruff check bot league config tests manage.py && uv run ruff format --check bot league config tests manage.py
```

```bash
DEBUG=true uv run mypy bot league config
```

Run the bot on your machine, without building an image:

```bash
DEBUG=true uv run python manage.py migrate
```

```bash
DEBUG=true uv run python manage.py load_presets
```

```bash
DEBUG=true uv run python manage.py runbot
```

### Testing alone

With `DEBUG=true` and `DEV_FAKE_PLAYERS=true`, this fills the queue with fake players. They ready up
by themselves, and Discord calls for them are skipped:

```bash
DEBUG=true DEV_FAKE_PLAYERS=true uv run python manage.py dev_fill_queue --count 11
```

Join the queue yourself as the twelfth. Fake players don't vote, so to be a captain, vote for
yourself or give yourself the top rating with `/rating seed`: the vote times out and Elo fills the
gap. Fake captains' turns and side claims time out on the normal clocks, so a match still reaches
you. The bot process needs `DEV_FAKE_PLAYERS=true` too. It's refused outside `DEBUG`.

To watch a whole match with no clicking, run this alongside the bot. It puts every real player who
has played into the lobby, holds at each stage for screenshots, and reuses an archived scoreboard, so
one match must already have been reported by hand:

```bash
DEBUG=true DEV_FAKE_PLAYERS=true uv run python manage.py dev_autoplay --pause 20 --step 2
```

It stands in for the people clicking, so it doesn't test the buttons themselves.

## Check before launch

- Night Shift's real turn order, from a #62 or #63 VOD.
- Voice invite links and modal screenshot upload on desktop, mobile and web.
- Current custom-lobby steps, for the host checklist text in `bot/renderers/board.py`.
- Behaviour after Discord's 16 November 2026 channel change. Every channel the bot creates already
  grants the bot its own `VIEW_CHANNEL`.
