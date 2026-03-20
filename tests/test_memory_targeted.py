import asyncio
import logging
import time
import traceback
from uuid import uuid4
from services.memory_evolution import generate_and_store_lesson

logging.basicConfig(level=logging.INFO)

async def test_memory():
    state = {
        "task": {"id": str(uuid4())},
        "tenant_id": "stress_test_v2",
        "project_id": "stress",
        "description": "Targeted test",
        "task_type": "logic",
        "start_time": time.time(),
        "planner_name": "test_planner",
        "confidence": 0.95
    }
    try:
        print("Starting generate_and_store_lesson...")
        res = await generate_and_store_lesson(state, "test_solution", True, verified=True)
        print(f"Success! Lesson: {res}")
    except Exception as e:
        print("FAILED with exception:")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_memory())
