import os
import uuid
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

app = FastAPI(title="TrendPilot FFmpeg Render Server")

BASE_DIR = Path("/app")
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])

    return result


def download_file(url, path):
    if not url or not str(url).startswith(("http://", "https://")):
        raise ValueError(f"Invalid URL: {url}")

    response = requests.get(
        url,
        timeout=120,
        stream=True,
        headers={"User-Agent": "TrendPilot-Render/1.0"}
    )
    response.raise_for_status()

    with open(path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def find_first(data, keys):
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key]:
                return data[key]

        for value in data.values():
            found = find_first(value, keys)
            if found:
                return found

    elif isinstance(data, list):
        for value in data:
            found = find_first(value, keys)
            if found:
                return found

    return None


def get_duration(audio_path):
    result = run_cmd([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path)
    ])

    return float(result.stdout.strip())


def get_scenes(payload):
    scenes = payload.get("scenes")

    if not isinstance(scenes, list):
        scenes = find_first(payload, ["scenes"])

    if not scenes:
        raise ValueError("No scenes found in request")

    return scenes


def get_image_url(scene):
    if isinstance(scene, str):
        return scene

    return find_first(
        scene,
        ["image_url", "imageUrl", "image", "url"]
    )


def get_scene_duration(scene):
    if isinstance(scene, dict):
        value = find_first(
            scene,
            ["duration", "duration_seconds", "durationSeconds", "seconds"]
        )

        if value is not None:
            try:
                return float(value)
            except Exception:
                pass

    return None


@app.get("/")
def home():
    return {
        "status": "ok",
        "service": "TrendPilot FFmpeg Render Server"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/render")
async def render(request: Request):
    payload = await request.json()

    job_id = uuid.uuid4().hex
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        scenes = get_scenes(payload)

        audio_url = find_first(
            payload,
            [
                "audio_url",
                "audioUrl",
                "audio",
                "voice_url",
                "voiceUrl"
            ]
        )

        if not audio_url:
            raise ValueError("No audio URL found in request")

        width = int(payload.get("width", 1080))
        height = int(payload.get("height", 1920))
        fps = int(payload.get("fps", 30))

        audio_path = job_dir / "audio"

        download_file(audio_url, audio_path)

        audio_duration = get_duration(audio_path)

        scene_data = []

        for i, scene in enumerate(scenes):
            image_url = get_image_url(scene)

            if not image_url:
                raise ValueError(f"No image URL found for scene {i + 1}")

            image_path = job_dir / f"image_{i}.jpg"

            download_file(image_url, image_path)

            duration = get_scene_duration(scene)

            scene_data.append({
                "image": image_path,
                "duration": duration
            })

        specified = [
            x["duration"]
            for x in scene_data
            if x["duration"] is not None
        ]

        remaining = audio_duration - sum(specified)

        missing = sum(
            1 for x in scene_data
            if x["duration"] is None
        )

        if missing > 0:
            default_duration = max(1.0, remaining / missing)
        else:
            default_duration = 1.0

        for item in scene_data:
            if item["duration"] is None:
                item["duration"] = default_duration

        segments = []

        for i, item in enumerate(scene_data):
            segment = job_dir / f"segment_{i}.mp4"

            duration = max(0.5, float(item["duration"]))

            vf = (
                f"scale={width}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                "setsar=1"
            )

            run_cmd([
                "ffmpeg",
                "-y",
                "-loop", "1",
                "-i", str(item["image"]),
                "-t", str(duration),
                "-vf", vf,
                "-r", str(fps),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-an",
                str(segment)
            ])

            segments.append(segment)

        concat_file = job_dir / "concat.txt"

        with open(concat_file, "w", encoding="utf-8") as f:
            for segment in segments:
                f.write(f"file '{segment.as_posix()}'\n")

        silent_video = job_dir / "silent.mp4"

        run_cmd([
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(silent_video)
        ])

        output_name = f"{job_id}.mp4"
        output_path = OUTPUT_DIR / output_name

        run_cmd([
            "ffmpeg",
            "-y",
            "-i", str(silent_video),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path)
        ])

        base_url = str(request.base_url).rstrip("/")
        video_url = f"{base_url}/video/{output_name}"

        return {
            "success": True,
            "status": "completed",
            "video_url": video_url,
            "url": video_url,
            "output_url": video_url,
            "filename": output_name,
            "duration": audio_duration
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:
        # Keep the final MP4 in /app/output.
        # Temporary downloaded files are removed.
        try:
            import shutil
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass


@app.get("/video/{filename}")
def get_video(filename: str):
    safe_name = Path(filename).name
    file_path = OUTPUT_DIR / safe_name

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Video not found"
        )

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=safe_name
    )
