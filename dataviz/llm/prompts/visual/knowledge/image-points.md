---
title: Objects or Full images (what one dot is)
summary: the Points switch of the Projection popover; full-image dots of a labelled dataset and their class; image-only data
core: false
---
- **Image-only data** (no label files, or the session set to one point per image): one dot per image, always. The
  class is `image`, or the image's sub-folder when "class from folder" is on. There is no Points switch.
- **Labelled data** (YOLO folder / bucket, combined dataset): the Projection popover has **Points: Objects | Full
  images**.
  - **Objects** (the default): one dot per labelled box, coloured by its class.
  - **Full images**: one dot per whole image. Its class is the image's **most frequent box class**, and an image
    with no boxes is `(no boxes)`. Area / aspect colourings, box editing, curation and model scoring against labels
    are not offered here (as for image-only data).
- The two are separate addresses of the same session: `s/<id>/` and `s/<id>@images/`. Switching reloads the page.
  Each kind keeps its own features, projections, saved views and tags, so switching back is instant once computed.
  Who can open the session can open both.
- A combined dataset in Full images shows every member as images. The Compare tab and the `origin` colour still work.
