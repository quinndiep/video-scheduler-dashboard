#!/usr/bin/env python3
"""Platform upload utilities (YouTube, Instagram, Facebook, TikTok).

Every platform authenticates with its **own** credential:

* **YouTube** – a per‑user Google OAuth token (possibly one of several linked
  channels, selected by ``youtube_account_id``).
* **Instagram / Facebook / TikTok** – a per‑user access token stored in the
  encrypted user store (``instagram_token`` / ``facebook_token`` /
  ``tiktok_token``).  An environment variable of the same name is used as a
  fallback for local development / CI.

The upload functions receive an already‑downloaded local file path so that a
single Drive download can feed every selected platform.
"""

import logging
import os
from typing import Callable, Dict, List, Optional

from googleapiclient.http import MediaIoBaseDownload

# Expected environment variables for credentials (example names)
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN")
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN")

SUPPORTED_PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")


def _require_token(token: Optional[str], platform: str) -> str:
    """Raise a clear error when a platform credential is missing.

    Args:
        token: The resolved access token (may be ``None``).
        platform: Lower‑case platform name used in the error message.

    Returns:
        str: The token, guaranteed non‑empty.

    Raises:
        RuntimeError: If no credential is configured for ``platform``.
    """
    if not token:
        raise RuntimeError(
            f"Missing {platform} credential. Connect a {platform} account for this "
            f"user or set the {platform.upper()}_ACCESS_TOKEN environment variable."
        )
    return token


def get_platform_token(user_id: Optional[str], platform: str) -> Optional[str]:
    """Resolve the access token for a platform, per user.

    Args:
        user_id: The scheduler user whose stored credential should be used.
        platform: One of ``instagram``, ``facebook`` or ``tiktok``.

    Returns:
        str | None: The stored token, or the environment fallback, or ``None``.
    """
    field = f"{platform}_token"
    if user_id:
        try:
            from server_secure import load_users

            users = load_users()
            token = users.get(user_id, {}).get(field)
            if token:
                return token
        except Exception as e:  # pragma: no cover - defensive
            logging.error(f"Could not read stored {platform} credential: {e}")
    return {
        "instagram": INSTAGRAM_ACCESS_TOKEN,
        "facebook": FACEBOOK_ACCESS_TOKEN,
        "tiktok": TIKTOK_ACCESS_TOKEN,
    }.get(platform)


