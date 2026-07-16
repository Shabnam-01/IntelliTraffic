// Global variables
let flowChart = null;
let classChart = null;
let frameWidth = 1280;
let frameHeight = 720;
let knownAlertIds = new Set();

// Canvas Coordinates Config Editor
let roiPoints = [];   // [{x, y}, {x, y}, {x, y}, {x, y}]
let linePoints = [];  // [{x, y}, {x, y}]
let draggedPoint = null;

// DOM Elements
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const kpiTotal = document.getElementById('kpi-total');
const kpiDensity = document.getElementById('kpi-density');
const densityIconContainer = document.getElementById('density-icon-container');
const kpiAlertsCount = document.getElementById('kpi-alerts-count');
const activeAlertsBadge = document.getElementById('active-alerts-badge');
const alertList = document.getElementById('alert-list');
const configForm = document.getElementById('config-form');
const videoSourceInput = document.getElementById('video-source');
const weatherModeSelect = document.getElementById('weather-mode');
const enableHeatmapCheckbox = document.getElementById('enable-heatmap');
const resetCountsBtn = document.getElementById('reset-counts-btn');

const canvas = document.getElementById('config-canvas');
const ctx = canvas.getContext('2d');

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    fetchConfig();
    
    // Poll API routes
    setInterval(updateStats, 1000);
    setInterval(updateAlerts, 2000);
    
    // Bind UI actions
    configForm.addEventListener('submit', handleConfigSubmit);
    resetCountsBtn.addEventListener('click', handleReset);
    
    // Canvas interaction listeners
    canvas.addEventListener('mousedown', handleCanvasMouseDown);
    canvas.addEventListener('mousemove', handleCanvasMouseMove);
    window.addEventListener('mouseup', handleCanvasMouseUp);
    window.addEventListener('resize', () => drawCanvas());
});

// Setup Chart.js instances
function initCharts() {
    const ctxFlow = document.getElementById('flowChart').getContext('2d');
    flowChart = new Chart(ctxFlow, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Cumulative Vehicle Throughput',
                data: [],
                borderColor: '#00f2fe',
                borderWidth: 2,
                backgroundColor: 'rgba(0, 242, 254, 0.05)',
                fill: true,
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { grid: { display: false }, ticks: { display: false } },
                y: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#94a3b8' } }
            }
        }
    });

    const ctxClass = document.getElementById('classChart').getContext('2d');
    classChart = new Chart(ctxClass, {
        type: 'doughnut',
        data: {
            labels: ['Cars', 'Bikes', 'Buses', 'Trucks'],
            datasets: [{
                data: [0, 0, 0, 0],
                backgroundColor: ['#00f2a9', '#00f2fe', '#9d4edd', '#ff9f1c'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: '#94a3b8', font: { size: 10, family: 'Outfit' }, padding: 10 }
                }
            },
            cutout: '75%'
        }
    });
}

// Fetch current configurations
async function fetchConfig() {
    try {
        const response = await fetch('/api/config');
        if (!response.ok) throw new Error("Config fetch failed");
        const data = await response.json();
        
        videoSourceInput.value = data.video_source || "";
        weatherModeSelect.value = data.weather_mode || "sunny";
        enableHeatmapCheckbox.checked = !!data.heatmap_enabled;
        
        // Sync Line points
        if (data.line_p1 && data.line_p2) {
            linePoints = [data.line_p1, data.line_p2];
        } else {
            linePoints = [{x: 0, y: 504}, {x: 1280, y: 504}];
        }
        
        // Sync ROI points
        if (data.roi_points && data.roi_points.length === 4) {
            roiPoints = data.roi_points;
        } else {
            roiPoints = [
                {x: 128, y: 720},
                {x: 448, y: 288},
                {x: 832, y: 288},
                {x: 1152, y: 720}
            ];
        }
        
        // Draw the initial canvas layout
        setTimeout(() => drawCanvas(), 500);
        
    } catch (err) {
        console.error("Error fetching config:", err);
    }
}

// ------------------ CANVAS EDITOR PHYSICS ------------------

// Coordinate converters: scaled to match current size on-screen
function toScreen(pt) {
    const rect = canvas.getBoundingClientRect();
    return {
        x: (pt.x / frameWidth) * rect.width,
        y: (pt.y / frameHeight) * rect.height
    };
}

function toBackend(pt) {
    const rect = canvas.getBoundingClientRect();
    return {
        x: Math.round((pt.x / rect.width) * frameWidth),
        y: Math.round((pt.y / rect.height) * frameHeight)
    };
}

