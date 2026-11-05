from django.db import migrations


def initialize_state(apps, schema_editor):
    apps.get_model("tseh", "DomainState").objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("tseh", "0001_initial")]
    operations = [
        migrations.RunSQL(
            """ALTER TABLE tseh_ingredient
               ADD CONSTRAINT ingredient_owns_current_price
               FOREIGN KEY (id, current_price_id) REFERENCES tseh_price (ingredient_id, id)
               DEFERRABLE INITIALLY DEFERRED""",
            "ALTER TABLE tseh_ingredient DROP CONSTRAINT ingredient_owns_current_price",
        ),
        migrations.RunPython(initialize_state, migrations.RunPython.noop),
    ]
