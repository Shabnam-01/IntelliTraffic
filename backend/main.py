import os
import asyncio
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Tuple

from .database import (
    init_db,
    get_latest_metrics,
    get_historical_metrics,
    get_recent_alerts,
    clear_logs
)
from .pipeline import TrafficPipeline

# Setup paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "traffic.db")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# Initialize DB
init_db(DB_PATH)

# Initialize FastAPI App
app = FastAPI(title="Smart Traffic Monitoring System API")

# Enable CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global pipeline instance
# We start with empty string which triggers downloading the default demo video
pipeline = TrafficPipeline(db_path=DB_PATH, model_name="yolov8n.pt", video_source="")

@app.on_event("startup")
async def startup_event():
    # Start the processing pipeline
    pipeline.start()

@app.on_event("shutdown")
async def shutdown_event():
    # Stop processing pipeline
    pipeline.stop()

# Config Models
class Point(BaseModel):
    x: int
    y: int

class ConfigData(BaseModel):
    line_p1: Point
    line_p2: Point
    roi_points: List[Point]
    video_source: str = ""
    weather_mode: str = "sunny"
    heatmap_enabled: bool = False

# Endpoints
@app.get("/")
async def get_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Frontend index.html not found.")

def frame_generator():
    """
    Generator for MJPEG stream.
    """
    while True:
        frame_bytes = pipeline.get_latest_frame()
        if frame_bytes is None:
            # Short sleep if frame is not ready
            time.sleep(0.03)
            continue
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        # Rate limit stream to ~30 FPS
        time.sleep(0.03)

@app.get("/api/stream")
async def get_video_stream():
    """
    Serves the live annotated video stream.
    """
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/api/stats/realtime")
async def get_realtime_stats():
    """
    Retrieves the most recent counts and system status.
    """
    latest = get_latest_metrics(DB_PATH)
    if not latest:
        # Defaults if DB is empty
        latest = {
            "timestamp": None,
            "cars_count": 0,
            "buses_count": 0,
            "trucks_count": 0,
            "bikes_count": 0,
            "total_passed": 0,
            "density_level": "LOW"
        }
    
    # Get active alerts (unresolved)
    from .database import get_active_alerts
    active_alerts = get_active_alerts(DB_PATH)
    
    return {
        "status": "running" if pipeline.is_running else "stopped",
        "current_metrics": latest,
        "counts": pipeline.counts,
        "total_passed": pipeline.total_passed,
        "active_alerts": active_alerts,
        "resolution": {"width": pipeline.frame_width, "height": pipeline.frame_height}
    }

@app.get("/api/stats/historical")
async def get_historical_stats(limit: int = 50):
    """
    Retrieves historical entries for plotting.
    """
    return get_historical_metrics(DB_PATH, limit=limit)

@app.get("/api/alerts")
async def get_alerts(limit: int = 20):
    """
    Retrieves the most recent alerts (active or resolved).
    """
    return get_recent_alerts(DB_PATH, limit=limit)

@app.get("/api/config")
async def get_config():
    """
    Retrieves the current configurations.
    """
    # Convert numpy array to point list
    roi_pts = []
    if pipeline.roi_polygon is not None:
        roi_pts = [{"x": int(pt[0]), "y": int(pt[1])} for pt in pipeline.roi_polygon]
        
    line_p1 = {"x": 0, "y": 0}
    line_p2 = {"x": 0, "y": 0}
    if pipeline.counting_line is not None:
        line_p1 = {"x": pipeline.counting_line[0][0], "y": pipeline.counting_line[0][1]}
        line_p2 = {"x": pipeline.counting_line[1][0], "y": pipeline.counting_line[1][1]}
        
    return {
        "line_p1": line_p1,
        "line_p2": line_p2,
        "roi_points": roi_pts,
        "video_source": pipeline.video_source,
        "weather_mode": pipeline.weather_mode,
        "heatmap_enabled": pipeline.heatmap_enabled
    }

@app.post("/api/config")
async def update_config(config: ConfigData):
    """
    Updates system config parameters. Restarts the pipeline if the video source changes.
    """
    line_p1 = (config.line_p1.x, config.line_p1.y)
    line_p2 = (config.line_p2.x, config.line_p2.y)
    roi_pts = [(pt.x, pt.y) for pt in config.roi_points]
    
    pipeline.set_config((line_p1, line_p2), roi_pts)
    pipeline.weather_mode = config.weather_mode
    pipeline.heatmap_enabled = config.heatmap_enabled
    
    # Check if video source changed
    if config.video_source != pipeline.video_source:
        pipeline.stop()
        pipeline.video_source = config.video_source
        pipeline.start()
        
    return {"status": "success", "message": "Configuration updated successfully."}

@app.get("/api/incidents/{alert_id}")
async def download_incident_clip(alert_id: int):
    """
    Downloads the forensic MP4 clip for a specific alert.
    """
    incident_path = os.path.join(BASE_DIR, "data", "incidents", f"incident_{alert_id}.mp4")
    if os.path.exists(incident_path):
        return FileResponse(incident_path, media_type="video/mp4", filename=f"incident_{alert_id}.mp4")
    raise HTTPException(status_code=404, detail="Incident video clip not ready or not found.")

@app.post("/api/reset")
async def reset_system():
    """
    Resets the counting logs and clears alerts.
    """
    clear_logs(DB_PATH)
    pipeline.counts = {'car': 0, 'bike': 0, 'bus': 0, 'truck': 0}
    pipeline.total_passed = 0
    pipeline.counted_ids.clear()
    pipeline.track_history.clear()
    # Reset anomaly detector state
    pipeline.anomaly_detector.active_alerts.clear()
    pipeline.anomaly_detector.stationary_since.clear()
    pipeline.anomaly_detector.trajectories.clear()
    return {"status": "success", "message": "System logs and counters have been reset."}

# Mount static folder for styling and javascript
# We do this after registering endpoints to avoid route overlapping
if os.path.exists(FRONTEND_DIR):
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
else:
    # Create frontend folder if it doesn't exist yet
    os.makedirs(FRONTEND_DIR, exist_ok=True)
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
