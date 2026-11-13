from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from league.domain.engine import Status as S


class Team(models.TextChoices):
    A = "A", "Team A"
    B = "B", "Team B"


class MatchStatus(models.TextChoices):
    READY_CHECK = S.READY_CHECK, "Ready check"
    CAPTAIN_PHASE = S.CAPTAIN_PHASE, "Captain vote"
    PLAYER_DRAFT = S.PLAYER_DRAFT, "Player draft"
    SIDE_SELECT = S.SIDE_SELECT, "Choosing sides"
    CHAMPION_DRAFT = S.CHAMPION_DRAFT, "Champion draft"
    LOBBY_CODE = S.LOBBY_CODE, "Lobby code"
    IN_PROGRESS = S.IN_PROGRESS, "In progress"
    AWAITING_CONFIRMATION = S.AWAITING_CONFIRMATION, "Awaiting confirmation"
    DISPUTED = S.DISPUTED, "Disputed"
    COMPLETED = S.COMPLETED, "Completed"
    VOIDED = S.VOIDED, "Voided"


class Source(models.TextChoices):
    USER = "user", "User"
    TIMEOUT = "timeout", "Timeout"
    STAFF = "staff", "Staff"
    SYSTEM = "system", "System"


class Match(models.Model):
    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    season = models.ForeignKey("league.Season", on_delete=models.PROTECT, related_name="matches")
    mode_version = models.ForeignKey(
        "league.DraftModeVersion", on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=24, choices=MatchStatus.choices)
    version = models.PositiveIntegerField(default=0)
    step_index = models.PositiveSmallIntegerField(default=0)
    deadline_at = models.DateTimeField(null=True, blank=True)
    rng_seed = models.BigIntegerField()
    champion_pool = ArrayField(models.PositiveIntegerField(), default=list)
    player_first_pick = models.CharField(max_length=1, choices=Team.choices, blank=True)
    champion_first_pick = models.CharField(max_length=1, choices=Team.choices, blank=True)
    hidden_king_team = models.CharField(max_length=1, choices=Team.choices, blank=True)
    lobby_code = models.CharField(max_length=32, blank=True)
    lobby_code_host = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    lobby_code_attempts = models.PositiveSmallIntegerField(default=0)
    # A lobby deadlock-api created for us. Its bot hosts it, and leaves 15 minutes after creating.
    api_party_id = models.CharField(max_length=32, blank=True)
    api_lobby_created_at = models.DateTimeField(null=True, blank=True)
    api_started_at = models.DateTimeField(null=True, blank=True)
    # Deadlock's own match ID, from the API lobby, a report or the scoreboard.
    deadlock_match_id = models.BigIntegerField(null=True, blank=True)
    winner = models.CharField(max_length=1, choices=Team.choices, blank=True)
    # While a sub slot is open the timer stops: this holds what was left, and deadline_at is null.
    held_seconds = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matches"
        ordering = ("-id",)
        indexes = [
            models.Index(
                fields=("deadline_at",),
                condition=Q(deadline_at__isnull=False),
                name="match_deadline_idx",
            ),
            models.Index(fields=("community", "status"), name="match_community_status_idx"),
            models.Index(fields=("season", "ended_at", "id"), name="match_season_timeline_idx"),
        ]

    def __str__(self) -> str:
        return f"Match #{self.pk} ({self.status})"


class RemovedReason(models.TextChoices):
    MISSED_READY = "missed_ready", "Missed ready check"
    LEFT = "left", "Left"
    SUBBED_OUT = "subbed_out", "Subbed out"


class MatchParticipant(models.Model):
    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="participants")
    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="matches")
    queue_joined_at = models.DateTimeField()
    ready_at = models.DateTimeField(null=True, blank=True)
    team = models.CharField(max_length=1, choices=Team.choices, blank=True)
    is_captain = models.BooleanField(default=False)
    draft_pick_number = models.PositiveSmallIntegerField(null=True, blank=True)
    # True from joining until the match ends or the player is removed. Backs the hard guarantee
    # below that nobody can be in two live matches.
    is_active = models.BooleanField(default=True)
    # Blank means the player is on the roster. Rated rosters are `team` set and this blank.
    removed_reason = models.CharField(max_length=16, choices=RemovedReason.choices, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    # Left the game while it was on: subbed out, voided as an early leaver, or abandoned per
    # deadlock-api.
    left_early = models.BooleanField(default=False)

    class Meta:
        db_table = "match_participants"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("match", "player"), name="uniq_match_player"),
            models.UniqueConstraint(
                fields=("player",),
                condition=Q(is_active=True),
                name="uniq_player_one_live_match",
            ),
        ]
        indexes = [models.Index(fields=("player", "-match"), name="participant_history_idx")]

    def __str__(self) -> str:
        return f"{self.player_id} in {self.match_id}"


