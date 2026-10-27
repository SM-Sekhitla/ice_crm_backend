ADMIN = "Super Admin"
BDO = "BDO"
PERMISSIONS = {
    ADMIN: {
        "contacts.view",
        "contacts.create",
        "contacts.edit",
        "contacts.delete",
        "leads.manage",
        "products.manage",
        "campaigns.view",
        "campaigns.manage",
        "campaigns.send",
        "users.manage",
        "email_config.manage",
    },
    BDO: {
        "contacts.view",
        "contacts.create",
        "contacts.edit",
        "leads.manage",
        "campaigns.view",
        "campaigns.manage",
        "campaigns.send",
    },
}


def has_permission(role: str, permission: str):
    return permission in PERMISSIONS.get(role, set())
