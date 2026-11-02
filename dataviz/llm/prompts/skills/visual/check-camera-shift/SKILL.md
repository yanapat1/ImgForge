---
name: check-camera-shift
description: tell whether a model trained on one dataset will fit new data from another camera / lighting, and what to do
when: the user changed camera, vendor, lens or lighting, asks if the old model still fits new data, or asks about light / dark / blur / noise / colour differences between datasets
---
1. It needs a combined dataset of the old (A, training) and new (B) data: if this session is not one, say how
   (home page: pick both datasets, Combine) and stop.
2. `compare_shift`: no report yet → `open_tab` `compare` and tell the user to expand **Image conditions**, set A =
   training data, B = new data, and press **Analyse conditions** (a background job; 3,000 images per side by default).
3. Read the verdict and score, then the moved metrics in this order: light (brightness, contrast, clipped shadows /
   highlights), blur (sharpness), noise, colour (saturation, tint / white balance), capture (resolution, JPEG
   quality, new cameras). Quote A → B medians and B-out; say which are major vs minor. AUC: 0.5 alike … > 0.9
   separate domains; name the drivers.
4. Model evidence beats image statistics: if its models list is not empty, compare mean top confidence, images with nothing
   found and mAP50 on A vs B (mAP only with labels on both sides), and point to "confidence vs <metric>". If empty,
   suggest running the model on this combined dataset (Model tab) for a direct check.
5. Recommend, most effective first: fix capture at the source when possible (exposure, white balance, focus,
   compression), the augmentations in its recommendations and hyp snippet (give the snippet), and when the verdict is
   shifted or a new camera appears: label the **Most B-like images** first, fine-tune on A + B, validate on B.
6. Few images (< 30 per side) or EXIF missing: say the result is indicative only. You cannot run the analysis or
   change files yourself.
