import cv2
import numpy as np
import time
import threading
import random
import collections
import queue
import base64
import os
from typing import Dict, List, Tuple, Optional
from ultralytics import YOLO
from .database import log_traffic_metrics, resolve_all_active_alerts, log_alert
from .anomalies import AnomalyDetector

def get_youtube_stream_url(youtube_url: str) -> Optional[str]:
    """
    Extracts the direct streaming URL from a YouTube video URL using yt-dlp.
    """
    try:
        import yt_dlp
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            if info:
                return info.get('url')
    except Exception as e:
        print(f"Error resolving YouTube URL using yt-dlp: {e}")
    return None

class TrafficPipeline:
    def __init__(self, db_path: str, model_name: str = "yolov8n.pt", video_source: str = ""):
        self.db_path = db_path
        self.model_name = model_name
        self.video_source = video_source
        
        # Load YOLO model (Lazy load only if not in simulation mode)
        self.model = None
        self.model_loaded = False
        
        # Class names we care about
        # COCO IDs: 2: car, 3: motorcycle, 5: bus, 7: truck
        self.target_classes = {2: 'car', 3: 'bike', 5: 'bus', 7: 'truck'}
        
        # Configuration
        self.counting_line: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None
        self.roi_polygon: Optional[np.ndarray] = None
        self.weather_mode = "sunny"  # sunny, night, foggy, rainy
        self.heatmap_enabled = False
        
        # Pipeline States
        self.is_running = False
        self.is_simulated = False
        self.current_frame: Optional[np.ndarray] = None
        self.fps = 30
        self.frame_width = 1280
        self.frame_height = 720
        
        # Stats counts
        self.counts = {'car': 0, 'bike': 0, 'bus': 0, 'truck': 0}
        self.total_passed = 0
        self.density_level = 'LOW'
        
        # Tracking history for line crossing: track_id -> previous centroid (x, y)
        self.track_history: Dict[int, Tuple[float, float]] = {}
        # Keep track of counted IDs to avoid double counting
        self.counted_ids = set()
        
        # Lock for frame access
        self.frame_lock = threading.Lock()
        
        # Anomaly Detector
        self.anomaly_detector = AnomalyDetector(db_path, stopped_time_threshold=4.0, speed_threshold=1.5, fps=30)
        
        # Simulation States
        self.sim_vehicles: List[dict] = []
        self.sim_next_id = 1
        self.sim_frame_count = 0
        
        # Forensics circular frame queue (stores last 10 seconds of raw frames)
        self.frame_buffer = collections.deque(maxlen=300)
        
        # Active incident writers: maps alert_id -> queue.Queue (to push post-incident frames)
        self.incident_queues: Dict[int, queue.Queue] = {}
        
        # Thread
        self.thread: Optional[threading.Thread] = None

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=3)

    def set_config(self, counting_line: Tuple[Tuple[int, int], Tuple[int, int]], roi_points: List[Tuple[int, int]]):
        """
        Update the counting line and ROI polygon.
        """
        self.counting_line = counting_line
        self.roi_polygon = np.array(roi_points, dtype=np.int32)

    def get_latest_frame(self) -> Optional[bytes]:
        """
        Returns the latest annotated frame encoded as JPEG bytes.
        """
        with self.frame_lock:
            if self.current_frame is None:
                return None
            ret, jpeg = cv2.imencode('.jpg', self.current_frame)
            if not ret:
                return None
            return jpeg.tobytes()

    def _intersect(self, p1: Tuple[float, float], p2: Tuple[float, float], q1: Tuple[float, float], q2: Tuple[float, float]) -> bool:
        """
        Checks if line segment p1-p2 intersects with q1-q2.
        """
        def ccw(A, B, C):
            return (C[1]-A[1]) * (B[0]-A[0]) > (B[1]-A[1]) * (C[0]-A[0])
        return ccw(p1, q1, q2) != ccw(p2, q1, q2) and ccw(p1, p2, q1) != ccw(p1, p2, q2)

    def _load_model_lazy(self):
        if not self.model_loaded:
            try:
                self.model = YOLO(self.model_name)
                self.model_loaded = True
            except Exception as e:
                print(f"Error loading YOLO model: {e}")

    def _run_pipeline(self):
        self.is_simulated = False
        cap = None
        
        # Check if source is a YouTube link
        is_youtube = "youtube.com" in self.video_source or "youtu.be" in self.video_source
        
        actual_source = self.video_source
        if is_youtube:
            print(f"YouTube URL detected. Resolving streaming link for '{self.video_source}'...")
            youtube_stream = get_youtube_stream_url(self.video_source)
            if youtube_stream:
                actual_source = youtube_stream
                print("YouTube streaming link resolved successfully.")
            else:
                print("Failed to resolve YouTube link. Falling back to Simulation Mode.")
                self.is_simulated = True
        
        if not self.video_source:
            print("No video source provided. Entering Simulation Mode.")
            self.is_simulated = True
            
        if not self.is_simulated:
            try:
                cap = cv2.VideoCapture(actual_source)
                if not cap.isOpened():
                    raise Exception("Cannot open video source stream")
            except Exception as e:
                print(f"Error opening source {self.video_source}: {e}. Attempting demo download...")
                demo_url = "https://assets.ultralytics.com/videos/traffic.mp4"
                try:
                    cap = cv2.VideoCapture(demo_url)
                    if not cap.isOpened():
                        raise Exception("Cannot open demo URL")
                except Exception as e_demo:
                    print(f"Failed to load demo video: {e_demo}. Falling back to Simulation Mode.")
                    self.is_simulated = True
                    cap = None
        
        # Reset counts and tracking history for the new session
        self.counts = {'car': 0, 'bike': 0, 'bus': 0, 'truck': 0}
        self.total_passed = 0
        self.counted_ids.clear()
        self.track_history.clear()
        
        # Resolve any leftover alerts from the previous session in DB and anomaly detector
        resolve_all_active_alerts(self.db_path)
        self.anomaly_detector.active_alerts.clear()
        self.anomaly_detector.stationary_since.clear()
        self.anomaly_detector.trajectories.clear()
        self.frame_buffer.clear()
        self.incident_queues.clear()

        if self.is_simulated:
            self.frame_width = 1280
            self.frame_height = 720
            self.fps = 30
            # Reset simulated states
            self.sim_vehicles = []
            self.sim_next_id = 1
            self.sim_frame_count = 0
        else:
            self.frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.fps = int(cap.get(cv2.CAP_PROP_FPS))
            if self.fps <= 0 or self.fps > 100:
                self.fps = 30
            self._load_model_lazy()
            
        # Re-initialize anomaly detector with correct FPS
        self.anomaly_detector.fps = self.fps
        self.frame_buffer = collections.deque(maxlen=self.fps * 10) # 10 seconds dynamic buffer

        # Initialize configurations if not set
        if self.counting_line is None:
            y_pos = int(self.frame_height * 0.7)
            self.counting_line = ((0, y_pos), (self.frame_width, y_pos))
            
        if self.roi_polygon is None:
            p1 = (int(self.frame_width * 0.1), self.frame_height)
            p2 = (int(self.frame_width * 0.35), int(self.frame_height * 0.4))
            p3 = (int(self.frame_width * 0.65), int(self.frame_height * 0.4))
            p4 = (int(self.frame_width * 0.9), self.frame_height)
            self.roi_polygon = np.array([p1, p2, p3, p4], dtype=np.int32)

        last_db_log_time = time.time()
        
        while self.is_running:
            start_time = time.time()
            active_tracks = []
            vehicles_in_roi = 0
            
            if self.is_simulated:
                # ----------------- SIMULATION MODE -----------------
                self._update_simulation()
                
                # Fetch tracks from simulator
                for v in self.sim_vehicles:
                    x1, y1 = v['x'], v['y']
                    x2, y2 = x1 + v['w'], y1 + v['h']
                    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    
                    active_tracks.append({
                        'track_id': v['track_id'],
                        'class_name': v['class_name'],
                        'bbox': (x1, y1, x2, y2)
                    })
                    
                    # Count ROI occupancy
                    if self.roi_polygon is not None:
                        is_inside = cv2.pointPolygonTest(self.roi_polygon, (float(cx), float(cy)), False)
                        if is_inside >= 0:
                            vehicles_in_roi += 1
                            
                    # Counting line crossing logic
                    if v['track_id'] in self.track_history and self.counting_line is not None:
                        prev_cx, prev_cy = self.track_history[v['track_id']]
                        crossed = self._intersect(
                            (prev_cx, prev_cy), (cx, cy),
                            self.counting_line[0], self.counting_line[1]
                        )
                        if crossed and v['track_id'] not in self.counted_ids:
                            self.counted_ids.add(v['track_id'])
                            self.counts[v['class_name']] += 1
                            self.total_passed += 1
                    
                    # Update trajectory history
                    self.track_history[v['track_id']] = (cx, cy)
                
                # Draw simulated base frame
                frame = self._render_simulation_frame(active_tracks, vehicles_in_roi)
                
            else:
                # ----------------- REAL YOLO TRACKING MODE -----------------
                ret, frame = cap.read()
                if not ret:
                    # Loop video
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                
                # Apply Weather Pre-processing filters
                frame = self._apply_weather_pre_processing(frame)
                
                # Run YOLO Tracking
                results = self.model.track(
                    source=frame,
                    persist=True,
                    tracker="bytetrack.yaml",
                    classes=list(self.target_classes.keys()),
                    verbose=False
                )
                
                if results and len(results) > 0 and results[0].boxes is not None:
                    boxes = results[0].boxes
                    for box in boxes:
                        if box.id is None:
                            continue
                        
                        track_id = int(box.id[0].item())
                        cls_id = int(box.cls[0].item())
                        class_name = self.target_classes.get(cls_id, 'car')
                        
                        coords = box.xyxy[0].tolist()
                        x1, y1, x2, y2 = coords
                        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                        
                        active_tracks.append({
                            'track_id': track_id,
                            'class_name': class_name,
                            'bbox': (x1, y1, x2, y2)
                        })
                        
                        if self.roi_polygon is not None:
                            is_inside = cv2.pointPolygonTest(self.roi_polygon, (float(cx), float(cy)), False)
                            if is_inside >= 0:
                                vehicles_in_roi += 1
                        
                        if track_id in self.track_history and self.counting_line is not None:
                            prev_cx, prev_cy = self.track_history[track_id]
                            crossed = self._intersect(
                                (prev_cx, prev_cy), (cx, cy),
                                self.counting_line[0], self.counting_line[1]
                            )
                            if crossed and track_id not in self.counted_ids:
                                self.counted_ids.add(track_id)
                                self.counts[class_name] += 1
                                self.total_passed += 1
                                
                        self.track_history[track_id] = (cx, cy)
            
            # Clean tracking histories
            current_track_ids = {t['track_id'] for t in active_tracks}
            dead_tracks = [tid for tid in list(self.track_history.keys()) if tid not in current_track_ids]
            for tid in dead_tracks:
                del self.track_history[tid]

            # Cache the raw frame before drawing UI details for forensics
            raw_cached_frame = frame.copy()
            self.frame_buffer.append(raw_cached_frame)
            
            # Push frame to active post-incident MP4 video writers
            for q_writer in list(self.incident_queues.values()):
                try:
                    q_writer.put_nowait(raw_cached_frame)
                except queue.Full:
                    pass

            # Run trajectory anomaly engine
            current_timestamp = time.time()
            triggered_alerts = self.anomaly_detector.update(current_timestamp, active_tracks)
            
            # Handle new alerts (perform Base64 cropping and spawn VideoWriter)
            for alert in triggered_alerts:
                tid = alert['vehicle_id']
                x1, y1, x2, y2 = map(int, alert['bbox'])
                
                # Clip box to frame boundary
                x1, x2 = max(0, x1), min(self.frame_width, x2)
                y1, y2 = max(0, y1), min(self.frame_height, y2)
                
                # Extract Base64 thumbnail crop
                thumbnail_base64 = None
                if x2 > x1 and y2 > y1:
                    crop = raw_cached_frame[y1:y2, x1:x2]
                    ret_enc, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ret_enc:
                        b64_data = base64.b64encode(buf).decode('utf-8')
                        thumbnail_base64 = f"data:image/jpeg;base64,{b64_data}"
                
                # Log Alert to DB
                alert_id = log_alert(
                    self.db_path,
                    alert_type=alert['type'],
                    vehicle_id=tid,
                    description=alert['description'],
                    thumbnail=thumbnail_base64
                )
                
                # Link alert ID inside anomaly detector active mapping
                self.anomaly_detector.active_alerts[tid] = (alert_id, alert['type'])
                
                # Spawn background incident video clip writer thread
                self._spawn_incident_recorder(alert_id)

            # Density level calculation
            if vehicles_in_roi < 4:
                self.density_level = 'LOW'
            elif vehicles_in_roi <= 8:
                self.density_level = 'MEDIUM'
            else:
                self.density_level = 'HIGH'

            # Log metrics to DB
            if current_timestamp - last_db_log_time >= 3.0:
                log_traffic_metrics(
                    self.db_path,
                    cars=self.counts['car'],
                    buses=self.counts['bus'],
                    trucks=self.counts['truck'],
                    bikes=self.counts['bike'],
                    total_passed=self.total_passed,
                    density_level=self.density_level
                )
                last_db_log_time = current_timestamp

            # Render HUD, Speed Heatmap, and bounding box overlays
            if not self.is_simulated:
                annotated_frame = frame.copy()
                
                # Apply Speed Congestion Heatmap overlay
                if self.heatmap_enabled:
                    annotated_frame = self._render_speed_heatmap(annotated_frame, active_tracks)
                
                # Draw ROI
                if self.roi_polygon is not None:
                    overlay = annotated_frame.copy()
                    cv2.fillPoly(overlay, [self.roi_polygon], (0, 255, 100))
                    cv2.addWeighted(overlay, 0.1, annotated_frame, 0.9, 0, annotated_frame)
                    cv2.polylines(annotated_frame, [self.roi_polygon], True, (0, 255, 100), 2)
                
                # Draw Line
                if self.counting_line is not None:
                    cv2.line(annotated_frame, self.counting_line[0], self.counting_line[1], (255, 0, 150), 3)
                    cv2.putText(annotated_frame, f"COUNTING LINE - TOTAL: {self.total_passed}", 
                                (self.counting_line[0][0] + 20, self.counting_line[0][1] - 10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 150), 2)
                
                # Draw boxes
                for track in active_tracks:
                    tid = track['track_id']
                    x1, y1, x2, y2 = map(int, track['bbox'])
                    cls = track['class_name']
                    
                    color = (0, 255, 0)
                    if cls == 'bike':
                        color = (255, 255, 0)
                    elif cls == 'bus':
                        color = (255, 0, 255)
                    elif cls == 'truck':
                        color = (0, 165, 255)
                    
                    is_anomaly = tid in self.anomaly_detector.active_alerts
                    if is_anomaly:
                        color = (0, 0, 255)
                        cv2.putText(annotated_frame, "!!! ANOMALY !!!", (x1, y1 - 25), 
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                    
                    cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2 if not is_anomaly else 3)
                    cv2.putText(annotated_frame, f"{cls} #{tid}", (x1, y1 - 8), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
                
                # Draw Top HUD
                hud_bg = annotated_frame.copy()
                cv2.rectangle(hud_bg, (10, 10), (320, 210), (30, 30, 30), -1)
                cv2.addWeighted(hud_bg, 0.7, annotated_frame, 0.3, 0, annotated_frame)
                
                cv2.putText(annotated_frame, "TRAFFIC INTELLIGENCE HUD", (20, 35), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                density_color = (0, 255, 0) if self.density_level == 'LOW' else ((0, 255, 255) if self.density_level == 'MEDIUM' else (0, 0, 255))
                cv2.putText(annotated_frame, f"DENSITY: {self.density_level}", (20, 65), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, density_color, 2)
                cv2.putText(annotated_frame, f"Cars: {self.counts['car']} | Bikes: {self.counts['bike']}", (20, 95), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
                cv2.putText(annotated_frame, f"Buses: {self.counts['bus']} | Trucks: {self.counts['truck']}", (20, 125), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
                cv2.putText(annotated_frame, f"Active Vehicles in ROI: {vehicles_in_roi}", (20, 155), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
                cv2.putText(annotated_frame, f"Calibration: {self.weather_mode.upper()} MODE", (20, 180), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 242, 254), 1)
                
                frame = annotated_frame

            with self.frame_lock:
                self.current_frame = frame
                
            elapsed_time = time.time() - start_time
            sleep_time = max(1.0 / self.fps - elapsed_time, 0.001)
            time.sleep(sleep_time)
            
        if cap:
            cap.release()

    # ------------------ PRE-PROCESSING WEATHER FILTERS ------------------
    def _apply_weather_pre_processing(self, frame: np.ndarray) -> np.ndarray:
        """
        Enhances raw frame feeds based on selected weather configuration.
        """
        if self.weather_mode == "night":
            # CLAHE on luminance to enhance dark lane visibility
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            
            # Draw blueish ambient darkness overlay
            dark_mask = np.zeros_like(frame)
            dark_mask[:] = (60, 25, 10)  # Dark blue-black tint
            cv2.addWeighted(dark_mask, 0.45, frame, 0.55, 0, frame)
            
        elif self.weather_mode == "foggy":
            # Dark Channel prior replacement: lower contrast and overlay mist
            clahe = cv2.createCLAHE(clipLimit=1.2, tileGridSize=(8, 8))
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            
            # Mist overlay
            fog_overlay = np.zeros_like(frame)
            fog_overlay[:] = (210, 210, 210) # light grey mist
            cv2.addWeighted(fog_overlay, 0.35, frame, 0.65, 0, frame)
            
        elif self.weather_mode == "rainy":
            # Bilateral filter to smooth rain streaks while keeping vehicle edges sharp
            frame = cv2.bilateralFilter(frame, 9, 75, 75)
            
        return frame

    # ------------------ SPEED CONGESTION HEATMAP OVERLAY ------------------
    def _render_speed_heatmap(self, frame: np.ndarray, active_tracks: List[dict]) -> np.ndarray:
        """
        Calculates traffic flow speed in 3 highway lanes and draws transparent colored congestion overlays.
        """
        # Lane bounds: 1 (200-493), 2 (493-786), 3 (786-1080)
        lanes = {
            1: {"x1": 200, "x2": 493, "speeds": []},
            2: {"x1": 493, "x2": 786, "speeds": []},
            3: {"x1": 786, "x2": 1080, "speeds": []}
        }
        
        for track in active_tracks:
            tid = track['track_id']
            bbox = track['bbox']
            cx = (bbox[0] + bbox[2]) / 2.0
            
            # Determine lane
            active_lane = None
            if lanes[1]["x1"] <= cx < lanes[1]["x2"]:
                active_lane = 1
            elif lanes[2]["x1"] <= cx < lanes[2]["x2"]:
                active_lane = 2
            elif lanes[3]["x1"] <= cx <= lanes[3]["x2"]:
                active_lane = 3
                
            if active_lane and tid in self.anomaly_detector.trajectories:
                traj = self.anomaly_detector.trajectories[tid]
                if len(traj) >= 5:
                    dx = traj[-1][1] - traj[-5][1]
                    dy = traj[-1][2] - traj[-5][2]
                    speed = (dx**2 + dy**2) ** 0.5 / 4.0
                    lanes[active_lane]["speeds"].append(speed)

        # Draw overlays
        overlay = frame.copy()
        for l_idx, l_info in lanes.items():
            avg_s = np.mean(l_info["speeds"]) if l_info["speeds"] else 8.0 # Default to free flow if empty
            
            # Green (Free Flow): speed >= 5px
            # Yellow (Slowing): speed 2.0 to 5px
            # Red (Congested): speed < 2px
            if avg_s >= 5.0:
                color = (0, 200, 0)
            elif avg_s >= 2.0:
                color = (0, 165, 255)
            else:
                color = (0, 0, 255)
                
            # Draw lane segment overlay
            cv2.rectangle(overlay, (l_info["x1"], 0), (l_info["x2"], self.frame_height), color, -1)
            
        cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)
        return frame

    # ------------------ BACKGROUND FORENSIC VIDEO RECORDER ------------------
    def _spawn_incident_recorder(self, alert_id: int):
        """
        Creates a frame queue and launches a thread to save a 15-second incident clip.
        """
        if alert_id in self.incident_queues:
            return
            
        q = queue.Queue(maxsize=150) # Buffer to collect next 5 seconds (150 frames at 30 fps)
        self.incident_queues[alert_id] = q
        
        # Take a copy of current pre-incident frames buffer (last 10 seconds)
        pre_frames = list(self.frame_buffer)
        
        # Start compiler thread
        t = threading.Thread(
            target=self._compile_incident_clip,
            args=(alert_id, pre_frames, q),
            daemon=True
        )
        t.start()

    def _compile_incident_clip(self, alert_id: int, pre_frames: List[np.ndarray], post_queue: queue.Queue):
        # Allow a few frames to accumulate
        time.sleep(1.0)
        
        # Create storage path
        incident_dir = os.path.join(os.path.dirname(self.db_path), "incidents")
        os.makedirs(incident_dir, exist_ok=True)
        clip_path = os.path.join(incident_dir, f"incident_{alert_id}.mp4")
        
        if not pre_frames:
            # Fallback if queue is empty
            self.incident_queues.pop(alert_id, None)
            return
            
        h, w = pre_frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'mp4v') # Highly portable format
        writer = cv2.VideoWriter(clip_path, fourcc, 30.0, (w, h))
        
        # 1. Write the 10 seconds of pre-incident video
        for f in pre_frames:
            writer.write(f)
            
        # 2. Write the 5 seconds of post-incident video
        written_post = 0
        max_post_frames = 150
        while written_post < max_post_frames:
            try:
                # Wait up to 1 second for frames to be pushed from main loop
                f = post_queue.get(timeout=1.0)
                writer.write(f)
                written_post += 1
            except queue.Empty:
                break
                
        writer.release()
        
        # Remove queue when writing is done
        self.incident_queues.pop(alert_id, None)
        print(f"Forensic incident clip generated successfully: {clip_path}")

    # ------------------ SIMULATOR METHODS ------------------
    def _update_simulation(self):
        self.sim_frame_count += 1
        
        # Spawning logic: Every 45 frames on average
        if self.sim_frame_count % random.randint(35, 55) == 0:
            lane = random.choice([1, 2, 3])
            class_name = random.choices(['car', 'truck', 'bus', 'bike'], weights=[65, 15, 10, 10])[0]
            
            if class_name == 'car':
                w, h = 50, 85
                speed = random.uniform(6.0, 9.0)
            elif class_name == 'truck':
                w, h = 65, 130
                speed = random.uniform(4.5, 6.0)
            elif class_name == 'bus':
                w, h = 70, 150
                speed = random.uniform(4.0, 5.0)
            else: # bike
                w, h = 25, 50
                speed = random.uniform(7.5, 11.0)
                
            lane_center = {1: 350, 2: 640, 3: 930}[lane]
            spawn_x = lane_center - (w / 2) + random.uniform(-15, 15)
            spawn_y = -h - 10
            
            behavior = 'normal'
            rand_val = random.random()
            
            if len(self.anomaly_detector.active_alerts) == 0 and rand_val < 0.07:
                behavior = 'breakdown'
            elif rand_val < 0.11:
                stopped_in_lane = any(v for v in self.sim_vehicles if v['lane'] == lane and v['state'] == 'stopped')
                if stopped_in_lane:
                    behavior = 'crash_victim'
            
            self.sim_vehicles.append({
                'track_id': self.sim_next_id,
                'class_name': class_name,
                'x': spawn_x,
                'y': spawn_y,
                'w': w,
                'h': h,
                'speed_y': speed,
                'lane': lane,
                'state': 'moving',
                'stopped_frame_counter': 0,
                'behavior': behavior,
                'color': (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255))
            })
            self.sim_next_id += 1

        # Update vehicle states
        for v in list(self.sim_vehicles):
            if v['state'] == 'moving':
                v['y'] += v['speed_y']
                
                # Check for breakdown stop trigger
                if v['behavior'] == 'breakdown' and v['y'] >= 320:
                    v['speed_y'] -= 1.0
                    if v['speed_y'] <= 0:
                        v['speed_y'] = 0
                        v['state'] = 'stopped'
                
                # Check for collision trigger
                elif v['behavior'] == 'crash_victim':
                    ahead = next((other for other in self.sim_vehicles 
                                  if other['lane'] == v['lane'] 
                                  and other['track_id'] != v['track_id'] 
                                  and other['state'] == 'stopped'
                                  and other['y'] > v['y']), None)
                    
                    if ahead and (ahead['y'] - (v['y'] + v['h'])) <= 10:
                        v['speed_y'] = 0
                        v['state'] = 'collided'
                        ahead['state'] = 'collided'
            
            elif v['state'] == 'stopped' or v['state'] == 'collided':
                v['stopped_frame_counter'] += 1
                if v['stopped_frame_counter'] >= 220:
                    if v in self.sim_vehicles:
                        self.sim_vehicles.remove(v)
                    continue

            if v['y'] > self.frame_height + 20:
                if v in self.sim_vehicles:
                    self.sim_vehicles.remove(v)

    def _render_simulation_frame(self, active_tracks: List[dict], vehicles_in_roi: int) -> np.ndarray:
        # Create dark highway background
        frame = np.zeros((self.frame_height, self.frame_width, 3), dtype=np.uint8)
        frame[:] = (20, 24, 30)
        
        # Draw shoulders
        cv2.rectangle(frame, (0, 0), (200, 720), (12, 38, 20), -1)
        cv2.rectangle(frame, (1080, 0), (1280, 720), (12, 38, 20), -1)
        
        # Draw highway gray lanes
        cv2.rectangle(frame, (200, 0), (1080, 720), (45, 50, 56), -1)
        
        # Draw solid boundaries
        cv2.line(frame, (200, 0), (200, 720), (220, 220, 220), 4)
        cv2.line(frame, (1080, 0), (1080, 720), (220, 220, 220), 4)
        
        # Draw separators
        for y in range(0, 720, 40):
            if (y // 40) % 2 == 0:
                cv2.line(frame, (493, y), (493, y + 25), (0, 200, 230), 2)
                cv2.line(frame, (786, y), (786, y + 25), (0, 200, 230), 2)
                
        # Draw speed congestion overlay if enabled
        if self.heatmap_enabled:
            frame = self._render_speed_heatmap(frame, active_tracks)

        # Draw ROI Polygon overlay
        if self.roi_polygon is not None:
            overlay = frame.copy()
            cv2.fillPoly(overlay, [self.roi_polygon], (0, 255, 100))
            cv2.addWeighted(overlay, 0.1, frame, 0.9, 0, frame)
            cv2.polylines(frame, [self.roi_polygon], True, (0, 255, 100), 2)
            
        # Draw Counting Line
        if self.counting_line is not None:
            cv2.line(frame, self.counting_line[0], self.counting_line[1], (255, 0, 150), 3)
            cv2.putText(frame, f"COUNTING LINE - TOTAL SIMULATED PASSED: {self.total_passed}", 
                        (self.counting_line[0][0] + 50, self.counting_line[0][1] - 12), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 150), 2)

        # Draw simulated vehicles
        for v in self.sim_vehicles:
            x1, y1 = int(v['x']), int(v['y'])
            w, h = int(v['w']), int(v['h'])
            x2, y2 = x1 + w, y1 + h
            cls = v['class_name']
            tid = v['track_id']
            
            if v['state'] == 'collided':
                color = (0, 0, 255)
            elif v['state'] == 'stopped':
                color = (0, 100, 255)
            else:
                color = (0, 255, 0) if cls == 'car' else ((255, 255, 0) if cls == 'bike' else ((255, 0, 255) if cls == 'bus' else (0, 165, 255)))
                
            is_anomaly = tid in self.anomaly_detector.active_alerts
            if is_anomaly:
                color = (0, 0, 255)
                alert_type = self.anomaly_detector.active_alerts[tid][1]
                cv2.putText(frame, f"!!! {alert_type} !!!", (x1, y1 - 25), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                
            # Draw vehicle body
            v_body = frame.copy()
            cv2.rectangle(v_body, (x1, y1), (x2, y2), color, -1)
            cv2.addWeighted(v_body, 0.25, frame, 0.75, 0, frame)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2 if not is_anomaly else 3)
            
            # Draw collision symbols if crashed
            if v['state'] == 'collided':
                cv2.line(frame, (x1 - 10, y1 - 10), (x2 + 10, y2 + 10), (0, 150, 255), 2)
                cv2.line(frame, (x2 + 10, y1 - 10), (x1 - 10, y2 + 10), (0, 150, 255), 2)
                cv2.putText(frame, "IMPACT", (x1 - 5, y1 - 10), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            
            # Draw simulated headlights if in night mode
            if self.weather_mode == "night":
                # Draw yellow/white transparent cones extending downwards
                cone = frame.copy()
                cone_pts = np.array([
                    [x1 + 5, y2], [x1 - 20, y2 + 100], 
                    [x2 + 20, y2 + 100], [x2 - 5, y2]
                ], dtype=np.int32)
                cv2.fillPoly(cone, [cone_pts], (200, 255, 255))
                cv2.addWeighted(cone, 0.25, frame, 0.75, 0, frame)

            cv2.putText(frame, f"SIM_{cls} #{tid}", (x1, y1 - 6), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

        # Apply Weather Calibration visual overlays on top of simulated frames
        frame = self._apply_simulation_weather_overlays(frame)

        # Draw HUD info top left
        hud_bg = frame.copy()
        cv2.rectangle(hud_bg, (10, 10), (380, 205), (30, 30, 30), -1)
        cv2.addWeighted(hud_bg, 0.75, frame, 0.25, 0, frame)
        
        cv2.putText(frame, "TRAFFIC INTELLIGENCE (SIMULATION MODE)", (20, 35), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 242, 254), 2)
        
        density_color = (0, 255, 0) if self.density_level == 'LOW' else ((0, 255, 255) if self.density_level == 'MEDIUM' else (0, 0, 255))
        cv2.putText(frame, f"DENSITY: {self.density_level}", (20, 65), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, density_color, 2)
        
        cv2.putText(frame, f"Cars: {self.counts['car']} | Bikes: {self.counts['bike']}", (20, 95), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(frame, f"Buses: {self.counts['bus']} | Trucks: {self.counts['truck']}", (20, 125), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(frame, f"Active simulated in ROI: {vehicles_in_roi}", (20, 155), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, f"Calibration: {self.weather_mode.upper()} MODE", (20, 175), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 242, 254), 1)
        cv2.putText(frame, "Live Simulation Fallback active. Offline mode.", (20, 192),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 150, 180), 1)
        
        return frame

    def _apply_simulation_weather_overlays(self, frame: np.ndarray) -> np.ndarray:
        """
        Draws active environment visual elements like rain drops or foggy mist over the frame.
        """
        if self.weather_mode == "night":
            # Apply blue/black night tint
            night_overlay = np.zeros_like(frame)
            night_overlay[:] = (55, 20, 8)
            cv2.addWeighted(night_overlay, 0.4, frame, 0.6, 0, frame)
            
        elif self.weather_mode == "foggy":
            # Dense white fog mist
            fog_overlay = np.zeros_like(frame)
            fog_overlay[:] = (220, 220, 220)
            cv2.addWeighted(fog_overlay, 0.35, frame, 0.65, 0, frame)
            
        elif self.weather_mode == "rainy":
            # Draw bilateral wet blur
            frame = cv2.bilateralFilter(frame, 5, 50, 50)
            # Draw random rain streaks
            for _ in range(35):
                rx = random.randint(0, self.frame_width - 1)
                ry = random.randint(0, self.frame_height - 30)
                rl = random.randint(10, 25)
                # Diagonal streaks
                cv2.line(frame, (rx, ry), (rx - 5, ry + rl), (200, 170, 140), 1) # light blue/grey rain lines
                
        return frame
