from django.db import migrations

CURRENCIES = [
    ("USD", "דולר אמריקאי", 10), ("EUR", "יורו", 20), ("GBP", "לירה שטרלינג", 30),
    ("CHF", "פרנק שוויצרי", 40), ("CAD", "דולר קנדי", 50), ("AUD", "דולר אוסטרלי", 60),
    ("JPY", "ין יפני", 70),
]


def forwards(apps, schema_editor):
    T = apps.get_model("budget", "TrackedCurrency")
    for code, name, sort in CURRENCIES:
        T.objects.get_or_create(code=code, defaults={"name_he": name, "sort": sort})


class Migration(migrations.Migration):
    dependencies = [("budget", "0003_tracked_currencies")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
