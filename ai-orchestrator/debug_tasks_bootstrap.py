import sys
import traceback
from fastapi import FastAPI
from importlib import import_module

sys.path.append('.')

def check_tasks():
    app = FastAPI()
    try:
        print("Importing tasks...")
        m = import_module('services.streaming.routes.tasks')
        print("Including router...")
        app.include_router(m.router)
        print("Success")
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    check_tasks()
