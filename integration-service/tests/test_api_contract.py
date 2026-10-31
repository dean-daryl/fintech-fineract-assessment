"""The HTTP contract: authentication, one error shape and correlation ids. No lifespan, so no database or downstream calls."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.faults import FaultInjector
from app.main import app

PARTNER = {"X-API-Key": "test-partner-key"}
ADMIN = {"X-API-Key": "test-admin-key"}


@app.get("/test-only/boom")
def boom() -> None:
    raise RuntimeError("unexpected")


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.state.faults = FaultInjector(enabled=False)
    yield TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/clients"), ("GET", "/api/loans/LN-2026-000001"), ("POST", "/api/reconciliation"), ("GET", "/api/gl-accounts")],
)
def test_partner_routes_need_the_partner_key(client: TestClient, method: str, path: str) -> None:
    for headers in ({}, {"X-API-Key": "wrong"}, ADMIN):
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 401
        assert response.json() == {"error": "missing or invalid X-API-Key", "code": "unauthorized"}
        assert response.headers["WWW-Authenticate"] == "X-API-Key"


def test_admin_routes_need_the_admin_key(client: TestClient) -> None:
    assert client.post("/admin/recover", headers=PARTNER).status_code == 401
    assert client.get("/admin/faults").status_code == 401


def test_health_and_docs_are_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert set(schemes) == {"PartnerApiKey", "AdminApiKey"}


def test_body_validation_errors_use_the_error_shape(client: TestClient) -> None:
    response = client.post("/api/loans", headers=PARTNER, json={"external_reference": "REF-1", "principal": 0})
    assert response.status_code == 422
    body = response.json()
    assert (body["error"], body["code"]) == ("the request is not valid", "invalid_request")
    fields = {detail["field"] for detail in body["details"]}
    assert {"principal", "client_uid", "client_name"} <= fields


def test_path_validation_errors_name_the_path(client: TestClient) -> None:
    body = client.get("/api/loans/not-a-reference", headers=PARTNER).json()
    assert body["code"] == "invalid_request"
    assert body["details"][0]["in"] == "path" and body["details"][0]["field"] == "loan_reference"


def test_http_errors_use_the_error_shape(client: TestClient) -> None:
    response = client.get("/admin/faults", headers=ADMIN)
    assert response.status_code == 404
    assert response.json() == {"error": "fault injection is disabled", "code": "not_found"}
    assert client.get("/api/nowhere", headers=PARTNER).json() == {"error": "Not Found", "code": "not_found"}


def test_unhandled_errors_use_the_error_shape_and_keep_the_correlation_id(client: TestClient) -> None:
    response = client.get("/test-only/boom", headers={"X-Correlation-ID": "corr-500"})
    assert response.status_code == 500
    assert response.json() == {"error": "internal error", "code": "internal_error"}
    assert response.headers["X-Correlation-ID"] == "corr-500"


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    assert client.get("/health", headers={"X-Correlation-ID": "corr-1"}).headers["X-Correlation-ID"] == "corr-1"
    assert client.post("/api/clients", json={}).headers["X-Correlation-ID"]
