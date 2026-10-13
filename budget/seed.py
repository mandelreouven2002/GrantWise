"""Default taxonomy: budget categories and the mappings found in the real exports.

Verified against the files: TRDF MATERIAL_BAL = SAP 600100 (חומרים) + 600150 (חיות מעבדה),
e.g. grant 2035816: 73,242.40 + (-10,833.03) = 62,409.
"""

CATEGORIES = [
    ("SALARY", "שכר", 10),
    ("MATERIAL", "חומרים", 20),
    ("EQUIPMENT", "ציוד", 30),
    ("SERVICE", "שירותים / סיוע טכני", 40),
    ("TRAVEL_EXT", "נסיעות לחו״ל", 50),
    ("TRAVEL_INT", "נסיעות בארץ", 60),
    ("BOOKS", "ספרים", 70),
    ("CONFERENCE", "אירוח וכנסים", 80),
    ("OFFICE", "משרדיות", 90),
    ("PUBLICATION", "פרסומים", 100),
    ("REPORTING", "דיווח", 110),
    ("OVERHEAD", "תקורה", 120),
    ("SELF", "עצמי", 130),
    ("OTHER", "אחר", 140),
    ("GENERAL", "כללי (מאגר)", 150),
]

TRDF_COLUMNS = {
    "SALARY_BAL": "SALARY", "MATERIAL_BAL": "MATERIAL", "EQUIPMENT_BAL": "EQUIPMENT",
    "SERVICE_BAL": "SERVICE", "TRAVEL_EXT_BAL": "TRAVEL_EXT", "TRAVEL_INT_BAL": "TRAVEL_INT",
    "BOOKS_BAL": "BOOKS", "HOST& CONFERENCE_BAL": "CONFERENCE", "OFFICE_BAL": "OFFICE",
    "PUBLICATION_BAL": "PUBLICATION", "REPORTING_BAL": "REPORTING", "OVERHEAD_BAL": "OVERHEAD",
    "SELF_BAL": "SELF", "OTHER_BAL": "OTHER", "GENERAL_BAL": "GENERAL",
}

SAP_COST_ELEMENTS = {
    "500100": "SALARY",       # שכר (מ)
    "600100": "MATERIAL",     # חומרים כלליים
    "600150": "MATERIAL",     # חיות מעבדה (TRDF מאחד עם חומרים)
    "600400": "SERVICE",      # סיוע טכני
    "700102": "TRAVEL_EXT",   # נסיעות לחו"ל
    "700202": "OFFICE",       # משרדיות
    "700300": "OTHER",        # רישום ותחזוקה
    "700510": "OTHER",        # השתתפות
    "700511": "OTHER",        # השתתפות
    "700901": "OTHER",        # הפרשי שער
    "IDC": "OVERHEAD",
    "RESAUTHOVERHEAD": "OVERHEAD",
    "UNITOVERHEAD": "OVERHEAD",
}

LABSUIT_ITEM_TYPES = {
    **{t: "MATERIAL" for t in [
        "Consumable", "Consumables", "Chemical", "Chemical-Poweders", "Antibody", "Tissue-culture",
        "Kits", "General Supply", "Oligo", "Protein", "ELISA", "Drugs", "Virtual", "Plasmid",
        "Bacteria", "Cell Line", "M2", "Plex", "CUSTOM_ENTITY"]},
    "Equipment": "EQUIPMENT",
    "Instrument": "EQUIPMENT",
    "Service": "SERVICE",
    "מזון ומשקאות": "OTHER",
}


def seed(apps=None):
    if apps is None:
        from django.apps import apps as _apps
        apps = _apps
    Cat = apps.get_model("budget", "BudgetCategory")
    Map = apps.get_model("budget", "CategoryMap")
    for code, name, sort in CATEGORIES:
        Cat.objects.update_or_create(code=code, defaults={"name_he": name, "sort": sort})
    for source, table in [("trdf_column", TRDF_COLUMNS), ("sap_cost_element", SAP_COST_ELEMENTS),
                          ("labsuit_item_type", LABSUIT_ITEM_TYPES)]:
        for value, cat in table.items():
            Map.objects.update_or_create(source=source, value=value, defaults={"category_id": cat})
