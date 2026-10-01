# Video Scheduler Dashboard

Upload videos from Google Drive to YouTube (TikTok/Instagram coming soon).

## Features
- Browse Google Drive videos
- Upload to YouTube with privacy settings
- Schedule uploads for later
- Track upload progress

## Setup

1. **Get Google OAuth credentials** from [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Set environment variables:
   ```
   GOOGLE_CLIENT_ID=your_client_id
   GOOGLE_CLIENT_SECRET=your_client_secret
   ```
3. Run the app:
   ```
   python server.py
   ```

## Environment Variables
- `GOOGLE_CLIENT_ID` - OAuth client ID
- `GOOGLE_CLIENT_SECRET` - OAuth client secret
- `PORT` - Server port (default: 8765)
