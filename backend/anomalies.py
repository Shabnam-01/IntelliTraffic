import time
from typing import Dict, List, Tuple, Optional
from .database import resolve_alert

class AnomalyDetector:
    def __init__(self, db_path: str, stopped_time_threshold: float = 4.0, speed_threshold: float = 1.5, fps: int = 30):
        """
        db_path: Path to SQLite DB.
        stopped_time_threshold: Seconds a vehicle must be nearly stationary to be flagged as stopped.
        speed_threshold: Maximum displacement in pixels per frame to consider a vehicle stationary.
        fps: Frames per second of the video source.
        """
        self.db_path = db_path
        self.stopped_time_threshold = stopped_time_threshold
        self.speed_threshold = speed_threshold
        self.fps = fps
        
        # Maps track_id -> List of Tuple (timestamp, centroid_x, centroid_y, bbox_width, bbox_height)
        self.trajectories: Dict[int, List[Tuple[float, float, float, float, float]]] = {}
        
        # Maps track_id -> timestamp when it first became stationary
        self.stationary_since: Dict[int, float] = {}
        
        # Maps track_id -> (alert_id, alert_type) of active alerts in DB (updated by pipeline after insert)
        self.active_alerts: Dict[int, Tuple[int, str]] = {}

    def update(self, current_time: float, active_tracks: List[dict]) -> List[dict]:
        """
        Update tracking history and run anomaly detection algorithms.
        active_tracks: List of dicts, each with keys: 'track_id', 'class_name', 'bbox' (x1, y1, x2, y2)
        Returns: List of new alert dicts (without DB ids) to be processed by pipeline.
        """
        new_alerts = []
        current_track_ids = set()

        for track in active_tracks:
            track_id = track['track_id']
            bbox = track['bbox']
            class_name = track['class_name']
            current_track_ids.add(track_id)
            
            # Centroid
            cx = (bbox[0] + bbox[2]) / 2.0
            cy = (bbox[1] + bbox[3]) / 2.0
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]

            if track_id not in self.trajectories:
                self.trajectories[track_id] = []
            
            # Record trajectory
            self.trajectories[track_id].append((current_time, cx, cy, w, h))
            
            # Limit history size (keep last 10 seconds of data)
            max_history = int(self.fps * 10)
            if len(self.trajectories[track_id]) > max_history:
                self.trajectories[track_id].pop(0)

            # Analyze Speed
            traj = self.trajectories[track_id]
            if len(traj) >= 5:
                # Calculate recent speed (displacement over last 5 frames)
                dx = traj[-1][1] - traj[-5][1]
                dy = traj[-1][2] - traj[-5][2]
                displacement = (dx**2 + dy**2) ** 0.5
                avg_speed_per_frame = displacement / 4.0
                
                # Calculate instant speed (displacement between the last 2 frames)
                dx_inst = traj[-1][1] - traj[-2][1]
                dy_inst = traj[-1][2] - traj[-2][2]
                inst_speed = (dx_inst**2 + dy_inst**2) ** 0.5
                
                # Check for Sudden Deceleration / Collision
                # If speed drops dramatically in a very short time from high speed
                if len(traj) >= 12:
                    # Speed 8 frames ago (frames -12 to -8)
                    dx_prev = traj[-8][1] - traj[-12][1]
                    dy_prev = traj[-8][2] - traj[-12][2]
                    prev_displacement = (dx_prev**2 + dy_prev**2) ** 0.5
                    prev_speed = prev_displacement / 4.0
                    
                    # If previously moving fast, and now suddenly stops (instant speed drops to near zero)
                    if prev_speed > 5.0 and inst_speed < 1.0:
                        # Ensure we don't spam multiple sudden deceleration alerts for the same track
                        if track_id not in self.active_alerts:
                            # Let's check for near collision (other vehicles nearby)
                            overlapping_vehicle = None
                            for other in active_tracks:
                                if other['track_id'] == track_id:
                                    continue
                                obox = other['bbox']
                                # Simple overlap check (Intersection over Union or proximity)
                                if self._is_close(bbox, obox):
                                    overlapping_vehicle = other
                                    break
                            
                            if overlapping_vehicle:
                                desc = f"Potential collision detected between {class_name} #{track_id} and {overlapping_vehicle['class_name']} #{overlapping_vehicle['track_id']}"
                                new_alerts.append({
                                    "type": "ACCIDENT",
                                    "vehicle_id": track_id,
                                    "description": desc,
                                    "bbox": bbox
                                })
                            else:
                                desc = f"Sudden deceleration anomaly detected for {class_name} #{track_id} (possible hard braking)"
                                new_alerts.append({
                                    "type": "STOPPED_VEHICLE",
                                    "vehicle_id": track_id,
                                    "description": desc,
                                    "bbox": bbox
                                })

                # Check for Stopped Vehicle
                # If displacement is low
                if avg_speed_per_frame < self.speed_threshold:
                    if track_id not in self.stationary_since:
                        self.stationary_since[track_id] = current_time
                    else:
                        stationary_duration = current_time - self.stationary_since[track_id]
                        if stationary_duration >= self.stopped_time_threshold:
                            # Trigger stopped alert if not already active
                            if track_id not in self.active_alerts:
                                desc = f"Vehicle {class_name} #{track_id} has been stationary for {int(stationary_duration)}s on active lane."
                                new_alerts.append({
                                    "type": "STOPPED_VEHICLE",
                                    "vehicle_id": track_id,
                                    "description": desc,
                                    "bbox": bbox
                                })
                else:
                    # Vehicle is moving
                    if track_id in self.stationary_since:
                        del self.stationary_since[track_id]
                    # If it was active alert, resolve it
                    if track_id in self.active_alerts:
                        alert_id, alert_type = self.active_alerts[track_id]
                        if alert_type == "STOPPED_VEHICLE":
                            # Only resolve if the vehicle has physically started moving again (instant speed >= threshold)
                            if inst_speed >= self.speed_threshold:
                                resolve_alert(self.db_path, alert_id)
                                del self.active_alerts[track_id]

        # Clean up history for tracks that are no longer active
        stored_tracks = list(self.trajectories.keys())
        for track_id in stored_tracks:
            if track_id not in current_track_ids:
                # Remove from tracking list
                if track_id in self.trajectories:
                    del self.trajectories[track_id]
                if track_id in self.stationary_since:
                    del self.stationary_since[track_id]
                # If there's an active alert for it, resolve it as vehicle left the frame
                if track_id in self.active_alerts:
                    alert_id, _ = self.active_alerts[track_id]
                    resolve_alert(self.db_path, alert_id)
                    del self.active_alerts[track_id]

        return new_alerts

    def _is_close(self, box1: Tuple[float, float, float, float], box2: Tuple[float, float, float, float]) -> bool:
        """
        Check if two bounding boxes are very close or overlapping.
        """
        # Expand box1 slightly and check intersection
        padding = 15.0
        x1_1, y1_1, x2_1, y2_1 = box1
        x1_2, y1_2, x2_2, y2_2 = box2

        # Overlap of expanded box1 with box2
        overlap_x = not (x2_1 + padding < x1_2 or x2_2 < x1_1 - padding)
        overlap_y = not (y2_1 + padding < y1_2 or y2_2 < y1_1 - padding)
        
        return overlap_x and overlap_y
