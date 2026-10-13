from django.db import migrations

# The 14 currencies the Bank of Israel publishes a representative rate for (PublicApi/GetExchangeRates).
BOI_CURRENCIES = [
    ("USD", "דולר אמריקאי", 10), ("EUR", "יורו", 20), ("GBP", "לירה שטרלינג", 30), ("CHF", "פרנק שוויצרי", 40),
    ("CAD", "דולר קנדי", 50), ("AUD", "דולר אוסטרלי", 60), ("JPY", "ין יפני", 70), ("SEK", "כתר שוודי", 80),
    ("NOK", "כתר נורווגי", 90), ("DKK", "כתר דני", 100), ("ZAR", "ראנד דרום אפריקאי", 110),
    ("JOD", "דינר ירדני", 120), ("EGP", "לירה מצרית", 130), ("LBP", "לירה לבנונית", 140),
]


def forwards(apps, schema_editor):
    T = apps.get_model("budget", "TrackedCurrency")
    for code, name, sort in BOI_CURRENCIES:
        T.objects.update_or_create(code=code, defaults={"name_he": name, "sort": sort})


class Migration(migrations.Migration):
    dependencies = [("budget", "0005_allocation_status_label")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
