"""Django admin: edit champions, modes and wiring. History tables are read-only by design."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.http import HttpRequest

from league.domain.mode_schema import ModeValidationError
from league.models import (
    CaptainHandoff,
    CaptainVote,
    Champion,
    Community,
    DraftAction,
    DraftModeVersion,
    Match,
    MatchParticipant,
    MatchResource,
    MatchResult,
    MatchTransition,
    OutboxEffect,
    Penalty,
    Player,
    PlayerRating,
    QueueEntry,
    RatingEvent,
    RatingRun,
    ResultSubmission,
    Season,
)
from league.services import modes


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Champion)
class ChampionAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "is_enabled", "updated_at")
    list_editable = ("is_enabled",)
    list_filter = ("is_enabled",)
    search_fields = ("name", "aliases")
    prepopulated_fields = {"slug": ("name",)}  # noqa: RUF012

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False  # Past drafts refer to these rows. Disable a champion instead.


class DraftModeForm(forms.ModelForm):
    class Meta:
        model = DraftModeVersion
        fields = ("body",)

    def clean_body(self) -> Any:
        body = self.cleaned_data["body"]
        try:
            modes.validate_body(body)
        except ModeValidationError as exc:
            raise ValidationError(exc.problems) from exc
        return body


@admin.register(DraftModeVersion)
class DraftModeVersionAdmin(admin.ModelAdmin):
    """Saving always publishes a new version. Past versions stay exactly as matches saw them."""

    form = DraftModeForm
    list_display = ("slug", "version", "name", "created_at", "created_by")
    list_filter = ("slug",)
    readonly_fields = ("slug", "version", "name", "content_hash", "created_at", "created_by")
    fields = ("slug", "version", "name", "body", "content_hash", "created_at", "created_by")

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def save_model(
        self, request: HttpRequest, obj: DraftModeVersion, form: Any, change: bool
    ) -> None:
        published = modes.publish(form.cleaned_data["body"], created_by=request.user)
        # Point the admin's success message at the new row; the old row is left untouched.
        obj.pk = published.pk
        obj.slug = published.slug
        obj.version = published.version
        obj._state.adding = False


@admin.register(Community)
class CommunityAdmin(admin.ModelAdmin):
    list_display = ("name", "discord_guild_id", "active_mode", "max_concurrent_matches")

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "community",
        "number",
        "k_factor",
        "start_rating",
        "started_at",
        "ended_at",
    )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "discord_user_id", "steam_account_id", "created_at")
    search_fields = ("display_name", "discord_user_id")
    readonly_fields = ("discord_user_id",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # Players are created by the bot on first interaction.

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Penalty)
class PenaltyAdmin(ReadOnlyAdmin):
    list_display = ("player", "kind", "tier", "starts_at", "expires_at", "revoked_at")
    list_filter = ("kind",)
    search_fields = ("player__display_name",)


class ParticipantInline(ReadOnlyInline):
    model = MatchParticipant
    fields = ("player", "team", "is_captain", "draft_pick_number", "ready_at", "removed_reason")


class DraftActionInline(ReadOnlyInline):
    model = DraftAction
    fields = ("step_index", "action", "team", "target_player", "target_champion", "source")


class TransitionInline(ReadOnlyInline):
    model = MatchTransition
    fields = ("from_status", "to_status", "source", "actor", "created_at")


class SubmissionInline(ReadOnlyInline):
    model = ResultSubmission
    fields = (
        "captain",
        "verdict",
        "claimed_winner",
        "deadlock_match_id",
        "archive_message_id",
        "created_at",
    )


class ResultInline(ReadOnlyInline):
    model = MatchResult
    fields = ("revision", "outcome", "source", "actor", "reason", "created_at")


@admin.register(Match)
class MatchAdmin(ReadOnlyAdmin):
    list_display = ("id", "status", "winner", "mode_version", "created_at", "ended_at")
    list_filter = ("status", "season")
    inlines = (
        ParticipantInline,
        DraftActionInline,
        SubmissionInline,
        ResultInline,
        TransitionInline,
    )


@admin.register(PlayerRating)
class PlayerRatingAdmin(ReadOnlyAdmin):
    list_display = ("player", "season", "rating", "games", "wins", "losses", "last_played_at")
    list_filter = ("season",)
    search_fields = ("player__display_name",)
    ordering = ("-rating",)


@admin.register(RatingRun)
class RatingRunAdmin(ReadOnlyAdmin):
    list_display = ("id", "kind", "match", "season", "algo_version", "reason", "created_at")
    list_filter = ("kind",)


@admin.register(RatingEvent)
class RatingEventAdmin(ReadOnlyAdmin):
    list_display = ("id", "player", "match", "delta", "rating_after", "expected", "created_at")
    search_fields = ("player__display_name",)


@admin.register(QueueEntry)
class QueueEntryAdmin(ReadOnlyAdmin):
    list_display = ("player", "community", "joined_at")


@admin.register(CaptainHandoff)
class CaptainHandoffAdmin(ReadOnlyAdmin):
    list_display = ("match", "team", "from_player", "to_player", "status", "created_at")


@admin.register(CaptainVote)
class CaptainVoteAdmin(ReadOnlyAdmin):
    list_display = ("match", "voter", "candidate", "created_at")


@admin.register(MatchResource)
class MatchResourceAdmin(ReadOnlyAdmin):
    list_display = ("match", "kind", "discord_id", "deleted_at")


@admin.register(OutboxEffect)
class OutboxEffectAdmin(ReadOnlyAdmin):
    list_display = ("id", "kind", "match", "status", "attempts", "run_after", "last_error")
    list_filter = ("status", "kind")
