import asyncio
from services.streaming.core.db import async_db

async def test():
    try:
        async with async_db(bypass_rls=True) as conn:
            print(f"Yielded type: {type(conn)}")
            print(f"Attributes: {dir(conn)}")
            if hasattr(conn, 'commit'):
                print("Has commit() method")
            else:
                print("Missing commit() method!")
    except Exception as e:
        print(f"Error during test: {e}")

if __name__ == "__main__":
    asyncio.run(test())