function drawCanvas() {
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    if (canvas.width === 0 || canvas.height === 0) return;
    
    // 1. Draw ROI Area Fill & Border
    if (roiPoints.length === 4) {
        const screenRoi = roiPoints.map(toScreen);
        
        // Shade
        ctx.fillStyle = 'rgba(0, 242, 169, 0.1)';
        ctx.beginPath();
        ctx.moveTo(screenRoi[0].x, screenRoi[0].y);
        ctx.lineTo(screenRoi[1].x, screenRoi[1].y);
        ctx.lineTo(screenRoi[2].x, screenRoi[2].y);
        ctx.lineTo(screenRoi[3].x, screenRoi[3].y);
        ctx.closePath();
        ctx.fill();
        
        // Stroke
        ctx.strokeStyle = '#00f2a9';
        ctx.lineWidth = 2.5;
        ctx.stroke();
        
        // Handles
        ctx.fillStyle = '#00f2a9';
        screenRoi.forEach((pt) => {
            ctx.beginPath();
            ctx.arc(pt.x, pt.y, 8, 0, 2 * Math.PI);
            ctx.fill();
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 2;
            ctx.stroke();
        });
    }
    
    // 2. Draw Counting Line
    if (linePoints.length === 2) {
        const screenLine = linePoints.map(toScreen);
        
        ctx.strokeStyle = '#ff3366';
        ctx.lineWidth = 3.5;
        ctx.beginPath();
        ctx.moveTo(screenLine[0].x, screenLine[0].y);
        ctx.lineTo(screenLine[1].x, screenLine[1].y);
        ctx.stroke();
        
        // Handles
        ctx.fillStyle = '#ff3366';
        screenLine.forEach((pt) => {
            ctx.beginPath();
            ctx.arc(pt.x, pt.y, 8, 0, 2 * Math.PI);
            ctx.fill();
            ctx.strokeStyle = '#ffffff';
            ctx.lineWidth = 2;
            ctx.stroke();
        });
    }
}

function handleCanvasMouseDown(e) {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    
    // Try to grab counting line handle
    const screenLine = linePoints.map(toScreen);
    for (let i = 0; i < screenLine.length; i++) {
        if (Math.hypot(screenLine[i].x - mx, screenLine[i].y - my) < 18) {
            draggedPoint = { type: 'line', index: i };
            return;
        }
    }
    
    // Try to grab ROI point handle
    const screenRoi = roiPoints.map(toScreen);
    for (let i = 0; i < screenRoi.length; i++) {
        if (Math.hypot(screenRoi[i].x - mx, screenRoi[i].y - my) < 18) {
            draggedPoint = { type: 'roi', index: i };
            return;
        }
    }
}

function handleCanvasMouseMove(e) {
    if (!draggedPoint) return;
    
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    
    // Clamp inside canvas boundary
    const x = Math.max(0, Math.min(rect.width, mx));
    const y = Math.max(0, Math.min(rect.height, my));
    
    const backendPt = toBackend({ x, y });
    
    if (draggedPoint.type === 'line') {
        linePoints[draggedPoint.index] = backendPt;
    } else {
        roiPoints[draggedPoint.index] = backendPt;
    }
    
    drawCanvas();
}

function handleCanvasMouseUp() {
    draggedPoint = null;
}

// -----------------------------------------------------------

// Poll Realtime Statistics
async function updateStats() {
    try {
        const response = await fetch('/api/stats/realtime');
        if (!response.ok) throw new Error("Offline");
        const data = await response.json();

        // Update Server Status Connection
        statusDot.className = "pulse-dot online";
        statusText.innerText = "CONNECTED";
        
        // Save frame dims
        frameWidth = data.resolution.width || 1280;
        frameHeight = data.resolution.height || 720;

        // Update numbers
        kpiTotal.innerText = data.total_passed;
        kpiDensity.innerText = data.current_metrics.density_level;
        
        // Density indicator styles
        kpiDensity.className = ""; // clear
        densityIconContainer.className = "kpi-icon"; // clear class
        
        if (data.current_metrics.density_level === 'LOW') {
            kpiDensity.classList.add('density-low');
            densityIconContainer.style.background = 'rgba(0, 242, 169, 0.1)';
            densityIconContainer.style.color = '#00f2a9';
            densityIconContainer.style.border = '1px solid rgba(0, 242, 169, 0.2)';
        } else if (data.current_metrics.density_level === 'MEDIUM') {
            kpiDensity.classList.add('density-medium');
            densityIconContainer.style.background = 'rgba(255, 159, 28, 0.1)';
            densityIconContainer.style.color = '#ff9f1c';
            densityIconContainer.style.border = '1px solid rgba(255, 159, 28, 0.2)';
        } else {
            kpiDensity.classList.add('density-high');
            densityIconContainer.style.background = 'rgba(255, 51, 102, 0.1)';
            densityIconContainer.style.color = '#ff3366';
            densityIconContainer.style.border = '1px solid rgba(255, 51, 102, 0.2)';
        }

        // Update active alerts count KPI
        kpiAlertsCount.innerText = data.active_alerts.length;

        // Update Doughnut Class chart
        classChart.data.datasets[0].data = [
            data.counts.car || 0,
            data.counts.bike || 0,
            data.counts.bus || 0,
            data.counts.truck || 0
        ];
        classChart.update('none');

        // Fetch and update line chart with historical values
        updateHistoricalChart();

    } catch (err) {
        statusDot.className = "pulse-dot";
        statusText.innerText = "DISCONNECTED";
        console.warn("Connection lost to API:", err);
    }
}

