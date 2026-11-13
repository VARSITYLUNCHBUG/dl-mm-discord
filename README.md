## Acknowledgments
Thank you to \_bacon_ for the base build. This project started from their Bebot draft bot. Without their work made, PUGs would in rigor mortis and none of our current community would be possible.

A big shout out to the ZA Deadlock community for playing in the inhouses, giving feedback and helping shape how the bot works.

---
# dl-mm-discord

A Discord bot that runs a 6v6 Deadlock inhouse from queue to results: FIFO queue, ready check,
captains, player draft, champion pick/ban, lobby code, screenshot reporting, OpenSkill ratings
with MVP and key-player awards, and optional score OCR.

Python, Django and Postgres. One codebase and one image, running as two processes: `bot`
(discord.py) and `web` (Django admin only).

The [wiki](https://github.com/VARSITYLUNCHBUG/dl-mm-discord/wiki) has the longer guides: setup,
match flow, draft modes, subs, ratings, commands, architecture, operations and development.

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
| Captain vote | Everyone votes for up to two captains, themselves included, from players with 5 completed matches. Most votes wins; rating breaks ties and fills any gap. Two or fewer eligible: no vote | 45 s, or closes early once everyone has voted |
| Player draft | `FSSFFSSFFS`, lower-rated captain first. Last player is assigned | 45 s, then highest rated |
| Team voice | Creates team channels and moves anyone in the lobby voice. Everyone else gets a DM invite | Never blocks the draft |
| Sides | A captain presses Become Hidden King captain or Become Archmother captain. The first press decides both sides | 20 s, then a coin flip |
| Champion draft | The mode's ban and pick order. Hidden King bans and picks first. 🎲 Random takes a champion on purpose | 40 s a turn. A timeout takes a random champion, for bans too |
| Draft summary | Named like "Crimson Wolf", with a GIF from `presets/draft_gifs.json` and GLHF, above the lobby code prompt | |
| Lobby code | A random captain enters it; it's posted with the settings checklist. With automatic lobbies, deadlock-api's bot creates it and a captain presses Start match | 3 min, then the other captain, then cancel |
| Report | Press the team that won, then add the screenshot, and optionally the Deadlock match ID, MVP and key players. With score OCR on, the screenshot is read | 3 h without a report goes to staff |
| Confirm | The other captain confirms, disputes or adds a screenshot | 5 min. Silence confirms |
| Results | First screenshot, the rating table, 🏅 MVP and ⭐ key players, and scoreboard stats go to the results channel. Every screenshot goes to the archive | Effects retry |

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
player goes back into the queue. Subbing yourself out after the ready check is a dodge, and once the
game is on it's a leaver; cancelling the request costs nothing. See Captains and dodge strikes. At most 4 requests can be pending at once. All of these timers
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

## Ratings

OpenSkill Plackett-Luce (`league/domain/openskill_v1.py`). Each player has a skill estimate (mu,
starting at 25) and an uncertainty (sigma, starting at 8.33). After the library's update:

| Rule | Starting value |
|---|---|
| Placement: a player's first rated matches | 10 |
| During placement, a game moves mu by at most | 3 |
| MVP share of a win / of a loss | ×1.20 / ×0.80 |
| Key player share of a win / of a loss | ×1.10 / ×0.90 |
| Most an award moves one player's mu in a game | 0.5 |
| Awards apply | after placement |
| Sigma never drops below | 2 |
| Sigma grows, in quadrature, per 14 days without a game, up to 6 | 0.5 |

Awards are zero-sum within each team: the MVP and key players take a bigger share of their team's
update, and their teammates a smaller one, so a team's total change is what it would have been
without them. The shown rating is the conservative `max(0, mu − 3 × sigma) × 40`. A new player
shows 0 and climbs as the system becomes sure of them, and a high seed (`/rating seed band:high`,
mu 35) also starts low on purpose. Players show "P n/10" during placement. Tiers are
Bronze, Silver from 10, Gold 18, Platinum 25 and Diamond 30, on `mu − 3 × sigma`.

Captain tie-breaks, the first player pick and auto-picks compare mu, not the shown rating, so a
strong player still in placement isn't treated as weak. After the player draft, each team heading
on the board shows its chance to win, with ⚠️ outside 40–60%. That is information only.

All the values live in each season's `rating_params`, over the defaults, and are fixed once the
season has rated a match. Start a new season with fresh ratings, optionally with new values:

```bash
uv run python manage.py start_season --params '{"beta": 3.5}'
```

The values are judgement, not measured. Check them on the staff dashboard, or with:

```bash
uv run python manage.py rating_calibration
```

```bash
uv run python manage.py rating_calibration --params '{"beta": 3.5}'
```

```bash
uv run python manage.py rating_calibration --simulate 300
```

The first prints Brier score and log loss against always saying 50%, calibration bins and the
Hidden King win rate with 95% intervals. The second compares candidate parameters without writing
anything. The third runs a synthetic league.

**Placement counts games, not sigma.** With 12 players a match, sigma shrinks slowly: in a 40-match
synthetic league it was still about 7.1 after 24 games, from 8.33. So the cap and the awards switch
at `placement_games` rated matches instead.

Ratings are a ledger, not a column. `rating_events` is append-only and enforced by database
triggers, and `player_ratings` is a projection that can be rebuilt from it. Rows carry exact
decimal `mu_delta` and `sigma_delta`, so the projection is the start plus the sum. A correction,
including awards that arrive late, appends differences and never rewrites history.
`/match explain` shows the trail. Migrations are generated by `makemigrations` and never edited:
the triggers are installed after every `migrate` by `league/db_guards.py`.

## deadlock-api: Steam links, result checks and lobbies

[deadlock-api.com](https://deadlock-api.com) is a community-run API for Deadlock match data. It's
unofficial, so everything here degrades to the manual flow when it fails. `DEADLOCK_API_KEY` is
optional and raises its limits. Staff manage it with `/deadlock settings` and `/deadlock status`.

**Steam links (optional).** `/link` takes a Steam profile link, SteamID64 or friend code and shows
the Steam name to confirm. Custom profile links (`/id/name`) need `STEAM_WEB_API_KEY`. One Steam
account per player and one player per account. A link becomes verified the first time
deadlock-api shows that account on the player's own team. `/unlink` removes it, and staff can
`/deadlock unlink` anyone. Linked Steam names also help score OCR match scoreboard names.

**Result checks** (on by default). When a match has a Deadlock match ID, from the bot's lobby, a
captain's report or the scoreboard, its metadata is fetched once a result is reported. deadlock-api
only has a custom match after someone's game client sends it in, so a miss is retried for about
four hours. Then:

| deadlock-api shows | What happens |
|---|---|
| The other team won | An unconfirmed result goes to disputed; a confirmed one pings staff |
| A linked player on the other team, or none of the linked players | Staff are pinged |
| Everything agrees | Links are verified, and the game's MVP and key players (`mvp_rank` 1 to 3) become the awards when all three are linked players |

Nothing is overturned automatically. Staff can set or correct a match's Deadlock ID, and check it
again at once, with `/deadlock check`. Awards from deadlock-api replace a captain's picks and the
scoreboard's, but never staff's. The results post shows each linked player's hero, K/D/A and
damage from the API when there was no scoreboard read.

**Automatic lobbies** (off by default; `/deadlock settings auto_lobby:true region:south_africa`).
When the champion draft ends, deadlock-api's bot creates the custom lobby and the code is posted.
Players join and sit on their side, then a captain presses **Start match** on the board. The bot
leaves the lobby 15 minutes after creating it, whatever the lobby is doing, so start within that
window. If the lobby can't be made, the host gets the usual prompt to create one and enter the
code. Once the match starts, its Deadlock match ID is fetched, so the result can be checked without
anyone typing it. Without an API key, deadlock-api allows 10 lobbies an hour from one IP.

## Calibration dashboard

`/admin/calibration/`, behind the Django staff login (the header's View site link goes there). It
shows, per season:

- Brier score and log loss against always saying 50%
- a calibration chart of predicted chance against how often it came true, with 95% intervals
- the favourite's and Hidden King's win rates
- how many players are still in placement, and the spread of uncertainty
- whether awards mean anything: how award holders do in their next 5 matches against the
  prediction, next to everyone else
- whose award share is frozen, and how many players left a game early
- awards by source, the most MVPs, score OCR reads and deadlock-api checks

Paste candidate parameters to see what they would have done. Every number is replayed in memory,
so nothing is written.

## MVP and key players

Deadlock's scoreboard marks one MVP and two key players. They reach the rating in four ways, and
a later one replaces an earlier one only if it ranks at least as high:

1. **The report form.** Optional MVP and Key players pickers. The confirming captain's picks
   replace the reporter's. Awards never cause a dispute.
2. **Score OCR**, when it reads all three badges cleanly and has matched each to a player.
3. **deadlock-api**, from the game's own MVP ranking, when all three are linked players.
4. **Staff**, with `/match awards`. Nothing replaces a staff decision except staff.

Before the result is final, awards are only recorded and the result is rated with them. After,
they go through the same refold as a rescore, and the results post is edited in place.

**MVP farming.** A player whose MVP rate, over 20 or more rated matches, is more than twice what
chance gives (1 in 12) keeps playing normally but gets no award share until the rate drops back.
Staff are pinged the first time. It is worked out from the same history on every replay, so a
later award correction can lift or add a freeze. The values are `farming_min_games`,
`farming_ratio` and `mvp_expected` in the season's parameters.

**Do awards mean anything?** The dashboard and `rating_calibration` compare how award holders do in
their next 5 matches against the rating's own prediction, next to everyone else. If award holders
keep beating it, awards carry skill the rating hasn't caught yet and the share could grow a
little. If the interval spans zero, keep it small, or set the multipliers to 1 to turn it off.

## Score OCR

`scoreocr/` reads a report screenshot: winner, Deadlock match ID, duration, and each player's
name, hero, K/D/A, souls, damage, healing and badge. It's optional, because its dependencies are
large. Build with it:

```bash
INSTALL_OCR=true docker compose up -d --build
```

Then choose a mode in Discord with `/ocr mode`. `/ocr status` shows whether it's installed and how
reads have gone.

| Mode | What a read does |
|---|---|
| `off` (default) | Nothing is read |
| `shadow` | Stores the stats and shows them on the results post. Changes nothing else |
| `on` | Also sets the MVP and key players, fills a Deadlock match ID the captain left blank, and disputes the report if the scoreboard shows the other team won |

The read runs after the report, off the board's path. The scoreboard shows in-game names, not
Discord names, so each team's six rows are matched to its six players by name similarity. In-game
names a captain or staff has confirmed before match exactly. When the match isn't clear-cut, those
rows stay unmatched and the match channel gets a **Match names** button for the captains. Staff can
do it later with `/match names`. Every answer is remembered for next time. A read that fails, or
isn't a scoreboard, is recorded and the result stands without it.

## Captains and dodge strikes

**Captains need 5 completed matches** in this server, across seasons, so a newcomer can't be
troll-picked. A player carrying 3 or more strikes can't captain either. Both values are in the draft
mode's `captains` section (`min_games`, `max_strikes`). On the board, players who can't captain show
🔒 and their count, and can't be voted for. With two or fewer eligible players in a lobby, the vote
is skipped: those players captain and any gap is filled by the most completed matches. If a captain
subs out and the sub can't captain, the teammate with the most completed matches takes over. Staff
can `/captain allow`, `/captain bar` or `/captain clear` anyone.

**Strikes** make repeat offences cost more, and playing works them back off. Each offence adds
strikes: missed ready check 1, dodge 2, leaver 3, false report 3. The cooldown comes from the
kind's ladder, one rung higher for every strike the player already carries, from any kind:

| Kind | Cooldown by rung, in minutes |
|---|---|
| Missed ready check | 5, 15, 60, 180, 720 |
| Dodge | 60, 360, 1440, 4320, 10080 |
| Leaver | 120, 720, 2880, 10080 |
| False report | 1440, 4320, 10080 |

Every 3 clean completed matches removes a strike, and so does each 14 days without an offence. The
player is told when a strike comes off, and the queue's refusal and `/stats` say how far they are
from the next. Subbing yourself out after the ready check but before the game starts is a dodge;
during the ready check, or when your captain asks for it, it isn't, and cancelling the request
before it's filled costs nothing. Once the game is on it's a leaver, as before.

**Early leaves.** While the game is on, a captain can press **Someone left in the first 5 min**
and pick who. If the other captain agrees, the match is voided with no rating change: the leaver
gets a leaver penalty and stays out of the queue, and everyone else goes back in. A disagreement,
or no answer in 2 minutes, goes to staff, who can void it with `/match early_leave`. Reports are
open for 20 minutes after the lobby code, or after the bot lobby starts. These timers are the draft
mode's `results` values `early_leave_minutes`, `early_leave_report_minutes` and
`early_leave_answer_seconds`.

**Left early** is marked on a player's match record when they sub out once the game is on, leave
early as above, or show as abandoning in deadlock-api's match data. An abandon inside the first 5
minutes is put to staff.

Strikes are worked out from the facts each time: penalties, completed matches and staff changes. So
voiding a match also takes back the clean game it counted for. `/penalty revoke` removes the
cooldown's strikes too, unless told to keep them, and `/penalty strikes` adds or removes them by
hand. All the numbers are in each server's `penalty_policy`, editable in admin. They are starting
judgements.

## Commands

**Players:** `/leaderboard`, `/stats`, `/rating history`, `/queue status`, `/link`, `/unlink`,
`/submit` (fallback for when the report form's upload fails).

**Staff:** `/setup`, `/match status|void|rescore|sub|explain|awards|names|early_leave`,
`/rating seed|adjust`, `/queue kick|clear`, `/penalty add|revoke|strikes`,
`/captain allow|bar|clear`, `/ocr mode|status`, `/deadlock settings|status|check|unlink`,
`/mode list|activate`, `/roster sync`, `/ledger_check`.
Anything that rewrites past ratings shows a preview and needs Confirm.

## Tables

| Group | Tables |
|---|---|
| Players and ratings | `players`, `seasons`, `player_ratings`, `rating_runs`, `rating_events` |
| Champions and config | `champions`, `draft_mode_versions`, `communities` |
| Queue | `queue_entries` |
| Matches | `matches`, `match_participants`, `match_resources`, `match_transitions`, `draft_actions`, `captain_votes`, `sub_requests`, `sub_offers`, `early_leaves` |
| Results | `result_submissions`, `match_results`, `match_awards`, `scoreboard_reads`, `player_match_stats`, `player_aliases`, `api_match_checks` |
| Operations | `penalties`, `strike_adjustments`, `captain_overrides`, `outbox_effects` |

For analytics, start at `result_submissions`. Screenshots are in the archive channel: use
`archive_message_id` and `attachment_id` with the Discord API, because CDN URLs expire. Join
`draft_actions` for picks and bans, `rating_events` for ratings and `player_match_stats` for
scoreboard stats. `deadlock_match_id`, when given,
works with `GET https://api.deadlock-api.com/v1/matches/{id}/metadata`.

## Code layout

```
league/domain/    Pure rules, no Django or Discord: ratings (openskill_v1), strikes and
                  captain eligibility (standing), Steam IDs, mode schema, draft plan, engine
league/models/    The tables
league/services/  Transactions: queue, lobby, draft, results, ratings, corrections, awards,
                  standing, penalties, ocr, steam, api_results, calibration, deadlines
league/integrations/  The deadlock-api client. Network only, no database
league/web/       The staff calibration dashboard
league/admin/     Django admin
bot/handlers/     Clicks and modals: validate, call a service, acknowledge
bot/commands/     Slash commands
bot/renderers/    State to Discord components, with no I/O
bot/effects/      Runs Discord side effects from the outbox, with retries
scoreocr/         Scoreboard screenshot reader, used through league/services/ocr.py when installed
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

Join the queue yourself as the twelfth. Fake players have no completed matches, so nobody here can
captain under the 5-match rule and captains are chosen by experience. To captain your own test
lobby, run `/captain allow` on yourself, then vote for yourself. Fake captains' turns and side claims time out on the normal clocks, so a match still reaches
you. The bot process needs `DEV_FAKE_PLAYERS=true` too. It's refused outside `DEBUG`.

To watch a whole match with no clicking, run this alongside the bot. It puts every real player who
has played into the lobby, holds at each stage for screenshots, and reuses an archived scoreboard, so
one match must already have been reported by hand:

```bash
DEBUG=true DEV_FAKE_PLAYERS=true uv run python manage.py dev_autoplay --pause 20 --step 2
```

It stands in for the people clicking, so it doesn't test the buttons themselves.

## Check before launch

- Rating convergence. In simulation a strong player seeded as new takes 29 to 81 games to reach
  mu 30. A 6v6 game moves mu well under the 3-point placement cap, so the cap isn't what slows it:
  the per-game step is, and `beta` barely changes it with 12 players. Seeding known players with
  `/rating seed` helps most. A higher starting `sigma` helps a little (12 gave 15 to 72 games) but
  keeps shown ratings at 0 for longer; try it with
  `rating_calibration --simulate 300 --params '{"sigma": 12}'`.
- deadlock-api's custom lobby start uses the party ID from creation as the lobby ID. Confirm it
  with one real lobby, and that the bot's lobby can be joined and started as described.
- Night Shift's real turn order, from a #62 or #63 VOD.
- Voice invite links and modal screenshot upload on desktop, mobile and web.
- Current custom-lobby steps, for the host checklist text in `bot/renderers/board.py`.
- Behaviour after Discord's 16 November 2026 channel change. Every channel the bot creates already
  grants the bot its own `VIEW_CHANNEL`.
