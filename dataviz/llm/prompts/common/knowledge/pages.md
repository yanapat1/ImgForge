---
title: The pages: Home, Bucket, Image, Data
summary: what each page of DataViz is for, its URL screen id for navigate, the header's fixed order, and the Home overview cards
---
- **Header** on every page: the DataViz logo (opens Home) and the links **Bucket · Image · Data**, always in that order;
  a part this install does not have has no link (and no card on Home).
- **Home** (screen `home`, the app root): one card each for Bucket, Image and Data. Each card shows its counts (buckets and
  their size, S3 / linked; visualize and label sessions; Data workspaces), its three most recent items (a click opens
  one) and its buttons: Add data (the Bucket page's import dialog), Visualize / Label (the Image page on that tab with
  its New dialog), New query (Data). Running jobs show in a strip under the cards.
- **Bucket** (screen `bucket`, was the Data Center): buckets of files: managed folders, linked folders, S3 links.
- **Image** (screen `image`, was the Sessions home): the sessions, two tabs: **Visualize** (opens a session in the
  visualizer, screen `viz`) and **Label** (opens the labeling page, screen `label`).
- **Data** (screen `data`, was Analysis): SQL and MongoDB queries on the user's own databases, catalog, statistics,
  forecasts, dashboards.
- Old links: `?analysis` opens Data. The user may still say "Data Center", "Sessions" or "Analysis": they mean Bucket,
  Image and Data.
