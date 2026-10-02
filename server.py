#!/usr/bin/env python3
"""Backend server for Video Upload Scheduler - Google Drive + YouTube integration."""

import json
import os
import threading
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Google APIs
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Import YouTube upload module
from youtube_upload import (
    check_youtube_channel,
    list_my_videos,
    upload_from_drive_to_youtube,
)
# Generic platform uploader (YouTube, Instagram, Facebook, TikTok)
from platform_upload import upload_to_platforms

# Configuration
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/scheduler")))
TOKEN_PATH = HERMES_HOME / "google_token.json"
DASHBOARD_DIR = Path(__file__).parent

# Video MIME types to filter
VIDEO_MIME_TYPES = [
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
    "video/mpeg",
    "video/3gpp",
    "video/x-flv",
]

# Track upload progress
upload_jobs = {}


def get_credentials():
    """Get valid credentials, refreshing if needed."""
    if not TOKEN_PATH.exists():
        raise Exception("Not authenticated. Run google_oauth.py --auth-url first.")
    
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data = json.loads(creds.to_json())
        token_data["type"] = "authorized_user"
        TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    
    return creds


def get_drive_service():
    """Get authenticated Google Drive service."""
    return build("drive", "v3", credentials=get_credentials())


def list_videos(folder_id=None, max_results=50):
    """List video files from Google Drive."""
    service = get_drive_service()
    
    # Build query for video files
    mime_queries = " or ".join([f"mimeType='{mt}'" for mt in VIDEO_MIME_TYPES])
    query = f"({mime_queries}) and trashed=false"
    
    if folder_id:
        query += f" and '{folder_id}' in parents"
    
    results = service.files().list(
        q=query,
        pageSize=max_results,
        fields="files(id, name, mimeType, size, createdTime, modifiedTime, thumbnailLink, webViewLink, webContentLink)",
        orderBy="modifiedTime desc"
    ).execute()
    
    files = results.get("files", [])
    
    # Format for dashboard
    videos = []
    for f in files:
        size_bytes = int(f.get("size", 0))
        size_mb = size_bytes / (1024 * 1024)
        
        videos.append({
            "id": f["id"],
            "title": f["name"],
            "mimeType": f["mimeType"],
            "size": f"{size_mb:.1f} MB" if size_mb > 0 else "Unknown",
            "sizeBytes": size_bytes,
            "createdTime": f.get("createdTime", ""),
            "modifiedTime": f.get("modifiedTime", ""),
            "thumbnail": f.get("thumbnailLink", ""),
            "webViewLink": f.get("webViewLink", ""),
            "downloadLink": f.get("webContentLink", ""),
            "status": "pending",
            "platforms": []
        })
    
    return videos


def list_folders():
    """List folders from Google Drive for navigation."""
    service = get_drive_service()
    
    results = service.files().list(
        q="mimeType='application/vnd.google-apps.folder' and trashed=false",
        pageSize=100,
        fields="files(id, name, modifiedTime)",
        orderBy="name"
    ).execute()
    
    return results.get("files", [])


