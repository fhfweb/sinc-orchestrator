import sys
import traceback
from fastapi import FastAPI
from fastapi.utils import create_response_field

sys.path.append('.')

def check_tasks_fields():
    try:
        from services.streaming.routes.tasks import router
        print(f"Inspecting {len(router.routes)} routes...")
        for i, route in enumerate(router.routes):
            if not hasattr(route, "endpoint"):
                continue
            
            rm = getattr(route, "response_model", None)
            if rm is not None:
                print(f"Route {route.path} has response_model={rm}")
                try:
                    create_response_field(name="response_field", type_=rm)
                    print(f"  Field validation OK")
                except Exception as e:
                    print(f"  Field validation FAILED: {e}")
                    
        # Now try to include it in a REAL app but catch the exact error message
        app = FastAPI()
        try:
            app.include_router(router)
        except Exception as e:
            print(f"CRITICAL: app.include_router(router) failed with: {type(e).__name__}: {e}")
            # If it's a FastAPIError, it might have more details in args
            print(f"Error args: {getattr(e, 'args', 'N/A')}")
            traceback.print_exc()

    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    check_tasks_fields()
