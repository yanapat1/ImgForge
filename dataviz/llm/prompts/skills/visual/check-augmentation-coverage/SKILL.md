---
name: check-augmentation-coverage
when: the user asks whether augmentation will make the dataset fit another place, situation or a new camera, or wants a recipe for that
description: Build an augmentation recipe for the new images and check how much of them it covers
---
1. `augment_coverage`. With no check yet, tell the user to open Augment › Coverage, pick the new images (Upload images…
   or a Bucket folder) and press Check coverage; meanwhile propose a starting recipe.
2. To propose one: `augment_catalog`, pick the steps for the change the user describes (a new camera: colour
   temperature, tone curve, sharpness, lens distortion, JPEG, noise; night: darker, gamma, ISO noise; weather: rain,
   fog), moderate chances (0.2–0.5), and `set_augment_recipe` with a one-line note.
3. With a report: say coverage before → after in plain words, then the gaps it lists; add their steps to the recipe
   (`set_augment_recipe` with the whole new recipe). Warn when unreal variants are 20 % or more: lower the strength.
4. When the user wants the app to choose: point to **Find a recipe for these images** in the Coverage view; it tries the
   steps for them and shows the coverage after each one.
5. Ask the user to check the Boxes view (labels lost by crops or shifts) and to run Coverage again.
6. Say that coverage is about appearance only: real labelled images from the new place are the real test.
