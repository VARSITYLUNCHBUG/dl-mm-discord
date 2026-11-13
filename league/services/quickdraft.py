"""Bebot's standalone draft: /startdraft and /captains_coinflip, in any channel.

Two captains ban and pick heroes in the active mode's order, one message, no ratings. It keeps
the match flow's guarantees: one row lock per draft, one row per step, timers as rows the poller
fires, and Discord edits through the outbox.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction

from league.domain.draft_plan import Role
from league.domain.engine import auto_champion, draft_name, pick_gif, seeded_rng
from league.domain.errors import DomainError, InvalidAction, NotAllowed, StaleAction
from league.domain.mode_schema import DraftMode, parse_mode
from league.models import Champion, Community, OutboxEffect, QuickDraft, QuickDraftAction
from league.models.outbox import EffectStatus
from league.models.quickdraft import QUICK_DRAFT_LIVE, QuickDraftStatus, Side
from league.services.common import add_effect, now, seconds_from_now
from league.services.gifs import draft_gifs

logger = logging.getLogger(__name__)

# The command posts the draft and saves its message ID straight away. A draft still without one
# after this long lost its message (the bot stopped in between, or the save failed), so it is
# closed to free the channel.
UNPOSTED_SECONDS = 60

# The coinflip choices. The winner picks any; the loser picks from the other kind.
FIRST_PICK = "first_pick"
LAST_PICK = "last_pick"
HIDDEN_KING = "hidden_king"
ARCHMOTHER = "archmother"
PICK_CHOICES = (FIRST_PICK, LAST_PICK)
SIDE_CHOICES = (HIDDEN_KING, ARCHMOTHER)
CHOICE_LABELS = {
    FIRST_PICK: "First Pick",
    LAST_PICK: "Last Pick",
    HIDDEN_KING: "Hidden King",
    ARCHMOTHER: "Archmother",
}


@dataclass(frozen=True, slots=True)
class HeroStep:
    index: int
    action: str  # "ban" or "pick"
    role: Role
    seconds: int


def hero_steps(mode: DraftMode) -> tuple[HeroStep, ...]:
    """The mode's ban and pick order, without the player draft."""
    steps: list[HeroStep] = []
    for step in mode.champion_draft.steps:
        seconds = step.seconds or mode.champion_draft.seconds
        for _ in range(step.count):
            steps.append(HeroStep(len(steps), step.action, step.team, seconds))
    return tuple(steps)


def side_for_role(role: str, first_side: str) -> str:
    if role == "first":
        return first_side
    return Side.ARCHMOTHER if first_side == Side.HIDDEN_KING else Side.HIDDEN_KING


def _mode(draft: QuickDraft) -> DraftMode:
    return parse_mode(draft.mode_version.body)


def _lock(draft_id: int) -> QuickDraft:
    draft = (
        QuickDraft.objects.select_for_update(of=("self",))
        .select_related("mode_version", "community")
        .filter(pk=draft_id)
        .first()
    )
    if draft is None:
        raise StaleAction("Draft not found!")
    return draft


def request_render(draft: QuickDraft) -> None:
    """Coalesces like the match board: a pending render reads the latest state anyway."""
    pending = OutboxEffect.objects.filter(
        kind="render_quick_draft",
        status=EffectStatus.PENDING,
        attempts=0,
        payload__draft_id=draft.pk,
    )
    if not pending.exists():
        add_effect(draft.community, "render_quick_draft", {"draft_id": draft.pk})


def _pool(mode: DraftMode) -> list[int]:
    excluded = set(mode.champion_draft.excluded_champion_ids)
    rows = Champion.objects.filter(is_enabled=True).order_by("name").values_list("pk", flat=True)
    pool = [pk for pk in rows if pk not in excluded]
    if len(pool) < len(hero_steps(mode)):
        raise InvalidAction("Not enough heroes are enabled to run a draft.")
    return pool


def live_in_channel(channel_id: int) -> QuickDraft | None:
    return QuickDraft.objects.filter(channel_id=channel_id, status__in=QUICK_DRAFT_LIVE).first()


