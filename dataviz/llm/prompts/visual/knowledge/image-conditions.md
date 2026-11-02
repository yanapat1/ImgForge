---
title: Compare › Image conditions (camera / lighting shift)
summary: will a model trained on A fit B (new camera, new lighting): light, light layout, blur, noise, colour, capture metrics, CI / q, light map, exposure mix, coverage, shift-detector AUC, model fit, preview, B → A correction LUT, model stress test, recommendations
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
- More metrics (same decode): **Light** midtones (median luma: a tone-curve / gamma change), local contrast (16 px
  block std), **vignetting** (corner / centre light, < 1 = darker corners), **uneven light** (4×4 grid CV %), **light
  direction** left(−)/right(+) and top(−)/bottom(+) in % of the mean, **haze / glare** (dark channel: lifted blacks) ·
  **Blur** blur direction (0 = defocus, → 1 = motion blur; n/a when no detail stays above the noise) · **Noise**
  colour noise (Lab a*/b* sigma) · **Colour** colour temperature in kelvin (McCamy; lower = warmer / yellower) ·
  **Capture** JPEG blocking (8 px edges / inside, > 1.2 visible blocks), time of day (EXIF).
- Statistics per metric: **95 % CI** of the median change (bootstrap) and **q** (KS p-value, Benjamini-Hochberg over
  the metrics). With many images almost everything is "significant": severity stays on the size of the change; q
  only tells whether a small change is real. Table tooltip, the line under the chart and the CSV carry them.
- **Light map**: mean brightness of a 4×4 grid over A's images, B's, and B − A (red = darker in B, blue = brighter):
  shows vignetting, a lamp on one side, a darker bottom. **Exposure mix**: share of images dark < 60 / dim 60–100 /
  normal 100–160 / bright > 160 (mean luma) per side + flat images (contrast < 25). **Light over the day**: brightness
  by EXIF hour (only when ≥ 30 % have a time). Scatters add Temperature × brightness and Light layout (vignetting ×
  uneven light).
- **B covered by A** KPI (when the Explore feature is computed): share of B's boxes with an A box as close as A's boxes
  are to each other; the rest is appearance the model never saw. **Uncovered B images** lists them in the grid
  (label these). Fréchet distance in its tooltip.
- Model fit adds a **Confidence | mAP50** toggle on "vs <metric>": mAP50 of the images per metric bin (A + B, labels
  needed), e.g. mAP falling below a brightness.
- **Preview › B corrected to A**: a per-channel curve (LUT, CDF matching of B's R / G / B to A's) applied to a B image;
  **LUT** downloads it as JSON with a 3-line `cv2.LUT` snippet to run before the model (a fix at inference instead of
  retraining). It fixes brightness / contrast / colour cast only, never blur, noise or compression; the hint shows
  the mean R / G / B gap before and after. `compare_shift` returns it as its correction field.
- **A as B** preview now also applies gamma, vignette, motion blur (when B's blur has a direction), colour noise and
  JPEG at B's quality.
- **Stress test** (collapsed, under the report): pick a model (Settings › Models; it does not need to have run here),
  images (50 / 150 / 500 of A, the labelled ones when A has labels) and axes, **Run stress test** (a job: the model runs
  images × steps times, e.g. 150 × 65 ≈ 10 k inferences, minutes on CPU). Axes: brightness ×0.3–1.75, midtones
  (gamma 0.5–2), contrast ×0.4–1.6, defocus blur σ 0–4 px, motion blur 0–15 px (px at a 640 px long side), noise σ
  0–25, colour temperature −3000…+3000 K, saturation ×0–2, JPEG quality 100–10, resolution ×1–0.25. Score = mAP50
  with labels, else **consistency** (mAP50 against its own clean predictions: how much it still finds). Per axis:
  the **safe window** (steps keeping ≥ 95 % of the clean score), A's own p5–p95 (grey), **B's position** (from the
  Image conditions report: B's median change mapped through what each step measurably does) and the **expected
  score at B**; status ok (< 5 % drop) / moderate / major (≥ 15 %). The headline = the worst axis: "Expected mAP50 on
  B ≈ …". It covers conditions only (not new objects / backgrounds): still validate on a few labelled B images.
  Clicking a row / point shows one image at every step. CSV export. `compare_stress` reads it.
