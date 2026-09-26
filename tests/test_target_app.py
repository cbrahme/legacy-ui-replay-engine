import pytest
from fastapi.testclient import TestClient

from src.target_app.app import app


@pytest.fixture
def client():
    # Reset state before each test
    with TestClient(app) as test_client:
        test_client.post("/reset")
        yield test_client


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "First Federal CU" in data["app"]


def test_root_redirect_to_dashboard(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_dashboard_renders(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Branch Operator Console" in response.text
    assert "Member Servicing" in response.text


def test_member_search_active_12345(client):
    # Form submission for active member
    response = client.post(
        "/members/search",
        data={"member_id": "12345"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/members/12345"

    # Detail view renders member information and savings balance
    detail_resp = client.get("/members/12345")
    assert detail_resp.status_code == 200
    assert "Eleanor Vance" in detail_resp.text
    assert "$4,250.75" in detail_resp.text
    assert "SAV-8002" in detail_resp.text
    assert "Active &amp; Verified" in detail_resp.text or "Active & Verified" in detail_resp.text


def test_member_search_not_found_99999(client):
    # Legitimate business outcome: not found
    response = client.post(
        "/members/search",
        data={"member_id": "99999"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Member record not found in system (ID: 99999)" in response.text
    assert "not-found-alert" in response.text


def test_member_locked_account_67890_and_override(client):
    # Detail view indicates security lockout
    response = client.get("/members/67890")
    assert response.status_code == 200
    assert "Arthur Pendelton" in response.text
    assert "Security Lockout:" in response.text
    assert "account-locked-alert" in response.text

    # Operator supervisor override
    override_resp = client.post(
        "/members/67890/override",
        follow_redirects=True,
    )
    assert override_resp.status_code == 200
    assert "account-locked-alert" not in override_resp.text
    assert "Active &amp; Verified" in override_resp.text or "Active & Verified" in override_resp.text


def test_transfer_flow_with_confirmation(client):
    # Initial submission requires confirmation
    prep_resp = client.post(
        "/transfers",
        data={
            "from_account": "SAV-8002",
            "to_account": "EXT-9871",
            "amount": "250.00",
        },
    )
    assert prep_resp.status_code == 200
    assert "IRREVERSIBLE ACTION" in prep_resp.text
    assert "confirm-transfer-btn" in prep_resp.text

    # Irreversible confirmation commit
    confirm_resp = client.post(
        "/transfers/confirm",
        data={
            "from_account": "SAV-8002",
            "to_account": "EXT-9871",
            "amount": "250.00",
        },
    )
    assert confirm_resp.status_code == 200
    assert "Transfer of $250.00 from SAV-8002 to EXT-9871 completed" in confirm_resp.text
