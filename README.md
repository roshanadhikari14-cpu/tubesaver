# TubeSaver

A local web app for downloading YouTube videos. Paste a link and the thumbnail shows immediately. The app then lists every available resolution plus MP3, M4A and thumbnail downloads, and saves the file you pick to a folder on your drive (`D:\YouTube` by default).

Everything runs on your own computer. The page talks to a small Python server ([Flask](https://flask.palletsprojects.com/) + [yt-dlp](https://github.com/yt-dlp/yt-dlp)) on `127.0.0.1`.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/), which merges video and audio for 1080p and higher and converts to MP3. On Windows: `winget install Gyan.FFmpeg`

## Run

Double-click `start.bat`, or:

```bash
pip install -r requirements.txt
python app.py
```

Your browser opens at http://127.0.0.1:5055.

## Features

- Thumbnail, title and channel appear as soon as you paste the link, while the full details load
- Every available resolution (144p to 4K/8K), with fps, codec and estimated size
- MP3 / M4A audio and full-size thumbnail downloads
- Live progress with download speed
- Change the save folder from the page; it's created if it doesn't exist
- "Show file" opens Explorer with the downloaded file selected

## Updating

YouTube changes often. If fetching stops working, update yt-dlp:

```bash
pip install -U yt-dlp
```

Only download videos you own or have permission to save, and respect YouTube's Terms of Service.