// Fetch historical records for line chart
async function updateHistoricalChart() {
    try {
        const response = await fetch('/api/stats/historical?limit=25');
        if (!response.ok) return;
        const data = await response.json();
        
        const labels = data.map(item => {
            const date = new Date(item.timestamp);
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        });
        const totals = data.map(item => item.total_passed);

        flowChart.data.labels = labels;
        flowChart.data.datasets[0].data = totals;
        flowChart.update('none');
    } catch (err) {
        console.error("Error drawing flow chart:", err);
    }
}

// Poll Alerts
async function updateAlerts() {
    try {
        const response = await fetch('/api/alerts?limit=15');
        if (!response.ok) return;
        const alerts = await response.json();

        // Count active alerts
        const activeCount = alerts.filter(a => a.is_active === 1).length;
        activeAlertsBadge.innerText = `${activeCount} Active`;

        if (alerts.length === 0) {
            alertList.innerHTML = `
                <li class="empty-alerts">
                    <i class="fa-solid fa-shield-halved"></i>
                    <p>No anomalies detected. Traffic flow is normal.</p>
                </li>
            `;
            return;
        }

        // Render alert list
        let listHTML = '';
        alerts.forEach(alert => {
            const timeStr = new Date(alert.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const itemClass = alert.type.toLowerCase();
            const resolvedClass = alert.is_active === 0 ? 'resolved' : '';
            
            const icon = alert.type === 'ACCIDENT' 
                ? '<i class="fa-solid fa-car-burst"></i>' 
                : '<i class="fa-solid fa-hand"></i>';

            // Forensic clip download element
            const downloadClipHTML = `
                <a href="/api/incidents/${alert.id}" download class="btn-download-clip">
                    <i class="fa-solid fa-download"></i> Clip
                </a>
            `;

            // Bounding box crop thumbnail
            const thumbnailHTML = alert.thumbnail
                ? `
                <div class="alert-thumbnail-container" onclick="showZoomModal('${alert.thumbnail}', '${alert.type.replace('_', ' ')}', '${alert.description}')">
                    <img src="${alert.thumbnail}" class="alert-thumbnail" alt="Forensic Crop">
                </div>
                `
                : '';

            listHTML += `
                <li class="alert-item ${itemClass} ${resolvedClass}">
                    ${thumbnailHTML}
                    <div class="alert-item-icon">
                        ${icon}
                    </div>
                    <div class="alert-item-details">
                        <div class="alert-meta">
                            <span class="alert-badge">${alert.type.replace('_', ' ')}</span>
                            <span class="alert-time">${timeStr}</span>
                        </div>
                        <p>${alert.description}</p>
                        <div style="margin-top: 6px; display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                ${downloadClipHTML}
                            </div>
                            <div>
                                ${alert.is_active === 0 
                                    ? '<span class="alert-resolved-text"><i class="fa-solid fa-check"></i> Resolved</span>' 
                                    : '<span class="alert-badge" style="color: var(--neon-red);"><i class="fa-solid fa-circle-exclamation"></i> ACTIVE</span>'}
                            </div>
                        </div>
                    </div>
                </li>
            `;

            // Sound beep/flash on fresh active alerts
            if (alert.is_active === 1 && !knownAlertIds.has(alert.id)) {
                knownAlertIds.add(alert.id);
                triggerVisualFlash();
            }
        });

        alertList.innerHTML = listHTML;

    } catch (err) {
        console.error("Error updating alerts:", err);
    }
}

// Make the dashboard border flash red briefly on new alert
function triggerVisualFlash() {
    const alertsCard = document.querySelector('.alerts-card');
    if (alertsCard) {
        alertsCard.style.boxShadow = '0 0 25px rgba(255, 51, 102, 0.6)';
        alertsCard.style.borderColor = 'rgba(255, 51, 102, 0.8)';
        
        setTimeout(() => {
            alertsCard.style.boxShadow = '';
            alertsCard.style.borderColor = '';
        }, 1500);
    }
}

// Submit configuration update
async function handleConfigSubmit(e) {
    e.preventDefault();
    
    if (linePoints.length !== 2 || roiPoints.length !== 4) {
        alert("Configuration vector nodes are missing.");
        return;
    }

    const payload = {
        line_p1: linePoints[0],
        line_p2: linePoints[1],
        roi_points: roiPoints,
        video_source: videoSourceInput.value.trim(),
        weather_mode: weatherModeSelect.value,
        heatmap_enabled: enableHeatmapCheckbox.checked
    };

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) throw new Error("Failed to update config");
        const res = await response.json();
        
        // Force-reload the video MJPEG stream to avoid browser-side freezing
        const videoFeed = document.getElementById('video-feed');
        if (videoFeed) {
            videoFeed.src = `/api/stream?t=${new Date().getTime()}`;
        }
        
        // Show transient success indication on button
        const applyBtn = document.getElementById('apply-config-btn');
        const origHTML = applyBtn.innerHTML;
        applyBtn.innerHTML = '<i class="fa-solid fa-circle-check"></i> Applied!';
        applyBtn.style.boxShadow = '0 0 15px var(--neon-green)';
        
        setTimeout(() => {
            applyBtn.innerHTML = origHTML;
            applyBtn.style.boxShadow = '';
        }, 2000);
        
    } catch (err) {
        alert("Error applying settings: " + err.message);
    }
}

