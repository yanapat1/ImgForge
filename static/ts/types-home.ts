// Types of the Sessions home (views/home.ts) and the labeling session wizard (views/label-new.ts): the rows of
// GET api/datasets and GET api/label/projects as the home cards read them. Pure types, no runtime code.

import type { Role } from './types.js';
import type { DatasetItem } from './types-settings.js';

/** A person on a card (owner / member); never an email. */
export interface SessionPerson { id: string; username?: string; name?: string; avatar_ver?: number }
export interface SessionMember extends SessionPerson { role: 'viewer' | 'labeler' }
export type Visibility = 'private' | 'global';
/** Who owns / shares a session and what I may do with it (dataviz/data/sessions.py listing). */
export interface SharedFields {
  owner?: SessionPerson | null;
  members?: SessionMember[];
  visibility?: Visibility;
  my_role?: Role;
  /** when I last opened it (epoch seconds; null = never) */
  opened_at?: number | null;
}
/** The home's scope filter (GET api/datasets?scope=, api/label/projects?scope=); 'everyone' is for admins. */
export type HomeScope = 'all' | 'mine' | 'shared' | 'global' | 'everyone';

/** A saved visualize session (GET api/datasets → datasets[], dataset_public in routes/datasets.py). */
export interface HomeSession extends DatasetItem, SharedFields {
  active?: boolean;
  /** open on the server right now (in its registry of loaded sessions) */
  loaded?: boolean;
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
export interface HomeProject extends SharedFields {
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