def _create(
    community: Community, channel_id: int, started_by_id: int, **fields: object
) -> QuickDraft:
    if live_in_channel(channel_id) is not None:
        raise InvalidAction("A draft is already in progress in this channel!")
    mode_version = community.active_mode
    try:
        with transaction.atomic():
            return QuickDraft.objects.create(
                community=community,
                mode_version=mode_version,
                channel_id=channel_id,
                started_by_id=started_by_id,
                rng_seed=secrets.randbits(62),
                champion_pool=_pool(parse_mode(mode_version.body)),
                created_at=now(),
                **fields,
            )
    except IntegrityError as exc:
        raise InvalidAction("A draft is already in progress in this channel!") from exc


@transaction.atomic
def start(community_id: int, channel_id: int, started_by_id: int) -> QuickDraft:
    """/startdraft: post the two captain buttons. Hidden King bans and picks first."""
    community = Community.objects.select_related("active_mode").get(pk=community_id)
    return _create(community, channel_id, started_by_id, status=QuickDraftStatus.CAPTAINS)


@transaction.atomic
def start_coinflip(
    community_id: int,
    channel_id: int,
    started_by_id: int,
    first: tuple[int, str],
    second: tuple[int, str],
) -> QuickDraft:
    """/captains_coinflip: flip between two captains; the winner chooses first."""
    if first[0] == second[0]:
        raise InvalidAction("You can't flip a coin between the same player!")
    community = Community.objects.select_related("active_mode").get(pk=community_id)
    winner, loser = (first, second) if secrets.randbelow(2) == 0 else (second, first)
    return _create(
        community,
        channel_id,
        started_by_id,
        status=QuickDraftStatus.COINFLIP,
        coinflip_winner_id=winner[0],
        coinflip_winner_name=winner[1][:100],
        coinflip_loser_id=loser[0],
        coinflip_loser_name=loser[1][:100],
    )


def moved_message(draft_id: int, message_id: int) -> None:
    """The draft message was posted again below the chat; the old one is deleted next."""
    QuickDraft.objects.filter(pk=draft_id, status__in=QUICK_DRAFT_LIVE).update(
        message_id=message_id
    )


def record_grid(draft_id: int, message_ids: list[int]) -> None:
    QuickDraft.objects.filter(pk=draft_id).update(grid_message_ids=message_ids)


def attach_message(draft_id: int, message_id: int) -> bool:
    """Remember the draft's message. False if the draft was closed first, so the caller should
    take the message down."""
    updated = QuickDraft.objects.filter(
        pk=draft_id, status__in=QUICK_DRAFT_LIVE, message_id__isnull=True
    ).update(message_id=message_id)
    return bool(updated)


# --- Captains ------------------------------------------------------------------------------


@transaction.atomic
def claim_captain(draft_id: int, side: str, user_id: int, name: str, *, allow_both: bool) -> str:
    """A Become Captain button. Returns the side's name. Both claimed starts the hero draft."""
    draft = _lock(draft_id)
    if draft.status != QuickDraftStatus.CAPTAINS:
        raise StaleAction("Captain selection is already complete!")
    if side not in (Side.HIDDEN_KING, Side.ARCHMOTHER):
        raise InvalidAction("Invalid team name!")
    label = Side(side).label
    if draft.captain_id(side) == user_id:
        raise InvalidAction(f"You are already the captain of {label}!")
    other = Side.ARCHMOTHER if side == Side.HIDDEN_KING else Side.HIDDEN_KING
    if not allow_both and draft.captain_id(other) == user_id:
        raise InvalidAction("You cannot be the captain of both teams!")
    if draft.captain_id(side) is not None:
        raise InvalidAction(f"{label} already has a captain!")
    if side == Side.HIDDEN_KING:
        draft.hk_captain_id, draft.hk_captain_name = user_id, name[:100]
    else:
        draft.am_captain_id, draft.am_captain_name = user_id, name[:100]
    draft.version += 1
    draft.save(
        update_fields=[
            "hk_captain_id",
            "hk_captain_name",
            "am_captain_id",
            "am_captain_name",
            "version",
        ]
    )
    if draft.hk_captain_id is not None and draft.am_captain_id is not None:
        _begin(draft)
    request_render(draft)
    return label


