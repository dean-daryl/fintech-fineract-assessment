---
name: idempotent-integration-ops
description: Use when building or testing any state-changing endpoint in the integration service or Mock ERP: idempotency table, operation state machine, retries and recovery sweep, concurrency tests, and SIMULATIONS.md evidence.
---

# Idempotent operations pattern

Correctness under duplicates, timeouts, partial success and concurrency is enforced by the database, never by in-memory state. Every state-changing endpoint uses the same pipeline.

## Operations table

One row per request, unique on `(operation_type, external_reference)`, with: normalised payload, payload hash, status, attempts, next_attempt_at, locked_until, last_error, fineract_txn_id, erp_txn_id, stored response. It is both the idempotency store and the retry queue.

## Pipeline

```python
def execute(op_type, ref, payload, steps):
    op, created = claim(op_type, ref, payload)   # INSERT; unique violation means it already exists
    if not created:
        return replay(op, payload)
    for step in steps:
        step(op)                                  # each step resumable from op.status
    return complete(op)
```

`replay` rules:
- finished and same hash: 200 with the stored response body
- in progress and same hash: 202 with current status, no second execution
- different hash: 409

## State machine

`RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`, plus `FAILED` and `NEEDS_RECONCILIATION`. Transitions live in one table in `domain/states.py`; invalid transitions raise. Persist the new state before calling the next system.

## Extra guards

- Disbursement: `SELECT ... FOR UPDATE` on the loan row and a unique constraint per loan, so different references still give one disbursement. Fineract's loan state is the last line of defence.
- Mock ERP: its own unique constraints on account code, account reference and journal entry reference; rejects unbalanced entries.
- Journal entry reference is derived deterministically from the transaction's external_reference.

## Retries and recovery (no broker)

- Inline: 2 to 3 quick attempts with short backoff.
- Deferred: set next_attempt_at with exponential backoff plus jitter, capped.
- `recover()` claims due rows with `FOR UPDATE SKIP LOCKED` plus a lease (`locked_until`), and resumes each from its saved status. Call it on startup, from an in-process timer, and from `POST /admin/recover`.
- Always look up by external reference before resending after a timeout.
- After max attempts: `FAILED` if nothing happened downstream, `NEEDS_RECONCILIATION` if Fineract succeeded and the ERP did not confirm.

## Required tests for each operation

- Same request 4 times: exactly one effect in each system; print the counts.
- 10+ parallel requests with the same reference, and (for disbursement) with different references, using a real thread pool against the running stack.
- Same reference with a different payload: 409.
- Each failure injection scenario: before, during and after state.

## Evidence

Every simulation is a re-runnable `make simulate-*` target and is written into SIMULATIONS.md using the brief's template: initial state, request, response, Fineract state, ERP state, integration state, expected, actual, explanation. Never claim a behaviour in the docs that the evidence does not show.

## Logging

JSON logs carrying correlation_id, external_reference, client_uid, loan_reference
