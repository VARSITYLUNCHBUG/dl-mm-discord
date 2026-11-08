from django.db import migrations

APPEND_ONLY_TABLES = (
    "rating_runs",
    "rating_events",
    "match_results",
    "match_transitions",
    "draft_actions",
    "draft_mode_versions",
)

CREATE_FUNCTION = """
CREATE FUNCTION league_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION league_append_only();"


def _create_trigger(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION league_append_only();"
    )


def _drop_trigger(table: str) -> str:
    return f"DROP TRIGGER {table}_append_only ON {table};"


class Migration(migrations.Migration):
    dependencies = [("league", "0001_initial")]

    operations = [
        migrations.RunSQL(CREATE_FUNCTION, DROP_FUNCTION),
        *[
            migrations.RunSQL(_create_trigger(table), _drop_trigger(table))
            for table in APPEND_ONLY_TABLES
        ],
    ]
