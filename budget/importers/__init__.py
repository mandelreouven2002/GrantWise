from . import labsuit_inventory, labsuit_orders, sap_budget, sap_grants

IMPORTERS = {
    "labsuit_orders": labsuit_orders.run,
    "labsuit_inventory": labsuit_inventory.run,
    "sap_grants": sap_grants.run,
    "sap_budget": sap_budget.run,
}


def run_import(source, f, file_name, as_of, mode, user="", **kw):
    return IMPORTERS[source](f, file_name=file_name, as_of=as_of, mode=mode, user=user, **kw)
