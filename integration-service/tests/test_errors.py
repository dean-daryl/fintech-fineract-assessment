from app.auth import key_matches
from app.errors import error_body, error_code, validation_details


def test_error_codes_follow_the_status() -> None:
    assert [error_code(s) for s in (401, 404, 409, 422, 503)] == [
        "unauthorized",
        "not_found",
        "conflict",
        "unprocessable",
        "service_unavailable",
    ]
    assert (error_code(418), error_code(504)) == ("client_error", "server_error")


def test_error_body_keeps_context_and_lets_the_message_and_code_win() -> None:
    body = error_body(409, "already used", external_reference="REF-1", operation_status="FAILED")
    assert body == {"external_reference": "REF-1", "operation_status": "FAILED", "error": "already used", "code": "conflict"}
    assert error_body(422, "bad", code="invalid_request")["code"] == "invalid_request"


def test_validation_details_name_the_source_and_the_field() -> None:
    errors = [
        {"loc": ("body", "principal"), "msg": "Input should be greater than 0"},
        {"loc": ("path", "loan_reference"), "msg": "String should match pattern"},
        {"loc": ("body", "lines", 0, "amount"), "msg": "Field required"},
    ]
    assert validation_details(errors) == [
        {"in": "body", "field": "principal", "message": "Input should be greater than 0"},
        {"in": "path", "field": "loan_reference", "message": "String should match pattern"},
        {"in": "body", "field": "lines.0.amount", "message": "Field required"},
    ]


def test_key_matches_only_the_exact_key() -> None:
    assert key_matches("secret", "secret")
    assert not key_matches("secret ", "secret")
    assert not key_matches(None, "secret")
    assert not key_matches("", "")
