from django.db import migrations


def forwards(apps, schema_editor):
    from budget.seed import seed
    seed(apps)


class Migration(migrations.Migration):
    dependencies = [("budget", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
