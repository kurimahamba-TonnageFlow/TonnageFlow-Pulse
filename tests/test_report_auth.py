from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from src import report_auth, hmi_auth, engineering_auth, management_auth


@pytest.mark.parametrize(
    "role,allowed",
    [
        ("linetech", {"linetech"}),
        ("engineering", {"linetech", "engineering"}),
        ("management", {"linetech", "engineering", "management"}),
    ],
)
def test_report_permissions(role, allowed):
    actor = report_auth.ReportActor(role, "Test actor")
    for kind in ["linetech", "engineering", "management"]:
        if kind in allowed:
            actor.require(kind)
        else:
            with pytest.raises(HTTPException) as error:
                actor.require(kind)
            assert error.value.status_code == 403


@pytest.mark.parametrize("role", ["engineering", "management"])
def test_reuses_existing_bearer_sessions(role):
    auth = engineering_auth if role == "engineering" else management_auth
    login = auth.create_session("Test user")
    try:
        actor = report_auth.require_report_actor("Bearer " + login["token"], None)
        assert actor == report_auth.ReportActor(role, "Test user")
    finally:
        auth.revoke_session(login["token"])


def test_existing_tablet_session_has_only_lintech_access(monkeypatch):
    monkeypatch.setitem(
        hmi_auth._sessions,
        "report-tablet",
        {
            "device_name": "Rovema tablet",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
    )
    actor = report_auth.require_report_actor(None, "report-tablet")
    assert actor.name == "Rovema tablet"
    assert actor.report_types == {"linetech"}


@pytest.mark.parametrize("header", ["Bearer invalid", "Basic invalid", "Bearer "])
def test_invalid_bearer_does_not_fall_back_to_tablet(header, monkeypatch):
    monkeypatch.setitem(
        hmi_auth._sessions,
        "report-tablet",
        {
            "device_name": "Tablet",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
    )
    with pytest.raises(HTTPException) as error:
        report_auth.require_report_actor(header, "report-tablet")
    assert error.value.status_code == 401


def test_expired_tablet_cannot_read_reports(monkeypatch):
    monkeypatch.setitem(
        hmi_auth._sessions,
        "expired-report-tablet",
        {
            "device_name": "Tablet",
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
    )
    with pytest.raises(HTTPException) as error:
        report_auth.require_report_actor(None, "expired-report-tablet")
    assert error.value.status_code == 401


def test_unsigned_request_is_rejected():
    with pytest.raises(HTTPException) as error:
        report_auth.require_report_actor(None, None)
    assert error.value.status_code == 401