# --- Coinflip -------------------------------------------------------------------------------


def coinflip_options(draft: QuickDraft) -> tuple[str, ...]:
    """What the chooser on turn can press: any of four for the winner, the other kind after."""
    if not draft.winner_choice:
        return (FIRST_PICK, LAST_PICK, HIDDEN_KING, ARCHMOTHER)
    return SIDE_CHOICES if draft.winner_choice in PICK_CHOICES else PICK_CHOICES


@transaction.atomic
def coinflip_choose(draft_id: int, user_id: int, choice: str) -> None:
    draft = _lock(draft_id)
    if draft.status != QuickDraftStatus.COINFLIP:
        raise StaleAction("The coinflip choices are already made.")
    winner_turn = not draft.winner_choice
    if winner_turn and user_id != draft.coinflip_winner_id:
        raise NotAllowed("Only the coinflip winner can make this choice!")
    if not winner_turn and user_id != draft.coinflip_loser_id:
        raise NotAllowed("Only the coinflip loser can make this choice!")
    if choice not in coinflip_options(draft):
        raise InvalidAction("That choice isn't available.")
    if winner_turn:
        draft.winner_choice = choice
    else:
        draft.loser_choice = choice
        _settle_coinflip(draft)
    draft.version += 1
    draft.save()
    request_render(draft)


def _settle_coinflip(draft: QuickDraft) -> None:
    """Bebot's check_and_show_start_button: turn the two choices into sides and first pick."""
    side_of = {HIDDEN_KING: Side.HIDDEN_KING, ARCHMOTHER: Side.ARCHMOTHER}
    winner_side = side_of.get(draft.winner_choice)
    loser_side = side_of.get(draft.loser_choice)
    if winner_side is None:
        winner_side = Side.ARCHMOTHER if loser_side == Side.HIDDEN_KING else Side.HIDDEN_KING
    if loser_side is None:
        loser_side = Side.ARCHMOTHER if winner_side == Side.HIDDEN_KING else Side.HIDDEN_KING
    if draft.winner_choice == FIRST_PICK:
        first = winner_side
    elif draft.winner_choice == LAST_PICK or draft.loser_choice == FIRST_PICK:
        first = loser_side
    elif draft.loser_choice == LAST_PICK:
        first = winner_side
    else:
        first = winner_side
    for side, user_id, name in (
        (winner_side, draft.coinflip_winner_id, draft.coinflip_winner_name),
        (loser_side, draft.coinflip_loser_id, draft.coinflip_loser_name),
    ):
        if side == Side.HIDDEN_KING:
            draft.hk_captain_id, draft.hk_captain_name = user_id, name
        else:
            draft.am_captain_id, draft.am_captain_name = user_id, name
    draft.first_side = first
    draft.status = QuickDraftStatus.READY


@transaction.atomic
def start_after_coinflip(draft_id: int, user_id: int) -> None:
    """The Start Draft button, once both captains have chosen."""
    draft = _lock(draft_id)
    if draft.status != QuickDraftStatus.READY:
        raise StaleAction("This draft has already started.")
    if user_id not in (draft.hk_captain_id, draft.am_captain_id):
        raise NotAllowed("Only the captains can start the draft.")
    _begin(draft)
    request_render(draft)


# --- Hero draft -----------------------------------------------------------------------------


def _begin(draft: QuickDraft) -> None:
    steps = hero_steps(_mode(draft))
    draft.status = QuickDraftStatus.DRAFTING
    draft.step_index = 0
    draft.deadline_at = seconds_from_now(steps[0].seconds)
    draft.version += 1
    draft.save(update_fields=["status", "step_index", "deadline_at", "version"])


def _current(draft: QuickDraft, step_index: int | None) -> HeroStep:
    if draft.status != QuickDraftStatus.DRAFTING:
        raise StaleAction("Draft is no longer accepting selections.")
    steps = hero_steps(_mode(draft))
    if step_index is not None and step_index != draft.step_index:
        raise StaleAction("Selection is already being processed.")
    if draft.step_index >= len(steps):
        raise StaleAction("Draft is no longer accepting selections.")
    return steps[draft.step_index]


