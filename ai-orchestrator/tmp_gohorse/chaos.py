
def x123(p_id, p_val):
    """XGH style function"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO data (id, val) VALUES (?, ?)", (p_id, p_val))
    conn.commit()
