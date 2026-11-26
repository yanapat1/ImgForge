---
name: find-label-errors
description: find likely wrong labels with k-NN suspicion, cleanlab and (if any) a model, then guide the fixes
when: find mislabelled boxes or wrong classes, clean the labels
---
1. `issue_summary` and `class_stats`: which classes have the most suspicious boxes and with which class they are
   confused; is cleanlab run; is the dataset scanned.
2. Make sure a strong feature is on (`openclip-h14` or `siglip2-so400m`); else propose `set_projection`, or the
   recipe `apply_recipe {id: 'find-mislabels'}` (a strong feature, UMAP, standardized, coloured by suspicion).
3. Start with the neighbour check: `show_issue` `suspicious` (boxes whose 10 nearest neighbours mostly have another
   class). Tell the user the lines on the map show those neighbours.
4. For a ranked, calibrated list: Find issues › Run label-quality check (cleanlab, logistic by default), then
   `show_issue` `cl-issues`; `cl-low` lists every box worst first. Colour by `cl_suggested` to see the proposals.
5. With a model run in the Model tab: `show_issue` `cl-swap` (the model sees another class), and the Model tab's
   Likely label errors.
6. Prioritise boxes flagged by two methods (suspicious + cleanlab, or two features: CLIP and DINOv2) — a single
   check also flags rare poses and look-alike classes.
7. Fixing is the user's: in the gallery, pick the boxes → pick bar › Change class (or "Change to cleanlab's class"),
   Delete for junk boxes; each has Undo (Curation › History). Then run the check again.