def _require_turn(draft: QuickDraft, step: HeroStep, user_id: int) -> str:
    side = side_for_role(step.role, draft.first_side)
    if draft.captain_id(side) != user_id:
        raise NotAllowed(f"It's not your turn to {step.action}.")
    return side


def taken(draft: QuickDraft) -> set[int]:
    return set(draft.actions.values_list("champion_id", flat=True))


def available(draft: QuickDraft) -> list[int]:
    used = taken(draft)
    return [champion for champion in draft.champion_pool if champion not in used]


def _record(
    draft: QuickDraft,
    step: HeroStep,
    side: str,
    champion_id: int,
    *,
    source: str,
    random_choice: bool = False,
) -> None:
    try:
        with transaction.atomic():
            QuickDraftAction.objects.create(
                draft=draft,
                step_index=step.index,
                action=step.action,
                side=side,
                champion_id=champion_id,
                source=source,
                random_choice=random_choice,
                created_at=now(),
            )
    except IntegrityError as exc:
        raise StaleAction("Selection is already being processed.") from exc


def _advance(draft: QuickDraft) -> None:
    steps = hero_steps(_mode(draft))
    draft.step_index += 1
    if draft.step_index >= len(steps):
        draft.status = QuickDraftStatus.COMPLETED
        draft.deadline_at = None
        draft.ended_at = now()
    else:
        draft.deadline_at = seconds_from_now(steps[draft.step_index].seconds)
    draft.version += 1
    draft.save(update_fields=["step_index", "status", "deadline_at", "ended_at", "version"])
    request_render(draft)


@transaction.atomic
def choose_hero(draft_id: int, user_id: int, step_index: int, champion_id: int) -> None:
    draft = _lock(draft_id)
    step = _current(draft, step_index)
    side = _require_turn(draft, step, user_id)
    if champion_id not in available(draft):
        name = Champion.objects.filter(pk=champion_id).values_list("name", flat=True).first()
        done = "banned" if step.action == "ban" else "picked"
        raise InvalidAction(f"{name or 'That hero'} has already been {done}.")
    _record(draft, step, side, champion_id, source="user")
    _advance(draft)


@transaction.atomic
def choose_random(draft_id: int, user_id: int, step_index: int) -> int:
    """The 🎲 Random button: shown as "(random choice)"."""
    draft = _lock(draft_id)
    step = _current(draft, step_index)
    side = _require_turn(draft, step, user_id)
    pool = available(draft)
    if not pool:
        raise InvalidAction("No available heroes to pick from.")
    champion = auto_champion(pool, draft.rng_seed, step.index)
    _record(draft, step, side, champion, source="user", random_choice=True)
    _advance(draft)
    return champion


def on_timeout(draft: QuickDraft) -> None:
    """A turn ran out: a random hero, shown as "(randomed)"."""
    step = _current(draft, None)
    side = side_for_role(step.role, draft.first_side)
    pool = available(draft)
    if not pool:
        _advance(draft)
        return
    champion = auto_champion(pool, draft.rng_seed, step.index)
    _record(draft, step, side, champion, source="timeout")
    _advance(draft)


def fire_deadline(draft_id: int) -> bool:
    with transaction.atomic():
        draft = _lock(draft_id)
        if (
            draft.status != QuickDraftStatus.DRAFTING
            or draft.deadline_at is None
            or draft.deadline_at > now()
        ):
            return False
        on_timeout(draft)
        return True


def tick(moment: datetime) -> int:
    fired = close_unposted(moment)
    due = QuickDraft.objects.filter(status=QuickDraftStatus.DRAFTING, deadline_at__lte=moment)
    for draft_id in due.values_list("pk", flat=True):
        try:
            fired += 1 if fire_deadline(draft_id) else 0
        except DomainError as exc:
            logger.info("Draft deadline skipped", extra={"draft": draft_id, "why": exc.message})
        except Exception:
            logger.exception("Draft deadline failed", extra={"draft": draft_id})
    return fired


