import pytest

from app.clients.fineract import FineractRejectedError, is_concurrent_modification


def conflict_body(code: str) -> dict[str, object]:
    message = "The server is currently unable to handle the request due to concurrent modification  on query [unknown], please try again"
    return {"httpStatusCode": "409", "userMessageGlobalisationCode": code, "defaultUserMessage": message, "errors": []}


@pytest.mark.parametrize(
    "code",
    ["error.msg.platform.service.query.conflict", "error.msg.platform.service.unknown.conflict", "error.msg.loan.locked"],
)
def test_optimistic_lock_and_cob_lock_conflicts_are_transient(code: str) -> None:
    error = FineractRejectedError(409, conflict_body(code))
    assert error.code == code
    assert is_concurrent_modification(error.status_code, error.code)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (409, "error.msg.glaccount.glcode.conflict"),
        (409, "error.msg.glaccount.external.reference.conflict"),
        (403, "error.msg.platform.service.query.conflict"),
        (400, "validation.msg.validation.errors.exist"),
        (409, None),
    ],
)
def test_business_rejections_stay_rejections(status_code: int, code: str | None) -> None:
    assert not is_concurrent_modification(status_code, code)
