---
title: Video → frames (Extract frames, auto-extract)
summary: cut videos into images: fps / bursts / every Nth / count / keyframes / scene / smart, range, near-duplicates, layouts, frames.csv, auto-extract on upload
---
- Videos (mp4, mov, avi, mkv, webm, m4v, wmv, mpg) are files of kind `video`. On the page they show a poster;
  double-click (or details › Extract frames…, or Send to… › Extract frames…) opens the dialog: a player with
  Set start / Set end, a live frame count and a film strip of the planned frames.
- While a Bucket job (import, frames, sync, export, …) runs, sessions still open at once (their own job
  lane). Each account's Bucket jobs run one after another; other accounts' jobs do not wait for them.
- `extract_frames` (a background job, bk-frames, cancellable) on a video file, several, or a folder (its videos).
  Which frames:
  - `fps` = frames per second spread over each second (15 of 30 fps = every 2nd; 0.5 = one every 2 s).
  - `mode` pattern = every `every_s` seconds take `take` frames from a window of `window_s`, `pick` spread or
    consecutive. "First 15 of each second" = take 15, every_s 1, window_s 1, consecutive. "15 from 1 s every 2 s"
    = take 15, window_s 1, every_s 2.
  - `every` (n = step), `all`, `count` (n frames over the video), `keyframes` (fastest), `scene` (a frame per
    picture change), `smart` (the n most varied frames, still covering the whole video).
  - `start` / `end` seconds; `dedupe` skips near-identical frames (0.01 light, 0.03 strong); `max_side`, `format`
    jpg / png / webp. At most 200 000 frames per job.
- Output: a NEW folder `<video>-frames` (several videos: `<folder>-frames`) next to the video, written hidden and
  shown when complete; `out` names it. `layout`: `yolo` (default: images/ + empty labels/ + data.yaml, no classes
  yet) · `yolo-split` (images/train|val|test split by time block so near-identical frames never sit in two splits)
  · `per-video` · `flat`. Always `frames.csv` (file, video, frame, time_s, split). Names `<video>_<frame>.jpg`.
- A streamed S3 bucket is read-only: `target` copy (a new managed bucket; each video is downloaded first).
- Next: `send_to_session` the folder to label (add classes there) or visualize, or the page's Label with model.
- Auto-extract (`set_video_auto`, or the dialog's checkbox): every new video that lands in the bucket by upload,
  import or S3 load is cut with the saved settings; shown in the bucket details with Turn off. `off=true` stops it.
- Out of reach: making a video from images, trimming or re-encoding the video itself.
