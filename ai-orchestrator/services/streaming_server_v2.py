from services.streaming.core.config import env_get
"""
SINC Streaming Server v3 — Entry Point
=======================================
Thin entry point. All logic lives in the `streaming/` package.

To run:
    python streaming_server_v2.py           # development (Uvicorn)
    uvicorn services.streaming_server_v2:app --host 0.0.0.0 --port 8000

Environment variables: see streaming/core/config.py
"""
import logging
import os
import sys

_NOISY_AUTH_PATH_PREFIXES = (
    "/dashboard/state",
    "/api/v5/dashboard/",
    "/system/infra",
    "/readiness/live",
    "/incidents",
    "/lessons",
    "/events",
)


class _ExpectedAuthNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = getattr(record, "args", ())
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method = str(args[1])
        path = str(args[2])
        try:
            status_code = int(args[4])
        except Exception:
            return True
        if status_code not in (401, 403):
            return True
        if path.startswith(_NOISY_AUTH_PATH_PREFIXES):
            return False
        if method == "GET" and path.startswith("/tasks?limit=25"):
            return False
        if method == "POST" and path in ("/queue/poll", "/tasks/claim"):
            return False
        return True

# ── Structured logging ────────────────────────────────────────────────────────
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logging.getLogger("orchestrator").addHandler(_handler)
logging.getLogger("orchestrator").setLevel(
    logging.DEBUG if env_get("LOG_LEVEL", default="").upper() == "DEBUG" else logging.INFO
)
logging.getLogger("uvicorn.access").addFilter(_ExpectedAuthNoiseFilter())

# ── App factory ───────────────────────────────────────────────────────────────
from services.streaming import create_app

app = create_app()

# ── Entry Point (development) ────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    from services.streaming.core.config import PORT
    logging.getLogger("orchestrator").info(
        f'"starting streaming_server_v2 (FastAPI) port={PORT}"'
    )
    uvicorn.run("services.streaming_server_v2:app", host="0.0.0.0", port=PORT, reload=True)
