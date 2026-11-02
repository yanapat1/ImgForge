---
title: Compare › Image conditions (camera / lighting shift)
summary: will a model trained on A fit B (new camera, new lighting): light, blur, noise, colour, capture metrics, shift-detector AUC, model fit, preview, recommendations
---
- Where: the Compare tab of a combined dataset (2+ datasets joined), the **Image conditions** section (collapsed).
  A = the data the model was trained on, B = the new data (new camera brand, new lighting, new site). **images**
  picks how many images per side are measured (1,000 / 3,000 default / 10,000 / all: a fixed sample, a bigger one
  reuses what was measured). **Analyse conditions** runs a background job (reads every sampled image once, cached
  per dataset, then instant); changing A or B asks to analyse again. `compare_shift` reads the result.
- Metrics per image (one decode; light / blur / colour measured at one ~512 px scale so cameras of different
  resolution compare): **Light** brightness (mean luma), contrast (luma std), crushed shadows (% luma ≤ 8), blown
  highlights (% ≥ 247), dynamic range (p99 − p1), tonal entropy · **Blur** sharpness (Laplacian variance with the
  noise part removed, so a noisy soft camera does not look sharp), edge strength (Tenengrad, noise removed), edge
  density (Canny %) · **Noise** sigma (Immerkær) · **Colour** R / G / B means, saturation, colourfulness, tint
  (Lab a*: green − / magenta +, b*: blue − / yellow +), white balance R/G and B/G · **Capture** width, height,
  megapixels, aspect, file bytes per pixel, JPEG quality (estimated from the quantisation tables), EXIF ISO,
  exposure, f-number · **Camera** EXIF make / model (often stripped by export tools: "no EXIF" is common).
- Per metric: A and B medians, Δ (a % or ×/÷ factor; tint in units), **KS**, **PSI** (0.1 slight, 0.25
  significant), **B out** = share of B outside the range of 98 % of A (A itself ~2 %: the excess is what the model
  never saw). Severity ok / minor / moderate / major = max(PSI / 0.25, effect in robust std units, B-out excess /
  15 %), with a minimum meaningful change per metric. Clicking a row draws A vs B with A's range shaded; links list
  B outside A's range, the lowest / highest B or A images in the Explore image grid.
- **Shift score** 0–100 and **shift detector AUC**: a classifier told "A or B?" from light / blur / noise / colour
  only, cross-validated: 0.5 = alike, 0.6–0.75 slightly different, 0.75–0.9 clearly, > 0.9 separate domains.
  "What gives B away" lists its strongest metrics. **Appearance AUC** = the same on the Explore feature (what a
  model sees), shown only when that feature is already computed. **Most B-like images** = the B images the detector
  is surest about: label these first.
- **Verdict**: fits (nothing beyond normal variation) / check (moderate drift, new camera, capture change, AUC ≥
  0.75) / shifted (a major drift, AUC ≥ 0.9, or the model's confidence drops ≥ 15 %). Few images (< 30 per side)
  = indicative only.
- **Light curve** (share of pixels per brightness level; R G B: A solid, B dotted) and **Image by image** scatters
  (brightness × contrast, sharpness × noise, saturation × tint) show it visually.
- **Model fit**: every model that already ran on this combined dataset (Model tab › Run): mean top confidence per
  image, images with nothing found, detections per image on A vs B (no labels needed), mAP50 / P / R when both
  sides have labels, the confidence histogram and **confidence vs <metric>** (e.g. confidence falls below a
  brightness). Nothing listed = no model has run here yet.
- **Preview**: an A image moved to B's medians (brightness / contrast gain, saturation, tint, blur fitted to the
  sharpness ratio, added noise), next to a real B image; strength 0–1.5. It shows what the new camera will do and
  the applied values are augmentation magnitudes to test the model with.
- **What to do**: rule-based advice (brightness → hsv_v, saturation → hsv_s, tint → hsv_h / white balance, blur →
  Blur / MotionBlur or check focus, noise → GaussNoise or denoise, resolution → imgsz, compression →
  ImageCompression, new camera / high AUC → label most-B-like images and fine-tune on A + B, validate on B) and a
  copyable Ultralytics hyp snippet. Export JSON / CSV.
- It measures image conditions, not labels: the label diff (cards, class table) above is a separate analysis.