// Reset stats handler
async function handleReset() {
    if (!confirm("Are you sure you want to clear cumulative traffic metrics and alerts?")) return;
    
    try {
        const response = await fetch('/api/reset', { method: 'POST' });
        if (!response.ok) throw new Error("Reset failed");
        
        // Clear local states
        knownAlertIds.clear();
        
        // Reset charts locally
        flowChart.data.labels = [];
        flowChart.data.datasets[0].data = [];
        flowChart.update();
        
        classChart.data.datasets[0].data = [0, 0, 0, 0];
        classChart.update();
        
        // Clear alert list UI
        alertList.innerHTML = `
            <li class="empty-alerts">
                <i class="fa-solid fa-shield-halved"></i>
                <p>No anomalies detected. Traffic flow is normal.</p>
            </li>
        `;
        
        kpiTotal.innerText = '0';
        kpiAlertsCount.innerText = '0';
        activeAlertsBadge.innerText = '0 Active';
        
        // Force-reload the video MJPEG stream
        const videoFeed = document.getElementById('video-feed');
        if (videoFeed) {
            videoFeed.src = `/api/stream?t=${new Date().getTime()}`;
        }

    } catch (err) {
        alert("Error resetting metrics: " + err.message);
    }
}

// Global hook to handle stream connection failures and retry
function handleStreamError(img) {
    console.warn("MJPEG video stream failed to load. Retrying in 2 seconds...");
    img.src = "https://images.unsplash.com/photo-1544620347-c4fd4a3d5957?q=80&w=1280&auto=format&fit=crop";
    
    setTimeout(() => {
        console.log("Retrying stream connection...");
        img.src = `/api/stream?t=${new Date().getTime()}`;
    }, 2000);
}

// ------------------ DYNAMIC POPUP ZOOM VIEW MODAL ------------------

function showZoomModal(src, title, desc) {
    let overlay = document.getElementById('zoom-modal-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'zoom-modal-overlay';
        overlay.className = 'thumbnail-modal-overlay';
        overlay.innerHTML = `
            <div class="thumbnail-modal-content">
                <span class="thumbnail-modal-close" onclick="closeZoomModal()">&times;</span>
                <img id="zoom-modal-img" src="" alt="Incident Zoom">
                <h3 id="zoom-modal-title" style="margin-bottom: 8px;"></h3>
                <p id="zoom-modal-desc" style="font-size: 0.85rem; color: #94a3b8;"></p>
            </div>
        `;
        document.body.appendChild(overlay);
        
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeZoomModal();
        });
    }
    
    document.getElementById('zoom-modal-img').src = src;
    document.getElementById('zoom-modal-title').innerText = title;
    document.getElementById('zoom-modal-desc').innerText = desc;
    overlay.classList.add('active');
}

function closeZoomModal() {
    const overlay = document.getElementById('zoom-modal-overlay');
    if (overlay) {
        overlay.classList.remove('active');
    }
}
