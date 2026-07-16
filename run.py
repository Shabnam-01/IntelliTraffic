import os
import sys
import webbrowser
import threading
import time
import uvicorn
from backend.database import init_db

def open_browser():
    # Wait 1.5 seconds for uvicorn to bind to the port
    time.sleep(1.5)
    print("\n--- Launching Traffic Monitoring Dashboard in Browser ---")
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    # Ensure current directory is in system path so backend modules are resolvable
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    db_path = os.path.join(db_dir, "traffic.db")
    
    print("Initialising SQLite Traffic database...")
    init_db(db_path)
    
    # Spawn browser opener in background thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Start ASGI Web Server
    print("Starting FastAPI ASGI backend server...")
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)
