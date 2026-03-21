import sys
import traceback
from fastapi import FastAPI
from pydantic import BaseModel

sys.path.append('.')

def check_tasks_routes():
    try:
        from services.streaming.routes.tasks import router
        print(f"Total routes found: {len(router.routes)}")
        for i, route in enumerate(router.routes):
            try:
                print(f"Testing route {i}: {route.path} [{route.methods}]")
                # Create a mini app just for this route to trigger validation
                app = FastAPI()
                app.include_router(router) 
                # Wait, if we include the whole router it fails. 
                # We need to find which route inside the router is the culprit.
            except Exception as e:
                print(f"  FAILED on route {i}: {route.path}")
                print(f"  Error: {e}")
                
        # Let's try to identify if any route has a response_model that is not a class
        for i, route in enumerate(router.routes):
            rm = getattr(route, "response_model", None)
            if rm:
                print(f"Route {route.path} has response_model: {rm} (type: {type(rm)})")
                if not isinstance(rm, type) and not str(type(rm)).startswith("<class 'typing."):
                     print(f"  WARNING: Potential invalid response_model at {route.path}")
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    check_tasks_routes()
