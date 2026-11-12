---
title: Objects or Full images (what one dot is)
summary: the Points switch (Objects | Full images), the class of a full-image dot, image-only data, predicting classes per image with a model and comparing two models
core: false
---
- **Image-only data** (no label files, or the session set to one point per image): one dot per image, always. The
  class is "image", or the image's sub-folder when "class from folder" is on. There is no Points switch.
- **Labelled data** (YOLO folder / bucket, combined dataset): the Projection popover has **Points: Objects | Full
  images**.
  - **Objects** (the default): one dot per labelled box, coloured by its class.
  - **Full images**: one dot per whole image. Its class is the image's **most frequent box class**, and an image
    with no boxes is "(no boxes)". Area / aspect colourings, box editing, curation and model scoring against labels
    are not offered here (as for image-only data).
- The two are separate addresses of the same session: s/<id>/ and s/<id>@images/. Switching reloads the page.
  Each kind keeps its own features, projections, saved views and tags, so switching back is instant once computed.
  Who can open the session can open both.
- A combined dataset in Full images shows every member as images. The Compare tab and the `origin` colour still work.
- **Predicting classes with a model** (no labels needed): Model tab, pick a model, **Run on this dataset**. Each image
  dot gets the model's **most confident detection** as its class ("nothing found" below the Confidence slider).
  The left panel shows the images and detections per predicted class (click a row to load those images); the right
  shows images per predicted class (per dataset in a combined one) and the top-confidence histogram. **Compare with
  another model** gives the agreement table, the % of images where both agree (classes matched by name) and Cohen's
  κ, and can load the images they disagree on. Colour the map with `ip_cls` / `ip_score`. Nothing is scored right
  or wrong, and nothing is written to the labels (to make labels from a model use Bucket › Label with model).