def close_unposted(moment: datetime) -> int:
    """Close live drafts that never got their message saved, so they don't block the channel."""
    cutoff = moment - timedelta(seconds=UNPOSTED_SECONDS)
    stuck = QuickDraft.objects.filter(
        status__in=QUICK_DRAFT_LIVE, message_id__isnull=True, created_at__lt=cutoff
    )
    closed = 0
    for draft_id in stuck.values_list("pk", flat=True):
        try:
            message_gone(draft_id)
        except Exception:
            logger.exception("Couldn't close an unposted draft", extra={"draft": draft_id})
            continue
        logger.warning("Closed a draft whose message was never saved", extra={"draft": draft_id})
        closed += 1
    return closed


def extend_past_due(new_deadline: datetime) -> int:
    return QuickDraft.objects.filter(
        status=QuickDraftStatus.DRAFTING, deadline_at__lt=new_deadline
    ).update(deadline_at=new_deadline)


# --- Stopping -------------------------------------------------------------------------------


@transaction.atomic
def stop(channel_id: int, user_id: int, *, is_staff: bool) -> QuickDraft:
    """/stopdraft: the captains, whoever started it, or staff. The message is deleted."""
    found = live_in_channel(channel_id)
    if found is None:
        raise InvalidAction("No draft is currently in progress in this channel!")
    draft = _lock(found.pk)
    allowed = {draft.started_by_id, draft.hk_captain_id, draft.am_captain_id}
    allowed |= {draft.coinflip_winner_id, draft.coinflip_loser_id}
    if not is_staff and user_id not in allowed:
        raise NotAllowed("Only the captains, whoever started it, or staff can stop this draft.")
    _close(draft)
    return draft


def _close(draft: QuickDraft) -> None:
    draft.status = QuickDraftStatus.STOPPED
    draft.deadline_at = None
    draft.ended_at = now()
    draft.version += 1
    draft.save(update_fields=["status", "deadline_at", "ended_at", "version"])
    message_ids = [draft.message_id] if draft.message_id else []
    message_ids += draft.grid_message_ids
    if message_ids:
        add_effect(
            draft.community,
            "delete_quick_draft",
            # draft_id keeps it in the draft's own outbox lane, after its renders.
            {"draft_id": draft.pk, "channel_id": draft.channel_id, "message_ids": message_ids},
        )


@transaction.atomic
def message_gone(draft_id: int) -> None:
    """The draft has no message on Discord (someone deleted it, or posting it failed): close it
    so the channel is free rather than blocked for ever."""
    draft = _lock(draft_id)
    if draft.status in QUICK_DRAFT_LIVE:
        draft.message_id = None
        _close(draft)


# --- Read model -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeroChoice:
    action: str
    side: str
    name: str
    emoji_id: int | None
    unicode_emoji: str
    random_choice: bool
    auto: bool


@dataclass(frozen=True, slots=True)
class HeroOption:
    id: int
    name: str
    emoji_id: int | None
    unicode_emoji: str
    taken: bool = False


@dataclass(slots=True)
class QuickDraftState:
    id: int
    status: str
    channel_id: int
    message_id: int | None
    hk_captain_id: int | None
    hk_captain_name: str
    am_captain_id: int | None
    am_captain_name: str
    first_side: str
    step_index: int
    deadline_at: datetime | None
    grid_message_ids: list[int]
    draft_name: str
    gif_url: str | None
    steps: list[tuple[str, str]] = field(default_factory=list)  # (action, side)
    actions: list[HeroChoice] = field(default_factory=list)
    available: list[HeroOption] = field(default_factory=list)
    # Every hero in the pool by name, taken ones marked: the grid keeps them all in place.
    heroes: list[HeroOption] = field(default_factory=list)
    coinflip_winner_id: int | None = None
    coinflip_winner_name: str = ""
    coinflip_loser_id: int | None = None
    coinflip_loser_name: str = ""
    winner_choice: str = ""
    loser_choice: str = ""
    options: tuple[str, ...] = ()

    @property
    def acting_side(self) -> str:
        if self.status != QuickDraftStatus.DRAFTING or self.step_index >= len(self.steps):
            return ""
        return self.steps[self.step_index][1]

    @property
    def step_action(self) -> str:
        if self.status != QuickDraftStatus.DRAFTING or self.step_index >= len(self.steps):
            return ""
        return self.steps[self.step_index][0]

    def captain(self, side: str) -> tuple[int | None, str]:
        if side == Side.HIDDEN_KING:
            return self.hk_captain_id, self.hk_captain_name
        return self.am_captain_id, self.am_captain_name


