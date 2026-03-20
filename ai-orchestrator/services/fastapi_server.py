from services.streaming.core.config import env_get
import os
import uvicorn
from streaming import create_app

app = create_app()

if __name__ == "__main__":
    port = int(env_get("ORCH_API_PORT", default="8888"))
    uvicorn.run(app, host="0.0.0.0", port=port)
