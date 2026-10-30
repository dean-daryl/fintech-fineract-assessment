from app.services.clients import ClientService
from app.services.gl_sync import GLSyncService


def run_recovery(gl_sync: GLSyncService, clients: ClientService) -> dict[str, int]:
    return {
        "gl_accounts_processed": gl_sync.recover(),
        "client_operations_processed": clients.recover(),
    }
