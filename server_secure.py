#!/usr/bin/env python3
"""Secure multi-user Video Scheduler with per-user Google credentials."""

import json
import os
import secrets
import hashlib
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse, urlencode
from base64 import b64encode, b64decode

# Google APIs
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Configuration
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

# Session expiry (24 hours)
SESSION_EXPIRY_HOURS = 24

# Google OAuth scopes
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# Track upload jobs per user
upload_jobs = {}

# Track OAuth flows (temporary, in-memory)
oauth_flows = {}

# Video MIME types
VIDEO_MIME_TYPES = [
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm",
    "video/x-matroska", "video/mpeg", "video/3gpp", "video/x-flv",
]


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash password with salt."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return b64encode(hashed).decode(), salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verify password against hash."""
    new_hash, _ = hash_password(password, salt)
    return new_hash == hashed


def load_users() -> dict:
    """Load users database."""
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}


def save_users(users: dict):
    """Save users database."""
    USERS_FILE.write_text(json.dumps(users, indent=2))


def load_sessions() -> dict:
    """Load sessions database."""
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text())
    return {}


def save_sessions(sessions: dict):
    """Save sessions database."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def create_session(user_id: str) -> str:
    """Create a new session for user."""
    sessions = load_sessions()
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {
        "user_id": user_id,
        "created": datetime.now().isoformat(),
        "expires": (datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)).isoformat()
    }
    save_sessions(sessions)
    return session_id


def get_session_user(session_id: str) -> str | None:
    """Get user ID from session, returns None if invalid/expired."""
    if not session_id:
        return None
    sessions = load_sessions()
    session = sessions.get(session_id)
    if not session:
        return None
    if datetime.fromisoformat(session["expires"]) < datetime.now():
        del sessions[session_id]
        save_sessions(sessions)
        return None
    return session["user_id"]


def delete_session(session_id: str):
    """Delete a session."""
    sessions = load_sessions()
    if session_id in sessions:
        del sessions[session_id]
        save_sessions(sessions)


def get_user_credentials(user_id: str) -> Credentials | None:
    """Get Google credentials for user."""
    users = load_users()
    user = users.get(user_id, {})
    token_data = user.get("google_token")
    if not token_data:
        return None
    
    creds = Credentials.from_authorized_user_info(token_data)
    
    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            user["google_token"] = json.loads(creds.to_json())
            save_users(users)
        except Exception as e:
            print(f"Failed to refresh token: {e}")
            return None
    
    return creds


def get_user_drive_service(user_id: str):
    """Get Drive service for user."""
    creds = get_user_credentials(user_id)
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)


def get_user_youtube_service(user_id: str):
    """Get YouTube service for user."""
    creds = get_user_credentials(user_id)
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds)


class SecureDashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler with authentication."""
    
    def __init__(self, *args, **kwargs):
        self.directory = str(Path(__file__).parent)
        super().__init__(*args, directory=self.directory, **kwargs)
    
    def get_session_id(self) -> str | None:
        """Extract session ID from cookie."""
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        if "session" in cookie:
            return cookie["session"].value
        return None
    
    def get_current_user(self) -> tuple[str | None, dict | None]:
        """Get current user from session."""
        session_id = self.get_session_id()
        user_id = get_session_user(session_id)
        if not user_id:
            return None, None
        users = load_users()
        return user_id, users.get(user_id)
    
    def send_json(self, data: dict, status: int = 200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def set_session_cookie(self, session_id: str):
        """Set session cookie."""
        self.send_header("Set-Cookie", f"session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_EXPIRY_HOURS * 3600}")
    
    def clear_session_cookie(self):
        """Clear session cookie."""
        self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; Max-Age=0")
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        
        # Public endpoints
        if parsed.path == "/":
            self.serve_index()
        elif parsed.path == "/api/auth/status":
            self.handle_auth_status()
        elif parsed.path == "/oauth/callback":
            self.handle_oauth_callback(parsed)
        # Protected endpoints
        elif parsed.path == "/api/status":
            self.handle_status()
        elif parsed.path == "/api/videos":
            self.handle_videos(parsed)
        elif parsed.path == "/api/folders":
            self.handle_folders()
        elif parsed.path == "/api/youtube/channel":
            self.handle_youtube_channel()
        elif parsed.path == "/api/schedules":
            self.handle_get_schedules()
        elif parsed.path == "/api/upload/status":
            self.handle_upload_status(parsed)
        else:
            super().do_GET()
    
    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        
        if parsed.path == "/api/auth/register":
            self.handle_register()
        elif parsed.path == "/api/auth/login":
            self.handle_login()
        elif parsed.path == "/api/auth/logout":
            self.handle_logout()
        elif parsed.path == "/api/auth/setup-google":
            self.handle_setup_google()
        elif parsed.path == "/api/auth/start-oauth":
            self.handle_start_oauth()
        elif parsed.path == "/api/youtube/upload":
            self.handle_youtube_upload()
        elif parsed.path == "/api/schedule":
            self.handle_schedule()
        elif parsed.path == "/api/schedule/delete":
            self.handle_delete_schedule()
        elif parsed.path == "/api/schedule/upload":
            self.handle_upload_scheduled()
        else:
            self.send_json({"error": "Not found"}, 404)
    
    def serve_index(self):
        """Serve index.html or login page based on auth status."""
        user_id, user = self.get_current_user()
        
        # Serve the appropriate page
        if user_id and user and user.get("google_token"):
            # User is logged in and has Google connected
            self.path = "/index.html"
        else:
            # Show login/setup page
            self.path = "/login.html"
        
        super().do_GET()
    
    def handle_auth_status(self):
        """Return current auth status."""
        user_id, user = self.get_current_user()
        
        if not user_id:
            self.send_json({
                "authenticated": False,
                "google_connected": False
            })
            return
        
        google_connected = bool(user and user.get("google_token"))
        has_credentials = bool(user and user.get("google_client_id"))
        
        self.send_json({
            "authenticated": True,
            "user_id": user_id,
            "email": user.get("email") if user else None,
            "google_connected": google_connected,
            "has_credentials": has_credentials
        })
    
    def handle_register(self):
        """Register new user."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        
        if not email or not password:
            self.send_json({"success": False, "error": "Email and password required"}, 400)
            return
        
        if len(password) < 8:
            self.send_json({"success": False, "error": "Password must be at least 8 characters"}, 400)
            return
        
        users = load_users()
        
        if email in users:
            self.send_json({"success": False, "error": "Email already registered"}, 400)
            return
        
        hashed, salt = hash_password(password)
        users[email] = {
            "email": email,
            "password_hash": hashed,
            "password_salt": salt,
            "created": datetime.now().isoformat()
        }
        save_users(users)
        
        # Auto-login
        session_id = create_session(email)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_session_cookie(session_id)
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "Registration successful"}).encode())
    
    def handle_login(self):
        """Login user."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        
        users = load_users()
        user = users.get(email)
        
        if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
            self.send_json({"success": False, "error": "Invalid email or password"}, 401)
            return
        
        session_id = create_session(email)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_session_cookie(session_id)
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "Login successful"}).encode())
    
    def handle_logout(self):
        """Logout user."""
        session_id = self.get_session_id()
        if session_id:
            delete_session(session_id)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.clear_session_cookie()
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode())
    
    def handle_setup_google(self):
        """Save user's Google OAuth credentials."""
        user_id, user = self.get_current_user()
        if not user_id:
            self.send_json({"success": False, "error": "Not authenticated"}, 401)
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        client_id = body.get("client_id", "").strip()
        client_secret = body.get("client_secret", "").strip()
        
        if not client_id or not client_secret:
            self.send_json({"success": False, "error": "Client ID and secret required"}, 400)
            return
        
        users = load_users()
        users[user_id]["google_client_id"] = client_id
        users[user_id]["google_client_secret"] = client_secret
        save_users(users)
        
        self.send_json({"success": True, "message": "Google credentials saved"})
    
    def handle_start_oauth(self):
        """Start OAuth flow - returns auth URL."""
        user_id, user = self.get_current_user()
        if not user_id:
            self.send_json({"success": False, "error": "Not authenticated"}, 401)
            return
        
        if not user.get("google_client_id"):
            self.send_json({"success": False, "error": "Google credentials not configured"}, 400)
            return
        
        # Get the host from request
        host = self.headers.get("Host", "localhost:8765")
        protocol = "https" if "railway" in host or "render" in host or "herokuapp" in host else "http"
        redirect_uri = f"{protocol}://{host}/oauth/callback"
        
        # Create OAuth flow
        client_config = {
            "web": {
                "client_id": user["google_client_id"],
                "client_secret": user["google_client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        }
        
        flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
        
        # Generate state token (includes user_id for callback)
        state = b64encode(json.dumps({"user_id": user_id, "nonce": secrets.token_urlsafe(16)}).encode()).decode()
        
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state
        )
        
        # Store flow for callback (keyed by state)
        oauth_flows[state] = {
            "flow": flow,
            "user_id": user_id,
            "redirect_uri": redirect_uri,
            "client_config": client_config
        }
        
        self.send_json({"success": True, "auth_url": auth_url})
    
    def handle_oauth_callback(self, parsed):
        """Handle OAuth callback from Google."""
        params = parse_qs(parsed.query)
        
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        
        if error:
            self.send_response(302)
            self.send_header("Location", f"/?error={error}")
            self.end_headers()
            return
        
        if not code or not state:
            self.send_response(302)
            self.send_header("Location", "/?error=missing_params")
            self.end_headers()
            return
        
        try:
            # Get stored flow
            flow_data = oauth_flows.get(state)
            
            if flow_data:
                # Use the stored flow (has code_verifier)
                flow = flow_data["flow"]
                user_id = flow_data["user_id"]
                
                # Clean up stored flow
                del oauth_flows[state]
            else:
                # Fallback: decode state and create new flow (may fail with PKCE)
                state_data = json.loads(b64decode(state).decode())
                user_id = state_data.get("user_id")
                
                users = load_users()
                user = users.get(user_id)
                
                if not user:
                    raise Exception("User not found")
                
                # Get redirect URI
                host = self.headers.get("Host", "localhost:8765")
                protocol = "https" if "railway" in host or "render" in host or "herokuapp" in host else "http"
                redirect_uri = f"{protocol}://{host}/oauth/callback"
                
                # Create new flow
                client_config = {
                    "web": {
                        "client_id": user["google_client_id"],
                        "client_secret": user["google_client_secret"],
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [redirect_uri]
                    }
                }
                
                flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
            
            # Exchange code for tokens
            flow.fetch_token(code=code)
            
            creds = flow.credentials
            
            # Save token
            users = load_users()
            users[user_id]["google_token"] = json.loads(creds.to_json())
            save_users(users)
            
            # Create new session and redirect
            session_id = create_session(user_id)
            
            self.send_response(302)
            self.set_session_cookie(session_id)
            self.send_header("Location", "/?connected=true")
            self.end_headers()
            
        except Exception as e:
            print(f"OAuth callback error: {e}")
            self.send_response(302)
            self.send_header("Location", f"/?error=oauth_failed")
            self.end_headers()
    
    def require_auth(self) -> tuple[str | None, dict | None]:
        """Check auth and return user or send 401."""
        user_id, user = self.get_current_user()
        if not user_id or not user:
            self.send_json({"success": False, "error": "Not authenticated"}, 401)
            return None, None
        if not user.get("google_token"):
            self.send_json({"success": False, "error": "Google not connected"}, 401)
            return None, None
        return user_id, user
    
    def handle_status(self):
        """Get connection status."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            # Get user info from Google
            creds = get_user_credentials(user_id)
            service = build("oauth2", "v2", credentials=creds)
            user_info = service.userinfo().get().execute()
            
            # Check YouTube channel
            youtube = get_user_youtube_service(user_id)
            channel_response = youtube.channels().list(part="snippet", mine=True).execute()
            channel = channel_response.get("items", [{}])[0].get("snippet", {}) if channel_response.get("items") else None
            
            self.send_json({
                "success": True,
                "connected": True,
                "user": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
                "youtube_channel": channel.get("title") if channel else None
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_folders(self):
        """List Drive folders."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            service = get_user_drive_service(user_id)
            results = service.files().list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                pageSize=50,
                fields="files(id, name)"
            ).execute()
            
            self.send_json({
                "success": True,
                "folders": results.get("files", [])
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_videos(self, parsed):
        """List videos from Drive."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            params = parse_qs(parsed.query)
            folder_id = params.get("folder", [None])[0]
            
            service = get_user_drive_service(user_id)
            
            mime_query = " or ".join([f"mimeType='{m}'" for m in VIDEO_MIME_TYPES])
            query = f"({mime_query}) and trashed=false"
            if folder_id:
                query += f" and '{folder_id}' in parents"
            
            results = service.files().list(
                q=query,
                pageSize=100,
                fields="files(id, name, size, mimeType, thumbnailLink, webViewLink, createdTime)"
            ).execute()
            
            videos = []
            for f in results.get("files", []):
                videos.append({
                    "id": f["id"],
                    "title": f["name"],
                    "size": self.format_size(int(f.get("size", 0))),
                    "sizeBytes": int(f.get("size", 0)),
                    "mimeType": f.get("mimeType"),
                    "thumbnail": f.get("thumbnailLink"),
                    "webViewLink": f.get("webViewLink"),
                    "downloadLink": f"https://drive.google.com/uc?id={f['id']}&export=download",
                    "createdTime": f.get("createdTime")
                })
            
            self.send_json({"success": True, "videos": videos})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    
    def handle_youtube_channel(self):
        """Get YouTube channel info."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            youtube = get_user_youtube_service(user_id)
            response = youtube.channels().list(
                part="snippet,statistics",
                mine=True
            ).execute()
            
            if not response.get("items"):
                self.send_json({"success": False, "error": "No YouTube channel found"})
                return
            
            channel = response["items"][0]
            self.send_json({
                "success": True,
                "channel": {
                    "id": channel["id"],
                    "title": channel["snippet"]["title"],
                    "thumbnail": channel["snippet"]["thumbnails"]["default"]["url"],
                    "subscriberCount": channel["statistics"].get("subscriberCount", 0)
                }
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_youtube_upload(self):
        """Upload video to YouTube."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        drive_file_id = body.get("driveFileId")
        title = body.get("title", "Untitled")
        description = body.get("description", "")
        privacy = body.get("privacy", "private")
        
        if not drive_file_id:
            self.send_json({"success": False, "error": "Drive file ID required"}, 400)
            return
        
        # Create job
        job_id = f"upload_{secrets.token_hex(8)}"
        upload_jobs[job_id] = {
            "status": "queued",
            "message": "Starting upload...",
            "user_id": user_id
        }
        
        # Start upload in background
        thread = threading.Thread(
            target=self.upload_video_async,
            args=(job_id, user_id, drive_file_id, title, description, privacy)
        )
        thread.daemon = True
        thread.start()
        
        self.send_json({"success": True, "jobId": job_id})
    
    def upload_video_async(self, job_id: str, user_id: str, drive_file_id: str, title: str, description: str, privacy: str):
        """Background upload task."""
        try:
            upload_jobs[job_id]["status"] = "downloading"
            upload_jobs[job_id]["message"] = "Downloading from Google Drive..."
            
            # Download from Drive
            drive = get_user_drive_service(user_id)
            file_meta = drive.files().get(fileId=drive_file_id, fields="name,mimeType").execute()
            
            request = drive.files().get_media(fileId=drive_file_id)
            
            ext = Path(file_meta["name"]).suffix or ".mp4"
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                downloader = MediaIoBaseDownload(tmp, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        upload_jobs[job_id]["progress"] = int(status.progress() * 50)
                temp_path = tmp.name
            
            upload_jobs[job_id]["status"] = "uploading"
            upload_jobs[job_id]["message"] = "Uploading to YouTube..."
            
            # Upload to YouTube
            youtube = get_user_youtube_service(user_id)
            
            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": "22"
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": False
                }
            }
            
            media = MediaFileUpload(temp_path, mimetype="video/*", resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    upload_jobs[job_id]["progress"] = 50 + int(status.progress() * 50)
            
            # Cleanup
            os.unlink(temp_path)
            
            video_id = response["id"]
            upload_jobs[job_id] = {
                "status": "completed",
                "message": "Upload complete!",
                "result": {
                    "videoId": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}"
                }
            }
            
        except Exception as e:
            upload_jobs[job_id] = {
                "status": "failed",
                "message": str(e),
                "error": str(e)
            }
    
    def handle_upload_status(self, parsed):
        """Get upload job status."""
        params = parse_qs(parsed.query)
        job_id = params.get("jobId", [None])[0]
        
        if not job_id or job_id not in upload_jobs:
            self.send_json({"success": False, "error": "Job not found"}, 404)
            return
        
        self.send_json({"success": True, **upload_jobs[job_id]})
    
    def handle_get_schedules(self):
        """Get user's scheduled uploads."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        schedule_file = DATA_DIR / f"schedules_{user_id.replace('@', '_at_')}.json"
        schedules = []
        if schedule_file.exists():
            schedules = json.loads(schedule_file.read_text())
        
        self.send_json({"success": True, "schedules": schedules})
    
    def handle_schedule(self):
        """Create scheduled upload."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        schedule_file = DATA_DIR / f"schedules_{user_id.replace('@', '_at_')}.json"
        schedules = []
        if schedule_file.exists():
            schedules = json.loads(schedule_file.read_text())
        
        for item in body.get("items", []):
            item["status"] = "pending"
        
        schedules.extend(body.get("items", []))
        schedule_file.write_text(json.dumps(schedules, indent=2))
        
        self.send_json({"success": True, "message": f"Scheduled {len(body.get('items', []))} videos"})
    
    def handle_delete_schedule(self):
        """Delete scheduled upload."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        schedule_id = body.get("id")
        
        schedule_file = DATA_DIR / f"schedules_{user_id.replace('@', '_at_')}.json"
        schedules = []
        if schedule_file.exists():
            schedules = json.loads(schedule_file.read_text())
        
        schedules = [s for s in schedules if s.get("id") != schedule_id]
        schedule_file.write_text(json.dumps(schedules, indent=2))
        
        self.send_json({"success": True, "message": "Schedule deleted"})
    
    def handle_upload_scheduled(self):
        """Trigger upload for scheduled item."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        schedule_id = body.get("id")
        
        schedule_file = DATA_DIR / f"schedules_{user_id.replace('@', '_at_')}.json"
        schedules = json.loads(schedule_file.read_text()) if schedule_file.exists() else []
        
        schedule = next((s for s in schedules if s.get("id") == schedule_id), None)
        if not schedule:
            self.send_json({"success": False, "error": "Schedule not found"}, 404)
            return
        
        # Create upload job
        job_id = f"upload_{secrets.token_hex(8)}"
        upload_jobs[job_id] = {
            "status": "queued",
            "message": "Upload queued...",
            "user_id": user_id
        }
        
        # Update schedule status
        for s in schedules:
            if s.get("id") == schedule_id:
                s["status"] = "uploading"
                s["jobId"] = job_id
        schedule_file.write_text(json.dumps(schedules, indent=2))
        
        # Start upload
        thread = threading.Thread(
            target=self.upload_video_async,
            args=(job_id, user_id, schedule.get("videoId"), schedule.get("title"),
                  schedule.get("description", ""), schedule.get("privacy", "private"))
        )
        thread.daemon = True
        thread.start()
        
        self.send_json({"success": True, "jobId": job_id})
    
    def log_message(self, format, *args):
        """Custom logging."""
        print(f"[Server] {args[0]}")


def run_server(port=8765, host="0.0.0.0"):
    """Start the secure dashboard server."""
    server = HTTPServer((host, port), SecureDashboardHandler)
    print(f"🔐 Secure Video Scheduler Dashboard")
    print(f"🚀 Running at http://{host}:{port}")
    print(f"📁 Data directory: {DATA_DIR}")
    print("Press Ctrl+C to stop")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        server.shutdown()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    run_server(port)
