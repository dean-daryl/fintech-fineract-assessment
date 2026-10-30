from collections.abc import Sequence

from app.services.gl_sync import GLSyncService
from app.services.pipeline import OperationHandler, OperationPipeline


def run_recovery(gl_sync: GLSyncService, pipeline: OperationPipeline, handlers: Sequence[OperationHandler]) -> dict[str, int]:
    return {
        "gl_accounts_processed": gl_sync.recover(),
        "operations_processed": pipeline.recover(handlers),
    }
