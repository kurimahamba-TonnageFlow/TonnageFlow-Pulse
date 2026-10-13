# ==========================================================
# TONNAGEFLOW PULSE
# Main HTTP API (HMI + WhatsApp)
# ==========================================================
#
# This is the process to run with uvicorn. It:
#   - Adds the versioned Production Run API (src/runs_api.py)
#   - Restricts CORS to the exact Pulse HMI origin (no wildcard)
#   - Mounts the existing, unmodified WhatsApp webhook app at "/" so
#     every route in src/whatsapp_webhook.py keeps working exactly as
#     it does today, including for its own existing test suite, which
#     imports that app directly and never touches this module.
#
# src/main.py (the CLI engine) is never imported here.

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

try:
    from .dashboard_api import router as dashboard_router
    from .engineering_api import router as engineering_router
    from .hmi_config_api import router as hmi_config_router
    from .management_api import router as management_router
    from .standards_api import router as standards_router
    from .pulse_capture_api import router as pulse_capture_router
    from .readiness_api import router as readiness_router
    from .runs_api import router as runs_router
    from .whatsapp_webhook import app as whatsapp_app
    from .api_logging import logger
except ImportError:
    from dashboard_api import router as dashboard_router
    from engineering_api import router as engineering_router
    from hmi_config_api import router as hmi_config_router
    from management_api import router as management_router
    from standards_api import router as standards_router
    from pulse_capture_api import router as pulse_capture_router
    from readiness_api import router as readiness_router
    from runs_api import router as runs_router
    from whatsapp_webhook import app as whatsapp_app
    from api_logging import logger

load_dotenv()


# ==========================================================
# CORS
# ==========================================================
# Exactly two allowed origins - the deployed Pulse HMI and the deployed
# Pulse Dashboard. Never widen this to "*"; override HMI_ORIGIN /
# DASHBOARD_ORIGIN in the environment if either one ever moves.

HMI_ORIGIN = os.getenv(
    "HMI_ORIGIN",
    "https://tonnage-flow-pulse-hmi.kurirai-mahamba.chatgpt.site",
)

DASHBOARD_ORIGIN = os.getenv(
    "DASHBOARD_ORIGIN",
    "https://tonnage-flow-pulse-dashboard.kurirai-mahamba.chatgpt.site",
)


# A missing variable silently falls back to the defaults above; say so
# at start-up, since a browser on any other origin is then refused.
for _name in ("HMI_ORIGIN", "DASHBOARD_ORIGIN"):
    if not os.getenv(_name):
        logger.warning("CONFIG %s is not set; CORS falls back to its built-in default origin.", _name)

app = FastAPI(title="TonnageFlow Pulse API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[HMI_ORIGIN, DASHBOARD_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    # Idempotency-Key: every HMI write sends one (src/pulse_capture_api.py).
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "X-HMI-Session"],
)


# ==========================================================
# ROUTES
# ==========================================================

app.include_router(dashboard_router)
from .hmi_auth import router as hmi_access_router, require_hmi_access
app.include_router(hmi_access_router)
from .task_capture_api import router as task_router
app.include_router(task_router)
app.include_router(runs_router, dependencies=[Depends(require_hmi_access)])
app.include_router(pulse_capture_router, dependencies=[Depends(require_hmi_access)])
app.include_router(management_router)
app.include_router(standards_router)
from .linetech_api import router as linetech_router
app.include_router(linetech_router)
app.include_router(engineering_router)
app.include_router(hmi_config_router, dependencies=[Depends(require_hmi_access)])
# /health/ready (database + schema). /health itself stays the static
# liveness check in the WhatsApp app below.
app.include_router(readiness_router)

# Mounted last, at root, so the explicit routes above always take
# priority; every other path (including /health and /webhooks/whatsapp)
# falls through to the existing WhatsApp app unchanged.
from .live_dashboard_api import router as live_dashboard_router
app.include_router(live_dashboard_router)
from .intelligence_api import router as intelligence_router
app.include_router(intelligence_router)
from .reports_api import router as reports_router
app.include_router(reports_router)
app.mount("/", whatsapp_app)
