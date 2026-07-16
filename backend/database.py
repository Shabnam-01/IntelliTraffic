import sqlite3
import os
from datetime import datetime

def init_db(db_path: str):
    """
    Initializes the SQLite database and creates the necessary tables.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create traffic logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS traffic_log (
            timestamp TEXT PRIMARY KEY,
            cars_count INTEGER,
            buses_count INTEGER,
            trucks_count INTEGER,
            bikes_count INTEGER,
            total_passed INTEGER,
            density_level TEXT
        )
    """)
    
    # Check if we need to add thumbnail column
    cursor.execute("PRAGMA table_info(alerts_log)")
    columns = [col[1] for col in cursor.fetchall()]
    if columns and "thumbnail" not in columns:
        cursor.execute("DROP TABLE alerts_log")
        
    # Create alerts logs table with thumbnail column
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            type TEXT,
            vehicle_id INTEGER,
            description TEXT,
            thumbnail TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    
    conn.commit()
    conn.close()

def log_traffic_metrics(db_path: str, cars: int, buses: int, trucks: int, bikes: int, total_passed: int, density_level: str):
    """
    Logs current vehicle counts and cumulative total to the traffic_log table.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    timestamp = datetime.now().isoformat()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO traffic_log (timestamp, cars_count, buses_count, trucks_count, bikes_count, total_passed, density_level)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, cars, buses, trucks, bikes, total_passed, density_level))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error in log_traffic_metrics: {e}")
    finally:
        conn.close()

def log_alert(db_path: str, alert_type: str, vehicle_id: int, description: str, thumbnail: str = None) -> int:
    """
    Logs a new traffic anomaly alert with optional Base64 thumbnail crop. Returns the ID of the inserted alert.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    timestamp = datetime.now().isoformat()
    alert_id = -1
    try:
        cursor.execute("""
            INSERT INTO alerts_log (timestamp, type, vehicle_id, description, thumbnail, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (timestamp, alert_type, vehicle_id, description, thumbnail))
        alert_id = cursor.lastrowid
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error in log_alert: {e}")
    finally:
        conn.close()
    return alert_id

def resolve_alert(db_path: str, alert_id: int):
    """
    Marks an alert as resolved (is_active = 0).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE alerts_log
            SET is_active = 0
            WHERE id = ?
        """, (alert_id,))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error in resolve_alert: {e}")
    finally:
        conn.close()

def get_latest_metrics(db_path: str):
    """
    Returns the most recent traffic metric log entry.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = None
    try:
        cursor.execute("""
            SELECT * FROM traffic_log
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
    except sqlite3.Error as e:
        print(f"Database error in get_latest_metrics: {e}")
    finally:
        conn.close()
    return dict(row) if row else None

def get_historical_metrics(db_path: str, limit: int = 100):
    """
    Returns historical logs up to a limit for trending visualization.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = []
    try:
        cursor.execute("""
            SELECT * FROM traffic_log
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        rows.reverse()  # chronological order
    except sqlite3.Error as e:
        print(f"Database error in get_historical_metrics: {e}")
    finally:
        conn.close()
    return [dict(r) for r in rows]

def get_active_alerts(db_path: str):
    """
    Returns all active (unresolved) traffic alerts.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = []
    try:
        cursor.execute("""
            SELECT * FROM alerts_log
            WHERE is_active = 1
            ORDER BY timestamp DESC
        """)
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Database error in get_active_alerts: {e}")
    finally:
        conn.close()
    return [dict(r) for r in rows]

def get_recent_alerts(db_path: str, limit: int = 20):
    """
    Returns recent alerts, whether active or resolved.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = []
    try:
        cursor.execute("""
            SELECT * FROM alerts_log
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Database error in get_recent_alerts: {e}")
    finally:
        conn.close()
    return [dict(r) for r in rows]

def clear_logs(db_path: str):
    """
    Clears all database logs (resets system).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM traffic_log")
        cursor.execute("DELETE FROM alerts_log")
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error in clear_logs: {e}")
    finally:
        conn.close()

def resolve_all_active_alerts(db_path: str):
    """
    Marks all currently active alerts as resolved (is_active = 0).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE alerts_log
            SET is_active = 0
            WHERE is_active = 1
        """)
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error in resolve_all_active_alerts: {e}")
    finally:
        conn.close()
