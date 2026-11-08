from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("league", "0002_append_only_triggers")]

    operations = [
        migrations.RenameField(
            model_name="match",
            old_name="amber_team",
            new_name="hidden_king_team",
        ),
    ]
