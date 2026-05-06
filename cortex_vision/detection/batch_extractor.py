"""Batch scene extraction using PySceneDetect.

Adapted from VideoIndex/ai-video-index/lib/scene_extractor.py with two
differences for cortex-vision:

  1. Writes keyframes into the per-session ``frames/<scene_index>/<frame_index>.jpg``
     layout that the SceneEntry schema expects, instead of a flat
     ``scene_NNNN.jpg`` directory.

  2. Supports `keyframes_per_scene > 1` — sample multiple frames across each
     scene to give the vision describer a richer picture (e.g. early + middle
     + late). Defaults to 1 (midpoint) for batch mode parity with VideoIndex.

Usage:
    from cortex_vision.detection.batch_extractor import extract_scenes
    scenes = extract_scenes(
        video_path="source.mp4",
        frames_dir=Path(".../sessions/abc/frames/"),
    )
    # scenes: list[ExtractedScene]

The single-shot fallback for cut-less short videos (TikTok clips, etc.) is
preserved — when PySceneDetect returns an empty scene_list we synthesize 1-5
evenly spaced sample windows so the describer has something to work with.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExtractedScene:
    """One scene boundary plus its captured keyframe paths.

    This is the intermediate shape that batch.py converts into a SceneEntry.
    """
    index: int
    start_s: float
    end_s: float
    duration_s: float
    keyframe_paths: list[str] = field(default_factory=list)
    brightness: float = 0.0           # mean of the first keyframe (filter dark)
    trigger_method: str = "scenedetect"   # or "single_shot_fallback"


def extract_scenes(
    video_path: str,
    frames_dir: Path,
    threshold: float = 27.0,
    min_scene_len_s: float = 1.0,
    keyframes_per_scene: int = 1,
    jpeg_quality: int = 85,
) -> list[ExtractedScene]:
    """Run PySceneDetect on a video file and capture keyframes per scene.

    Args:
        video_path: Path to a local video file (mp4, mkv, webm, ...).
        frames_dir: Output root for keyframes. Subdirs ``<scene_index>/`` are
            created on demand. Files are named ``<frame_index>.jpg``.
        threshold: ContentDetector threshold. Lower = more sensitive (more
            scene cuts detected). 27.0 is PySceneDetect's recommended default.
        min_scene_len_s: Drop scenes shorter than this many seconds.
        keyframes_per_scene: How many frames to capture across each scene.
            1 = midpoint only; 3 = early+middle+late.
        jpeg_quality: Output JPEG quality 0-100.

    Returns:
        Ordered list of ExtractedScene. Empty list if the video has zero
        usable frames (corrupt file).

    Raises:
        FileNotFoundError: if `video_path` doesn't exist
        RuntimeError: if OpenCV can't open the video
    """
    import cv2
    import numpy as np
    from scenedetect import ContentDetector, SceneManager, open_video

    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(video_path)

    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Scene boundary detection via PySceneDetect
    # ------------------------------------------------------------------
    video = open_video(str(src))
    sm = SceneManager()
    sm.add_detector(
        ContentDetector(
            threshold=threshold,
            min_scene_len=int(min_scene_len_s * video.frame_rate),
        )
    )
    sm.detect_scenes(video, show_progress=False)
    raw_scene_list = sm.get_scene_list()

    # ------------------------------------------------------------------
    # OpenCV for keyframe extraction (PySceneDetect's video object isn't
    # ergonomic for arbitrary frame seek)
    # ------------------------------------------------------------------
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV failed to open {src}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    total_duration_s = total_frames / fps if fps > 0 else 0.0

    # ------------------------------------------------------------------
    # Single-shot fallback for cut-less videos
    # ------------------------------------------------------------------
    trigger_method = "scenedetect"
    if not raw_scene_list and total_duration_s > 0:
        trigger_method = "single_shot_fallback"
        n_samples = max(1, min(5, int(total_duration_s / 3) + 1))
        chunk = total_duration_s / n_samples
        scene_pairs = [
            (i * chunk, (i + 1) * chunk) for i in range(n_samples)
        ]
    else:
        scene_pairs = [
            (s.get_seconds(), e.get_seconds()) for s, e in raw_scene_list
        ]

    # ------------------------------------------------------------------
    # Capture keyframes
    # ------------------------------------------------------------------
    scenes: list[ExtractedScene] = []

    for scene_idx, (start_s, end_s) in enumerate(scene_pairs):
        duration = end_s - start_s
        if duration <= 0:
            continue

        scene_frames_dir = frames_dir / str(scene_idx)
        scene_frames_dir.mkdir(parents=True, exist_ok=True)

        keyframe_paths: list[str] = []
        first_brightness = 0.0

        # Sample uniformly across the scene. With keyframes_per_scene=1 this
        # captures the midpoint (offset=0.5). With =3 it captures
        # ~1/4, ~1/2, ~3/4 through the scene.
        for frame_idx in range(keyframes_per_scene):
            offset = (frame_idx + 1) / (keyframes_per_scene + 1)
            target_s = start_s + duration * offset
            cap.set(cv2.CAP_PROP_POS_MSEC, target_s * 1000)
            ret, frame = cap.read()
            if not ret:
                continue

            out_path = scene_frames_dir / f"{frame_idx}.jpg"
            cv2.imwrite(
                str(out_path),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
            )
            keyframe_paths.append(str(out_path))

            if frame_idx == 0:
                first_brightness = float(np.mean(frame))

        if not keyframe_paths:
            # Couldn't read any frames in this scene window; skip
            continue

        scenes.append(
            ExtractedScene(
                index=scene_idx,
                start_s=start_s,
                end_s=end_s,
                duration_s=duration,
                keyframe_paths=keyframe_paths,
                brightness=first_brightness,
                trigger_method=trigger_method,
            )
        )

    cap.release()
    return scenes


def probe_duration(video_path: str) -> float:
    """Quick OpenCV-only duration probe (avoids loading PySceneDetect)."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return n / fps if fps > 0 else 0.0
