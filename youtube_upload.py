#!/usr/bin/env python3
"""YouTube upload functionality for Video Scheduler."""

import json
import os
import io
import tempfile
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# Configuration
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/scheduler")))
TOKEN_PATH = HERMES_HOME / "google_token.json"


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


def get_youtube_service():
    """Get authenticated YouTube service."""
    creds = get_credentials()
    return build("youtube", "v3", credentials=creds)


def get_drive_service():
    """Get authenticated Drive service."""
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


def check_youtube_channel():
    """Check if user has a YouTube channel and return info."""
    try:
        youtube = get_youtube_service()
        response = youtube.channels().list(
            part="snippet,contentDetails,statistics",
            mine=True
        ).execute()
        
        if not response.get("items"):
            return {"hasChannel": False, "error": "No YouTube channel found for this account"}
        
        channel = response["items"][0]
        return {
            "hasChannel": True,
            "channelId": channel["id"],
            "title": channel["snippet"]["title"],
            "description": channel["snippet"].get("description", ""),
            "thumbnail": channel["snippet"]["thumbnails"]["default"]["url"],
            "subscriberCount": channel["statistics"].get("subscriberCount", "0"),
            "videoCount": channel["statistics"].get("videoCount", "0"),
        }
    except Exception as e:
        return {"hasChannel": False, "error": str(e)}


def download_from_drive(file_id: str, output_path: str = None) -> str:
    """Download a file from Google Drive to local temp storage."""
    drive = get_drive_service()
    
    # Get file metadata
    file_meta = drive.files().get(fileId=file_id, fields="name,mimeType,size").execute()
    
    if output_path is None:
        # Create temp file with original extension
        ext = Path(file_meta["name"]).suffix or ".mp4"
        fd, output_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
    
    # Download file
    request = drive.files().get_media(fileId=file_id)
    
    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Download progress: {int(status.progress() * 100)}%")
    
    print(f"Downloaded to: {output_path}")
    return output_path


def upload_to_youtube(
    video_path: str,
    title: str,
    description: str = "",
    tags: list = None,
    category_id: str = "22",  # People & Blogs
    privacy_status: str = "private",  # private, public, unlisted
    notify_subscribers: bool = False,
) -> dict:
    """
    Upload a video to YouTube.
    
    Args:
        video_path: Local path to the video file
        title: Video title (max 100 chars)
        description: Video description (max 5000 chars)
        tags: List of tags
        category_id: YouTube category ID (22 = People & Blogs)
        privacy_status: private, public, or unlisted
        notify_subscribers: Whether to notify subscribers (only for public)
    
    Returns:
        dict with video ID and URL
    """
    youtube = get_youtube_service()
    
    # Prepare video metadata
    body = {
        "snippet": {
            "title": title[:100],  # YouTube max title length
            "description": description[:5000],  # YouTube max description length
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    
    # Only set notifySubscribers for public videos
    if privacy_status == "public":
        body["status"]["notifySubscribers"] = notify_subscribers
    
    # Create media upload
    media = MediaFileUpload(
        video_path,
        mimetype="video/*",
        resumable=True,
        chunksize=1024 * 1024 * 10,  # 10MB chunks
    )
    
    # Execute upload
    print(f"Uploading video: {title}")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")
    
    video_id = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    
    print(f"Upload complete! Video ID: {video_id}")
    print(f"Video URL: {video_url}")
    
    return {
        "success": True,
        "videoId": video_id,
        "url": video_url,
        "title": response["snippet"]["title"],
        "privacyStatus": response["status"]["privacyStatus"],
    }


def upload_from_drive_to_youtube(
    drive_file_id: str,
    title: str,
    description: str = "",
    tags: list = None,
    privacy_status: str = "private",
) -> dict:
    """
    Download a video from Google Drive and upload it to YouTube.
    
    Args:
        drive_file_id: Google Drive file ID
        title: Video title
        description: Video description
        tags: List of tags
        privacy_status: private, public, or unlisted
    
    Returns:
        dict with video ID and URL
    """
    temp_path = None
    try:
        # Download from Drive
        print(f"Downloading video from Google Drive...")
        temp_path = download_from_drive(drive_file_id)
        
        # Upload to YouTube
        result = upload_to_youtube(
            video_path=temp_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
        )
        
        return result
        
    finally:
        # Clean up temp file
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            print(f"Cleaned up temp file: {temp_path}")


def list_my_videos(max_results: int = 10) -> list:
    """List videos from the authenticated user's channel."""
    youtube = get_youtube_service()
    
    # Get uploads playlist ID
    channels_response = youtube.channels().list(
        part="contentDetails",
        mine=True
    ).execute()
    
    if not channels_response.get("items"):
        return []
    
    uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    
    # Get videos from uploads playlist
    videos_response = youtube.playlistItems().list(
        part="snippet,status",
        playlistId=uploads_playlist_id,
        maxResults=max_results
    ).execute()
    
    videos = []
    for item in videos_response.get("items", []):
        snippet = item["snippet"]
        videos.append({
            "videoId": snippet["resourceId"]["videoId"],
            "title": snippet["title"],
            "description": snippet.get("description", "")[:200],
            "thumbnail": snippet["thumbnails"]["default"]["url"],
            "publishedAt": snippet["publishedAt"],
            "url": f"https://www.youtube.com/watch?v={snippet['resourceId']['videoId']}",
        })
    
    return videos


# CLI interface
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="YouTube upload tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Check channel command
    check_parser = subparsers.add_parser("check", help="Check YouTube channel status")
    
    # List videos command
    list_parser = subparsers.add_parser("list", help="List my uploaded videos")
    list_parser.add_argument("--max", type=int, default=10, help="Max videos to list")
    
    # Upload from Drive command
    upload_parser = subparsers.add_parser("upload", help="Upload video from Drive to YouTube")
    upload_parser.add_argument("drive_id", help="Google Drive file ID")
    upload_parser.add_argument("--title", required=True, help="Video title")
    upload_parser.add_argument("--description", default="", help="Video description")
    upload_parser.add_argument("--tags", nargs="*", help="Video tags")
    upload_parser.add_argument("--privacy", choices=["private", "public", "unlisted"], 
                               default="private", help="Privacy status")
    
    args = parser.parse_args()
    
    if args.command == "check":
        result = check_youtube_channel()
        print(json.dumps(result, indent=2))
        
    elif args.command == "list":
        videos = list_my_videos(args.max)
        print(json.dumps(videos, indent=2))
        
    elif args.command == "upload":
        result = upload_from_drive_to_youtube(
            drive_file_id=args.drive_id,
            title=args.title,
            description=args.description,
            tags=args.tags,
            privacy_status=args.privacy,
        )
        print(json.dumps(result, indent=2))