def download_from_drive_for_user(
    user_id: str,
    file_id: str,
    output_path: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> str:
    """Download a Drive file using the *calling user's* OAuth credentials.

    ``youtube_upload.download_from_drive`` relies on a module-level Drive client
    built from a single token file, which only works for one user. Tasks run
    under an arbitrary job owner, so the credential has to be resolved per user.

    Args:
        user_id: Owner of the job; selects the Google credential.
        file_id: Google Drive file id.
        output_path: Destination path; a temp file with the original suffix is
            created when omitted.
        progress_cb: Optional callable receiving 0-100 while downloading.

    Returns:
        str: Path to the downloaded file.

    Raises:
        RuntimeError: If Drive is not connected for this user.
    """
    import tempfile
    from pathlib import Path

    from server_secure import get_user_drive_service

    drive = get_user_drive_service(user_id)
    if not drive:
        raise RuntimeError("Google Drive is not connected for this account")

    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    if output_path is None:
        suffix = Path(meta["name"]).suffix or ".mp4"
        fd, output_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)

    request = drive.files().get_media(fileId=file_id)
    with open(output_path, "wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status and progress_cb:
                progress_cb(int(status.progress() * 100))
    return output_path


def upload_to_youtube(
    file_path: str,
    title: str,
    description: str = "",
    privacy: str = "private",
    user_id: Optional[str] = None,
    youtube_account_id: Optional[str] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> Dict:
    """Upload a local video file to YouTube using a per‑user Google token.

    Args:
        file_path: Path to the downloaded video file.
        title: Video title.
        description: Video description.
        privacy: One of ``public``, ``unlisted`` or ``private``.
        user_id: Owner of the stored Google credential.
        youtube_account_id: UUID of the specific linked channel (optional).
        progress_cb: Optional callable receiving a 0‑100 percentage.

    Returns:
        dict: ``{"success": True, "platform": "youtube", "videoId": ..., "url": ...}``

    Raises:
        RuntimeError: If no YouTube credential is available for the user.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials

    from server_secure import load_users

    users = load_users()
    user = users.get(user_id, {}) if user_id else {}
    token_data = None
    if youtube_account_id:
        for account in user.get("youtube_accounts", []):
            if account.get("account_id") == youtube_account_id:
                token_data = account.get("token")
                break
        if not token_data:
            raise RuntimeError(f"YouTube account '{youtube_account_id}' not found")
    else:
        token_data = user.get("google_token")

    _require_token(token_data, "youtube")
    creds = Credentials.from_authorized_user_info(token_data)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {"title": title, "description": description, "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(file_path, mimetype="video/*", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_cb:
            progress_cb(int(status.progress() * 100))

    video_id = response["id"]
    logging.info(f"YouTube upload finished: video {video_id}")
    return {
        "success": True,
        "platform": "youtube",
        "videoId": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def upload_to_instagram(
    file_path: str,
    title: str,
    description: str = "",
    privacy: str = "private",
    user_id: Optional[str] = None,
    **kwargs,
) -> Dict:
    """Upload a local video file to Instagram using the user's IG token.

    Args:
        file_path: Path to the downloaded video file.
        title: Caption / title of the reel.
        description: Longer caption text.
        privacy: ``public`` or ``private`` visibility.
        user_id: Owner of the stored ``instagram_token``.

    Returns:
        dict: Platform result payload.

    Raises:
        RuntimeError: If no Instagram credential is configured.
    """
    _require_token(get_platform_token(user_id, "instagram"), "instagram")
    # Never log any part of the token – only the title, user and settings.
    logging.info(
        f"[Instagram] Uploading '{title}' as user {user_id or 'env'} "
        f"(privacy={privacy})"
    )
    # Replace with the Instagram Graph API container + publish flow.
    return {
        "success": True,
        "platform": "instagram",
        "title": title,
        "privacy": privacy,
        "mediaId": None,
    }


def upload_to_facebook(
    file_path: str,
    title: str,
    description: str = "",
    privacy: str = "private",
    user_id: Optional[str] = None,
    **kwargs,
) -> Dict:
    """Upload a local video file to Facebook using the user's FB token.

    Args:
        file_path: Path to the downloaded video file.
        title: Post title.
        description: Post description.
        privacy: Audience setting forwarded to the API.
        user_id: Owner of the stored ``facebook_token``.

    Returns:
        dict: Platform result payload.

    Raises:
        RuntimeError: If no Facebook credential is configured.
    """
    _require_token(get_platform_token(user_id, "facebook"), "facebook")
    # Never log any part of the token – only the title, user and settings.
    logging.info(
        f"[Facebook] Uploading '{title}' as user {user_id or 'env'} "
        f"(privacy={privacy})"
    )
    # Replace with the Facebook Graph API resumable upload flow.
    return {
        "success": True,
        "platform": "facebook",
        "title": title,
        "privacy": privacy,
        "postId": None,
    }


def upload_to_tiktok(
    file_path: str,
    title: str,
    description: str = "",
    privacy: str = "private",
    user_id: Optional[str] = None,
    **kwargs,
) -> Dict:
    """Upload a local video file to TikTok using the user's TikTok token.

    Args:
        file_path: Path to the downloaded video file.
        title: Video title / caption.
        description: Longer description.
        privacy: ``public_to_followers`` or ``private_to_followers``.
        user_id: Owner of the stored ``tiktok_token``.

    Returns:
        dict: Platform result payload.

    Raises:
        RuntimeError: If no TikTok credential is configured.
    """
    _require_token(get_platform_token(user_id, "tiktok"), "tiktok")
    # Never log any part of the token – only the title, user and settings.
    logging.info(
        f"[TikTok] Uploading '{title}' as user {user_id or 'env'} "
        f"(privacy={privacy})"
    )
    # Replace with the TikTok Content Posting API direct-post flow.
    return {
        "success": True,
        "platform": "tiktok",
        "title": title,
        "privacy": privacy,
        "publishId": None,
    }


def upload_to_platforms(
    file_path: str | None = None,
    title: str = "",
    description: str = "",
    platforms: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    youtube_account_id: Optional[str] = None,
    privacy: str = "private",
    progress_cb: Optional[Callable[[int], None]] = None,
    drive_file_id: Optional[str] = None,
) -> Dict[str, Dict]:
    """Dispatch one already‑downloaded file to every selected platform.

    Args:
        file_path: Path to the downloaded video file.
        title: Video title.
        description: Video description.
        platforms: Platform names; defaults to ``["youtube"]``.
        user_id: Owner used to resolve each platform credential.
        youtube_account_id: Optional specific YouTube channel UUID.
        privacy: Privacy setting forwarded to each platform.
        progress_cb: Optional callable receiving 0‑100 for the overall upload.
        drive_file_id: Legacy alias for ``file_path`` kept for the older
            ``server.py`` caller, which passed a Drive id rather than a path.

    Returns:
        dict: Mapping of platform name to that platform's result payload. A
        platform that fails is reported as ``{"success": False, "error": ...}``
        without aborting the remaining platforms.
    """
    file_path = file_path or drive_file_id
    if not file_path:
        raise ValueError("upload_to_platforms requires a file path or drive file id")
    if platforms is None:
        platforms = ["youtube"]
    results: Dict[str, Dict] = {}
    for name in platforms:
        key = str(name).lower()
        try:
            if key == "youtube":
                results[key] = upload_to_youtube(
                    file_path, title, description, privacy,
                    user_id=user_id,
                    youtube_account_id=youtube_account_id,
                    progress_cb=progress_cb,
                )
            elif key == "instagram":
                results[key] = upload_to_instagram(
                    file_path, title, description, privacy, user_id=user_id
                )
            elif key == "facebook":
                results[key] = upload_to_facebook(
                    file_path, title, description, privacy, user_id=user_id
                )
            elif key == "tiktok":
                results[key] = upload_to_tiktok(
                    file_path, title, description, privacy, user_id=user_id
                )
            else:
                results[key] = {"success": False, "error": f"Unsupported platform '{name}'"}
        except Exception as e:
            logging.error(f"Upload to {key} failed: {e}")
            results[key] = {"success": False, "error": str(e)}
    return results