class ResourceKind(models.TextChoices):
    TEXT = "text", "Lobby text channel"
    LOBBY_VOICE = "lobby_voice", "Lobby voice channel"
    TEAM_A_VOICE = "team_a_voice", "Team A voice channel"
    TEAM_B_VOICE = "team_b_voice", "Team B voice channel"
    BOARD_MESSAGE = "board_message", "Board message"
    PICKER_MESSAGE = "picker_message", "Champion grid message"
    PICKER_MESSAGE_2 = "picker_message_2", "Champion grid message, second half"
    RESULTS_MESSAGE = "results_message", "Results message"
    NOTICE = "notice", "Notice message"


class MatchResource(models.Model):
    """Every Discord object a match owns. Cleanup works only from these IDs, never name patterns."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="resources")
    kind = models.CharField(max_length=16, choices=ResourceKind.choices)
    discord_id = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "match_resources"
        constraints = [
            # A match has one of everything except notices, which come and go as it progresses.
            models.UniqueConstraint(
                fields=("match", "kind"),
                condition=~Q(kind="notice"),
                name="uniq_match_resource_kind",
            ),
        ]
        indexes = [
            models.Index(
                fields=("discord_id",),
                condition=Q(kind="text", deleted_at__isnull=True),
                name="resource_live_text_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.discord_id}"


class MatchTransition(models.Model):
    """Append-only status log: the audit trail and the source for time-in-state metrics."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="transitions")
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24)
    source = models.CharField(max_length=8, choices=Source.choices)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField()

    class Meta:
        db_table = "match_transitions"
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.from_status or 'new'} -> {self.to_status}"


class DraftActionKind(models.TextChoices):
    PLAYER_PICK = "player_pick", "Player pick"
    BAN = "ban", "Champion ban"
    PICK = "pick", "Champion pick"


class DraftAction(models.Model):
    """Append-only record of each accepted draft step. A timed-out step takes a random target."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="draft_actions")
    step_index = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=12, choices=DraftActionKind.choices)
    team = models.CharField(max_length=1, choices=Team.choices)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    target_player = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    target_champion = models.ForeignKey(
        "league.Champion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source = models.CharField(max_length=8, choices=Source.choices)
    # The captain pressed Random rather than choosing a champion.
    random_choice = models.BooleanField(default=False)
    interaction_id = models.BigIntegerField(null=True, blank=True, unique=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "draft_actions"
        ordering = ("match", "step_index")
        constraints = [
            models.UniqueConstraint(fields=("match", "step_index"), name="uniq_draft_step"),
            models.UniqueConstraint(
                fields=("match", "target_champion"),
                condition=Q(target_champion__isnull=False),
                name="uniq_draft_champion",
            ),
            models.UniqueConstraint(
                fields=("match", "target_player"),
                condition=Q(target_player__isnull=False),
                name="uniq_draft_player",
            ),
            models.CheckConstraint(
                condition=Q(target_player__isnull=True) | Q(target_champion__isnull=True),
                name="draft_at_most_one_target",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.match_id}#{self.step_index} {self.action}"


class CaptainVote(models.Model):
    """One player's vote for a captain. Each voter has up to two; voting again replaces them."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="captain_votes")
    voter = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    candidate = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField()

    class Meta:
        db_table = "captain_votes"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("match", "voter", "candidate"), name="uniq_captain_vote"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.voter_id} -> {self.candidate_id}"


class EarlyLeaveStatus(models.TextChoices):
    PENDING = "pending", "Waiting for the other captain"
    APPROVED = "approved", "Match voided"
    REJECTED = "rejected", "The other captain disagreed"
    EXPIRED = "expired", "No answer, sent to staff"


class EarlyLeave(models.Model):
    """A captain saying someone left in the first minutes of the game. Agreed, the match is voided
    with no rating change and the leaver gets a leaver penalty."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="early_leaves")
    leaver = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=8, choices=EarlyLeaveStatus.choices, default=EarlyLeaveStatus.PENDING
    )
    deadline_at = models.DateTimeField()
    resolved_by = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "early_leaves"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("match",),
                condition=Q(status="pending"),
                name="uniq_pending_early_leave",
            ),
        ]
        indexes = [
            models.Index(
                fields=("deadline_at",),
                condition=Q(status="pending"),
                name="early_leave_pending_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.match_id}: {self.leaver_id} {self.status}"
