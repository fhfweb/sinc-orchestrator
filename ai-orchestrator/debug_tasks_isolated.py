import sys
import traceback
from fastapi import FastAPI, APIRouter
from fastapi.routing import APIRoute

sys.path.append('.')

def check_tasks_individually():
    try:
        from services.streaming.routes.tasks import router
        print(f"Total routes found: {len(router.routes)}")
        
        for i, route in enumerate(router.routes):
            if not isinstance(route, APIRoute):
                continue
                
            print(f"Testing route {i}: {route.path}")
            try:
                # Create a mini app and dummy router for this single route
                app = FastAPI()
                test_router = APIRouter()
                test_router.routes.append(route)
                app.include_router(test_router)
                print(f"  OK")
            except Exception as e:
                print(f"  FAILED: {e}")
                traceback.print_exc()
                
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    check_tasks_individually()
