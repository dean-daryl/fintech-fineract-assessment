import os

for name, value in {
    "DATABASE_URL": "postgresql://unused@localhost/unused",
    "ERP_BASE_URL": "http://unused",
    "FINERACT_BASE_URL": "http://unused",
    "FINERACT_USERNAME": "unused",
    "FINERACT_PASSWORD": "unused",
    "PARTNER_API_KEY": "test-partner-key",
    "ADMIN_API_KEY": "test-admin-key",
}.items():
    os.environ.setdefault(name, value)
