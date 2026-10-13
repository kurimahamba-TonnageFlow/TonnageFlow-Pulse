"""Report-specific access using existing sessions, never a second credential store.

A report is classified by its template, not by the screen that links to it.
Apply this guard to generation, history, retrieval, revision and every export.
"""

from dataclasses import dataclass
from typing import Literal
from fastapi import Header, HTTPException
from . import engineering_auth, hmi_auth, management_auth

ReportKind = Literal["linetech", "engineering", "management"]
REPORT_ACCESS = {
    "linetech": frozenset({"linetech"}),
    "engineering": frozenset({"linetech", "engineering"}),
    "management": frozenset({"linetech", "engineering", "management"}),
}


@dataclass(frozen=True)
class ReportActor:
    role: str
    name: str

    @property
    def report_types(self):
        return REPORT_ACCESS[self.role]

    def require(self, report_type: str):
        if report_type not in self.report_types:
            raise HTTPException(
                403, "Your credentials do not allow access to this report type."
            )


def require_report_actor(
    authorization: str | None = Header(default=None),
    x_hmi_session: str | None = Header(default=None),
) -> ReportActor:
    # An explicitly supplied bearer must validate; never downgrade a rejected
    # Management/Engineering token to any tablet session sent alongside it.
    if authorization is not None:
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise HTTPException(401, "Report sign-in required.")
        token = authorization[7:].strip()
        manager = management_auth.get_session(token)
        if manager:
            return ReportActor("management", manager["manager_name"])
        engineer = engineering_auth.get_session(token)
        if engineer:
            return ReportActor("engineering", engineer["engineer_name"])
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    tablet = hmi_auth.require_device(x_hmi_session)
    # Device identity is authenticated. Technician names remain reported
    # identities and must not be presented as the authenticated generator.
    return ReportActor("linetech", tablet["device_name"])
