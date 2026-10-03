import type { Crop, Frame, ImageElement, SlideScene, TextElement, TextStyle } from './slideScene.generated';
export type { Crop, Frame, ImageElement, SlideScene, TextElement, TextStyle } from './slideScene.generated';
export type SceneElement = TextElement | ImageElement;
export type OutlineRole = 'cover' | 'agenda' | 'section' | 'content' | 'closing' | 'unknown';
export type OutlineContent = { title?: string; points?: string[]; role?: OutlineRole;
  facts_needed?: string[]; sources?: string[] };
export type ScenePage = { page_id: string; order_index: number; outline_content: OutlineContent | null;
  page_version: number; revision_id: string; template_asset_id?: string | null; template_style_text?: string | null };
export type SceneProject = { project_id: string; title: string; prompt: string; editor_mode: 'scene_v1';
  project_version: number; canvas: { width_pt: number; height_pt: number }; font_manifest_id: string;
  active_plan_id: string | null; model_config: { text_model?: string; image_model?: string }; pages: ScenePage[] };
export type SceneResponse = { page_id: string; page_version: number; revision_id: string; scene_hash: string;
  scene: SlideScene; asset_urls: Record<string, string> };
export type CandidateObjectChange = { element_id: string; kind: SceneElement['kind'];
  role: SceneElement['role']; label: string; changed_fields?: string[] };
export type CandidateSummary = { text_count?: number; image_count?: number; warnings?: string[];
  schema_version?: number; base_revision_id?: string | null; added?: CandidateObjectChange[];
  removed?: CandidateObjectChange[]; modified?: CandidateObjectChange[];
  background_changed?: boolean; layer_order_changed?: boolean };
export type SceneCandidate = { candidate_id: string; page_id: string; state: string;
  revision_id: string; change_summary: CandidateSummary };
export type Command =
  | { op: 'add_element'; element: SceneElement }
  | { op: 'delete_element'; element_id: string }
  | { op: 'set_text'; element_id: string; text: string }
  | { op: 'set_frame'; element_id: string; frame: Frame }
  | { op: 'set_text_style'; element_id: string; style: TextStyle }
  | { op: 'replace_image'; element_id: string; asset_id: string }
  | { op: 'set_crop'; element_id: string; crop: Crop; fit: 'contain' | 'cover' }
  | { op: 'set_opacity'; element_id: string; opacity: number }
  | { op: 'reorder_elements'; element_ids: string[] }
  | { op: 'set_background'; background: SlideScene['background'] }
  | { op: 'set_locked'; element_id: string; locked: boolean };
