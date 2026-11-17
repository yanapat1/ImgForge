---
name: video-to-frames
description: cut videos into image frames to label or visualize, once or automatically for every new video
when: the user mentions a video, mp4 / mov files, frames, fps, cutting, sampling or extracting images from a video
---
1. Find the videos (`search` with glob `*.mp4` or `list_folder`); use the exact paths. A folder takes all its videos.
2. Turn the wish into settings: "N per second" → `fps`; "first N of each second" or "N frames every K seconds" →
   `mode` pattern with `take`, `window_s`, `every_s`, `pick`; "about N images" → `mode` count or smart (with `n`);
   "only when the scene changes" → `mode` scene. Add `start` / `end` for a part, `dedupe` 0.01 for a static camera.
3. Pick the `layout`: yolo to label next, yolo-split for training splits, per-video for many clips.
4. Propose `extract_frames`; on a streamed S3 bucket use `target` copy with a `new_name`.
5. Report the job and the folder it writes; `job_status` (job_id, wait_s 60) says when it is done and the frame count; offer `send_to_session` (label or visualize) next.
6. "Every new video" → `set_video_auto` with the same settings (`off` to stop).