def state(draft_id: int) -> QuickDraftState:
    draft = QuickDraft.objects.select_related("mode_version").get(pk=draft_id)
    steps = hero_steps(_mode(draft))
    champions = {row.pk: row for row in Champion.objects.filter(pk__in=draft.champion_pool)}
    result = QuickDraftState(
        id=draft.pk,
        status=draft.status,
        channel_id=draft.channel_id,
        message_id=draft.message_id,
        hk_captain_id=draft.hk_captain_id,
        hk_captain_name=draft.hk_captain_name,
        am_captain_id=draft.am_captain_id,
        am_captain_name=draft.am_captain_name,
        first_side=draft.first_side,
        step_index=draft.step_index,
        deadline_at=draft.deadline_at,
        grid_message_ids=list(draft.grid_message_ids),
        draft_name=draft_name(draft.rng_seed),
        gif_url=pick_gif(draft.rng_seed, draft_gifs()),
        steps=[(step.action, side_for_role(step.role, draft.first_side)) for step in steps],
        coinflip_winner_id=draft.coinflip_winner_id,
        coinflip_winner_name=draft.coinflip_winner_name,
        coinflip_loser_id=draft.coinflip_loser_id,
        coinflip_loser_name=draft.coinflip_loser_name,
        winner_choice=draft.winner_choice,
        loser_choice=draft.loser_choice,
    )
    if draft.status == QuickDraftStatus.COINFLIP:
        result.options = coinflip_options(draft)
    used: set[int] = set()
    for action in draft.actions.order_by("step_index"):
        used.add(action.champion_id)
        champion = champions.get(action.champion_id)
        result.actions.append(
            HeroChoice(
                action=action.action,
                side=action.side,
                name=champion.name if champion else str(action.champion_id),
                emoji_id=champion.discord_emoji_id if champion else None,
                unicode_emoji=champion.unicode_emoji if champion else "",
                random_choice=action.random_choice,
                auto=action.source == "timeout",
            )
        )
    result.available = [
        HeroOption(
            pk, champions[pk].name, champions[pk].discord_emoji_id, champions[pk].unicode_emoji
        )
        for pk in draft.champion_pool
        if pk not in used and pk in champions
    ]
    result.heroes = sorted(
        (
            HeroOption(
                row.pk, row.name, row.discord_emoji_id, row.unicode_emoji, taken=row.pk in used
            )
            for row in champions.values()
        ),
        key=lambda hero: hero.name,
    )
    return result


# --- /randomdraft ---------------------------------------------------------------------------


@dataclass(slots=True)
class RandomDraft:
    bans: dict[str, list[HeroOption]]
    picks: dict[str, list[HeroOption]]


def random_draft(community: Community) -> RandomDraft:
    """Bebot's /randomdraft: every ban and pick in the mode's order, chosen at random."""
    mode = parse_mode(community.active_mode.body)
    pool = list(
        Champion.objects.filter(is_enabled=True)
        .exclude(pk__in=mode.champion_draft.excluded_champion_ids)
        .order_by("name")
    )
    rng = seeded_rng(secrets.randbits(62), "random-draft")
    result = RandomDraft(
        bans={Side.HIDDEN_KING: [], Side.ARCHMOTHER: []},
        picks={Side.HIDDEN_KING: [], Side.ARCHMOTHER: []},
    )
    for step in hero_steps(mode):
        if not pool:
            break
        champion = pool.pop(rng.randrange(len(pool)))
        side = side_for_role(step.role, Side.HIDDEN_KING)
        option = HeroOption(
            champion.pk, champion.name, champion.discord_emoji_id, champion.unicode_emoji
        )
        (result.bans if step.action == "ban" else result.picks)[side].append(option)
    return result
