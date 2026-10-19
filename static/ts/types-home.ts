// Types of the Sessions home (views/home.ts) and the labeling session wizard (views/label-new.ts): the rows of
// GET api/datasets and GET api/label/projects as the home cards read them. Pure types, no runtime code.

import type { DatasetItem } from './types-settings.js';

/** A saved visualize session (GET api/datasets → datasets[], dataset_public in routes/datasets.py). */
export interface HomeSession extends DatasetItem {
  active?: boolean;
  /** number of crop tiles that exist for the mosaic (0 = not indexed yet) */
  cover?: number;
  /** epoch seconds */
  created?: number;
  tags?: string[];
  stats?: {
    n_boxes?: number | null; n_images?: number | null; n_classes?: number | null; classes?: string[];
    last_opened?: string | null; workspace?: string;
  };
}

/** One task of a labeling session card: counts per status + review counts (labeling/projects.py card()). */
export interface HomeTask {
  id: string; name: string; task_type: string; n_classes?: number;
  done?: number; in_progress?: number; skipped?: number; todo?: number;
  review_manual?: number; review_bulk?: number;
}
export interface HomeProjectSource { kind?: string; path?: string; bucket?: string; prefix?: string }
/** Where the last export went (project.export_default.target). */
export interface HomeExportTarget { kind?: string; path?: string; bucket?: string; prefix?: string }
/** A labeling session card (GET api/label/projects → projects[]). */
export interface HomeProject {
  id: string; name: string;
  created?: number | null; updated?: number | null;
  source?: HomeProjectSource | null;
  n_media?: number;
  cover?: string[];
  tasks?: HomeTask[];
  tags?: string[];
  review?: boolean;
  error?: string;
  /** not in the list rows: read once from GET api/label/projects/{pid} for the "Re-export to" item */
  export_default?: { target?: HomeExportTarget | null } | null;
}

/** Options of the wizard (views/label-new.ts openWizard). */
export interface WizardOpts {
  /** project id to edit; absent = a new session */
  edit?: string | undefined;
  /** the other label sessions' names (a new name is checked against them) */
  names?: string[] | undefined;
}
