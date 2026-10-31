from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    erp_base_url: str
    fineract_base_url: str
    fineract_tenant: str = "default"
    fineract_username: str
    fineract_password: str
    partner_api_key: SecretStr
    admin_api_key: SecretStr
    fineract_tls_verify: bool = True
    fineract_timeout_seconds: float = 10.0
    erp_timeout_seconds: float = 10.0

    tenant_currency: str = "RWF"
    tenant_timezone: str = "Africa/Kigali"
    fineract_office_name: str = "Kigali Main Office"
    loan_product_short_name: str = "SAF"

    gl_sync_inline_attempts: int = 3
    gl_sync_max_attempts: int = 10
    gl_sync_lease_seconds: int = 60
    recovery_interval_seconds: int = 30
    fault_injection_enabled: bool = False

    operation_lease_seconds: int = 60
    operation_inline_attempts: int = 3
    operation_max_attempts: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
