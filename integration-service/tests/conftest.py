import os

for name, value in {
    "DATABASE_URL": "postgresql://unused@localhost/unused",
    "ERP_BASE_URL": "http://unused",
    "FINERACT_BASE_URL": "http://unused",
    "FINERACT_USERNAME": "unused",
    "FINERACT_PASSWORD": "unused",
}.items():
    os.environ.setdefault(name, value)