def upload_video_async(job_id: str, drive_file_id: str, title: str, description: str, privacy: str, platforms=None):
    """Upload video(s) in background thread for specified platforms."""
    if platforms is None:
        platforms = ["youtube"]
    try:
        upload_jobs[job_id]["status"] = "downloading"
        upload_jobs[job_id]["message"] = "Downloading from Google Drive..."
        # Use generic uploader which will handle each platform
        result = upload_to_platforms(
            drive_file_id=drive_file_id,
            title=title,
            description=description,
            privacy=privacy,
            platforms=platforms,
        )
        upload_jobs[job_id]["status"] = "completed"
        upload_jobs[job_id]["result"] = result
        upload_jobs[job_id]["message"] = "Upload completed for platforms"
    except Exception as e:
        upload_jobs[job_id]["status"] = "failed"
        upload_jobs[job_id]["error"] = str(e)
        upload_jobs[job_id]["message"] = f"Upload failed: {str(e)}"
    # Deprecated YouTube-only upload logic removed


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler for the dashboard with API endpoints."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)
    
    def do_GET(self):
        parsed = urlparse(self.path)
        
        # API endpoints
        if parsed.path == "/api/videos":
            self.handle_api_videos(parsed)
        elif parsed.path == "/api/folders":
            self.handle_api_folders()
        elif parsed.path == "/api/status":
            self.handle_api_status()
        elif parsed.path == "/api/youtube/channel":
            self.handle_youtube_channel()
        elif parsed.path == "/api/youtube/videos":
            self.handle_youtube_videos()
        elif parsed.path == "/api/upload/status":
            self.handle_upload_status(parsed)
        elif parsed.path == "/api/schedules":
            self.handle_get_schedules()
        else:
            # Serve static files
            super().do_GET()
    
    def do_POST(self):
        parsed = urlparse(self.path)
        
        if parsed.path == "/api/schedule":
            self.handle_schedule()
        elif parsed.path == "/api/schedule/delete":
            self.handle_delete_schedule()
        elif parsed.path == "/api/schedule/upload":
            self.handle_upload_scheduled()
        elif parsed.path == "/api/youtube/upload":
            self.handle_youtube_upload()
        else:
            self.send_error(404)
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def send_json(self, data, status=200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def handle_api_videos(self, parsed):
        """Return list of videos from Google Drive."""
        try:
            params = parse_qs(parsed.query)
            folder_id = params.get("folder", [None])[0]
            
            videos = list_videos(folder_id)
            self.send_json({"success": True, "videos": videos})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_api_folders(self):
        """Return list of folders."""
        try:
            folders = list_folders()
            self.send_json({"success": True, "folders": folders})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_api_status(self):
        """Check Google Drive + YouTube connection status."""
        try:
            if not TOKEN_PATH.exists():
                self.send_json({"connected": False, "error": "Not authenticated"})
                return
            
            # Get Drive info
            service = get_drive_service()
            about = service.about().get(fields="user").execute()
            user = about.get("user", {})
            
            # Get YouTube info
            youtube_info = check_youtube_channel()
            
            self.send_json({
                "connected": True,
                "user": {
                    "name": user.get("displayName", "Unknown"),
                    "email": user.get("emailAddress", "")
                },
                "youtube": youtube_info
            })
        except Exception as e:
            self.send_json({"connected": False, "error": str(e)})
    
    def handle_youtube_channel(self):
        """Get YouTube channel info."""
        try:
            info = check_youtube_channel()
            self.send_json({"success": True, **info})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_youtube_videos(self):
        """List videos from YouTube channel."""
        try:
            videos = list_my_videos(max_results=20)
            self.send_json({"success": True, "videos": videos})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_youtube_upload(self):
        """Start upload (multiple platforms) from Google Drive."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            drive_file_id = data.get("driveFileId")
            title = data.get("title", "Untitled Video")
            description = data.get("description", "")
            privacy = data.get("privacy", "private")
            platforms = data.get("platforms", ["youtube"])
            
            if not drive_file_id:
                self.send_json({"success": False, "error": "Missing driveFileId"}, 400)
                return
            
            # Create job ID
            import time
            job_id = f"upload_{int(time.time() * 1000)}"
            
            upload_jobs[job_id] = {
                "status": "queued",
                "message": "Upload queued...",
                "driveFileId": drive_file_id,
                "title": title,
                "platforms": platforms,
            }
            
            # Start upload in background thread
            thread = threading.Thread(
                target=upload_video_async,
                args=(
                    job_id,
                    drive_file_id,
                    title,
                    description,
                    privacy,
                    platforms,
                ),
            )
            thread.daemon = True
            thread.start()
            
            self.send_json({
                "success": True,
                "jobId": job_id,
                "message": "Upload started in background"
            })
            
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_upload_status(self, parsed):
        """Get upload job status."""
        params = parse_qs(parsed.query)
        job_id = params.get("jobId", [None])[0]
        
        if not job_id or job_id not in upload_jobs:
            self.send_json({"success": False, "error": "Job not found"}, 404)
            return
        
        self.send_json({"success": True, **upload_jobs[job_id]})
    
    def handle_schedule(self):
        """Handle schedule creation request."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            schedule_file = DASHBOARD_DIR / "schedules.json"
            
            schedules = []
            if schedule_file.exists():
                schedules = json.loads(schedule_file.read_text())
            
            # Validate and enrich each new item
            for item in data.get("items", []):
                required = ["id", "videoId", "title", "scheduledTime", "platforms"]
                missing = [k for k in required if k not in item]
                if missing:
                    raise Exception(f"Schedule item missing required keys: {', '.join(missing)}")
                item.setdefault("description", "")
                item.setdefault("privacy", "private")
                item["status"] = "pending"
            
            schedules.extend(data.get("items", []))
            schedule_file.write_text(json.dumps(schedules, indent=2))
            
            self.send_json({
                "success": True,
                "message": f"Scheduled {len(data.get('items', []))} videos",
                "schedules": schedules
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_get_schedules(self):
        """Return list of scheduled uploads."""
        try:
            schedule_file = DASHBOARD_DIR / "schedules.json"
            schedules = []
            if schedule_file.exists():
                schedules = json.loads(schedule_file.read_text())
            self.send_json({"success": True, "schedules": schedules})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_delete_schedule(self):
        """Delete a scheduled upload."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            schedule_id = data.get("id")
            
            schedule_file = DASHBOARD_DIR / "schedules.json"
            schedules = []
            if schedule_file.exists():
                schedules = json.loads(schedule_file.read_text())
            
            schedules = [s for s in schedules if s.get("id") != schedule_id]
            schedule_file.write_text(json.dumps(schedules, indent=2))
            
            self.send_json({"success": True, "message": "Schedule deleted"})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_upload_scheduled(self):
        """Manually trigger upload for a scheduled item."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            schedule_id = data.get("id")
            
            schedule_file = DASHBOARD_DIR / "schedules.json"
            schedules = json.loads(schedule_file.read_text()) if schedule_file.exists() else []
            
            # Find the schedule
            schedule = next((s for s in schedules if s.get("id") == schedule_id), None)
            if not schedule:
                self.send_json({"success": False, "error": "Schedule not found"}, 404)
                return
            
            # Create upload job
            import time
            job_id = f"upload_{int(time.time() * 1000)}"
            
            upload_jobs[job_id] = {
                "status": "queued",
                "message": "Upload queued...",
                "driveFileId": schedule.get("videoId"),
                "title": schedule.get("title"),
            }
            
            # Update schedule status
            for s in schedules:
                if s.get("id") == schedule_id:
                    s["status"] = "uploading"
                    s["jobId"] = job_id
            schedule_file.write_text(json.dumps(schedules, indent=2))
            
            # Start upload in background
            # Start upload in background, passing platforms from schedule
            thread = threading.Thread(
                target=upload_video_async,
                args=(
                    job_id,
                    schedule.get("videoId"),
                    schedule.get("title"),
                    schedule.get("description", ""),
                    schedule.get("privacy", "private"),
                    schedule.get("platforms", ["youtube"]),
                ),
            )
            thread.daemon = True
            thread.start()
            
            self.send_json({
                "success": True,
                "jobId": job_id,
                "message": "Upload started"
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def log_message(self, format, *args):
        """Custom logging."""
        print(f"[Server] {args[0]}")


def schedule_checker():
    """Background thread that checks scheduled uploads and triggers them when due."""
    import time as _time
    import datetime as _dt
    schedule_file = DASHBOARD_DIR / "schedules.json"
    while True:
        if schedule_file.exists():
            try:
                schedules = json.loads(schedule_file.read_text())
            except Exception:
                schedules = []
            now = _dt.datetime.utcnow()
            updated = False
            for s in schedules:
                if s.get("status") == "pending":
                    sched_time_str = s.get("scheduledTime")
                    if not sched_time_str:
                        continue
                    try:
                        sched_time = _dt.datetime.fromisoformat(sched_time_str)
                    except Exception:
                        continue
                    if sched_time <= now:
                        # Trigger upload
                        job_id = f"upload_{int(_time.time() * 1000)}"
                        upload_jobs[job_id] = {
                            "status": "queued",
                            "message": "Upload queued via schedule...",
                            "driveFileId": s.get("videoId"),
                            "title": s.get("title"),
                        }
                        # Update schedule status
                        s["status"] = "uploading"
                        s["jobId"] = job_id
                        # Start upload thread
                        thread = threading.Thread(
                            target=upload_video_async,
                            args=(
                                job_id,
                                s.get("videoId"),
                                s.get("title"),
                                s.get("description", ""),
                                s.get("privacy", "private"),
                                s.get("platforms", ["youtube"]),
                            ),
                        )
                        thread.daemon = True
                        thread.start()
                        updated = True
            if updated:
                schedule_file.write_text(json.dumps(schedules, indent=2))
        _time.sleep(30)  # check every 30 seconds

def run_server(port=8765, host="0.0.0.0"):
    """Start the dashboard server.

    Use the secure handler (which includes authentication routes) instead of the
    basic DashboardHandler. This fixes the 404 errors seen when posting to
    `/api/auth/register` and related endpoints.
    """
    # Import the secure handler that implements /api/auth/* routes
    from server_secure import SecureDashboardHandler
    server = HTTPServer((host, port), SecureDashboardHandler)
    print(f"🚀 Video Scheduler Dashboard running at http://{host}:{port}")
    print(f"📁 Serving from: {DASHBOARD_DIR}")
    print(f"🎬 YouTube integration: enabled")
    print("Press Ctrl+C to stop")
    # Start background scheduler thread
    checker_thread = threading.Thread(target=schedule_checker, daemon=True)
    checker_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        server.shutdown()


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8765))
    run_server(port)
