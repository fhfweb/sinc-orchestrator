
import os
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

# Load env from Laravel .env
laravel_env = Path("g:/Fernando/project0/workspace/projects/sistema-gestao-psicologos-autonomos/.env")
load_dotenv(laravel_env)

def inspect_db():
    try:
        conn = psycopg2.connect(
            host="localhost", # Since we are running from host
            port=5432,
            database=os.getenv("DB_DATABASE"),
            user=os.getenv("DB_USERNAME"),
            password=os.getenv("DB_PASSWORD")
        )
        print(f"Connected to {os.getenv('DB_DATABASE')} successfully.")
        
        with conn.cursor() as cur:
            # Check tables
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            tables = cur.fetchall()
            print("Tables in public schema:", [t[0] for t in tables])
            
            # Check medical_records columns
            if ('medical_records',) in tables:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'medical_records'")
                cols = cur.fetchall()
                print("\nColumns in medical_records:")
                for c in cols:
                    print(f"  {c[0]} ({c[1]})")
            else:
                print("\nTable 'medical_records' NOT FOUND in PostgreSQL.")

            # Check patients columns
            if ('patients',) in tables:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'patients'")
                cols = cur.fetchall()
                print("\nColumns in patients:")
                for c in cols:
                    print(f"  {c[0]} ({c[1]})")
            else:
                print("\nTable 'patients' NOT FOUND in PostgreSQL.")
                
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspect_db()
