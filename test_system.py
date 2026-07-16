import os
import sys
import unittest
import tempfile
import sqlite3
import time
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from backend.database import (
    init_db,
    log_traffic_metrics,
    log_alert,
    resolve_alert,
    get_latest_metrics,
    get_historical_metrics,
    get_active_alerts,
    get_recent_alerts
)
from backend.anomalies import AnomalyDetector
from backend.pipeline import TrafficPipeline

class TestTrafficDatabase(unittest.TestCase):
    def setUp(self):
        # Create a temporary file for database
        self.db_fd, self.db_path = tempfile.mkstemp()
        init_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_log_traffic_metrics(self):
        # Insert stats
        log_traffic_metrics(self.db_path, cars=5, buses=2, trucks=1, bikes=4, total_passed=12, density_level="MEDIUM")
        
        # Retrieve latest
        latest = get_latest_metrics(self.db_path)
        self.assertIsNotNone(latest)
        self.assertEqual(latest['cars_count'], 5)
        self.assertEqual(latest['buses_count'], 2)
        self.assertEqual(latest['trucks_count'], 1)
        self.assertEqual(latest['bikes_count'], 4)
        self.assertEqual(latest['total_passed'], 12)
        self.assertEqual(latest['density_level'], "MEDIUM")

    def test_alerts_logging_and_resolving(self):
        # Log alert
        alert_id = log_alert(self.db_path, "STOPPED_VEHICLE", 42, "Car #42 has stopped on Lane 1")
        self.assertGreater(alert_id, 0)
        
        # Check active alerts
        active = get_active_alerts(self.db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['vehicle_id'], 42)
        self.assertEqual(active[0]['is_active'], 1)
        
        # Resolve alert
        resolve_alert(self.db_path, alert_id)
        
        # Check active alerts (should be empty)
        active = get_active_alerts(self.db_path)
        self.assertEqual(len(active), 0)
        
        # Check recent alerts (should contain the resolved alert)
        recent = get_recent_alerts(self.db_path)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['is_active'], 0)


class TestIntersectionMath(unittest.TestCase):
    def setUp(self):
        self.pipeline = TrafficPipeline(":memory:", video_source="")

    def test_intersecting_lines(self):
        # Standard cross
        p1 = (50, 50)
        p2 = (50, 150)
        q1 = (0, 100)
        q2 = (100, 100)
        self.assertTrue(self.pipeline._intersect(p1, p2, q1, q2))

    def test_non_intersecting_lines(self):
        # Parallel lines
        p1 = (50, 50)
        p2 = (50, 150)
        q1 = (100, 50)
        q2 = (100, 150)
        self.assertFalse(self.pipeline._intersect(p1, p2, q1, q2))

        # Close but no crossing
        p1 = (10, 10)
        p2 = (40, 40)
        q1 = (50, 50)
        q2 = (90, 90)
        self.assertFalse(self.pipeline._intersect(p1, p2, q1, q2))


class TestAnomalyDetection(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        init_db(self.db_path)
        # 1-second stopped threshold for quick testing
        self.detector = AnomalyDetector(self.db_path, stopped_time_threshold=1.0, speed_threshold=1.0, fps=10)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _update_detector(self, t, tracks):
        alerts = self.detector.update(t, tracks)
        for a in alerts:
            alert_id = log_alert(self.db_path, a['type'], a['vehicle_id'], a['description'], "data:image/jpeg;base64,mockstring")
            self.detector.active_alerts[a['vehicle_id']] = (alert_id, a['type'])
        return alerts

    def test_stopped_vehicle_alert(self):
        t = time.time()
        
        # Simulate static vehicle over 20 frames (2.0 seconds at 10 fps)
        for i in range(20):
            tracks = [{
                'track_id': 1,
                'class_name': 'car',
                'bbox': (100, 100, 150, 150) # completely stationary coordinates
            }]
            alerts = self._update_detector(t + (i * 0.1), tracks)
            
            # Speed check begins at frame 4 (0.4s) when history is >= 5 frames.
            # The stationary timer starts at 0.4s.
            # With a 1.0s threshold, the alert triggers at 1.4s (frame 14).
            if i >= 14:
                active_alerts = get_active_alerts(self.db_path)
                self.assertEqual(len(active_alerts), 1)
                self.assertEqual(active_alerts[0]['type'], "STOPPED_VEHICLE")
                self.assertEqual(active_alerts[0]['vehicle_id'], 1)
            else:
                active_alerts = get_active_alerts(self.db_path)
                self.assertEqual(len(active_alerts), 0)

        # Now simulate vehicle moving again
        for i in range(20, 25):
            tracks = [{
                'track_id': 1,
                'class_name': 'car',
                'bbox': (100 + (i * 10), 100, 150 + (i * 10), 150) # moving horizontally 10 pixels per frame
            }]
            self._update_detector(t + (i * 0.1), tracks)
        
        # Alert should be automatically resolved
        active_alerts = get_active_alerts(self.db_path)
        self.assertEqual(len(active_alerts), 0)
        
        recent = get_recent_alerts(self.db_path)
        resolved_car_alerts = [a for a in recent if a['vehicle_id'] == 1 and a['is_active'] == 0]
        self.assertEqual(len(resolved_car_alerts), 1)

    def test_sudden_deceleration_collision(self):
        t = time.time()
        
        # Vehicle 1 moving fast (10 pixels per frame displacement)
        # Frame 0 to 14
        for i in range(15):
            tracks = [
                {'track_id': 1, 'class_name': 'car', 'bbox': (10 * i, 100, 10 * i + 40, 140)},
                {'track_id': 2, 'class_name': 'truck', 'bbox': (140, 100, 200, 160)} # stationary truck
            ]
            self._update_detector(t + (i * 0.1), tracks)
            
        # Frame 15: Car 1 stops suddenly (displacement = 0) right near truck 2 (coordinates are close)
        tracks = [
            {'track_id': 1, 'class_name': 'car', 'bbox': (140, 100, 180, 140)}, # stationary now, overlapping truck
            {'track_id': 2, 'class_name': 'truck', 'bbox': (140, 100, 200, 160)}
        ]
        alerts = self._update_detector(t + 1.5, tracks)
        
        # Verify collision alert generated
        active = get_active_alerts(self.db_path)
        # There should be 2 active alerts (Truck stopped, Car accident)
        self.assertEqual(len(active), 2)
        
        car_alerts = [a for a in active if a['vehicle_id'] == 1]
        self.assertEqual(len(car_alerts), 1)
        self.assertEqual(car_alerts[0]['type'], "ACCIDENT")
        self.assertIn("collision", car_alerts[0]['description'].lower())
        self.assertEqual(car_alerts[0]['thumbnail'], "data:image/jpeg;base64,mockstring")


if __name__ == "__main__":
    unittest.main()
