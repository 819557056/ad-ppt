import { describe, expect, it } from 'vitest';
import { sceneHistoryTarget } from './sceneHistory';
import type { SceneResponse, TextElement } from './sceneTypes';

const element: TextElement = { id: 'title', kind: 'text', role: 'title', text: '标题', locked: false,
  frame: { x: 10, y: 10, w: 200, h: 60, rotation_deg: 0 },
  style: { font_family_id: 'noto-sans-sc', font_size_pt: 24, font_weight: 400, color: '#000000',
    align: 'left', vertical_align: 'top', line_height: 1.2, padding_pt: 0 } };
const base: SceneResponse = { page_id: 'page', revision_id: 'before', page_version: 3, scene_hash: 'hash',
  asset_urls: {}, scene: { schema_version: 1, font_manifest_id: 'fonts-v1',
    canvas: { width_pt: 960, height_pt: 540 }, background: { kind: 'solid', color: '#FFFFFF' },
    elements: [element, { ...element, id: 'note', locked: true }] } };

describe('Scene lock history', () => {
  it('inverts lock gestures to the original values, including repeated commands', () => {
    expect(sceneHistoryTarget(base, [
      { op: 'set_locked', element_id: 'title', locked: true },
      { op: 'set_locked', element_id: 'note', locked: false },
      { op: 'set_locked', element_id: 'title', locked: false },
    ])).toEqual({ kind: 'locks', commands: [
      { op: 'set_locked', element_id: 'title', locked: false },
      { op: 'set_locked', element_id: 'note', locked: true },
    ] });
    expect(base.scene.elements.map(item => item.locked)).toEqual([false, true]);
  });

  it('keeps other gestures and AI history behind the server revision lock checks', () => {
    expect(sceneHistoryTarget(base)).toEqual({ kind: 'revision', revision_id: 'before' });
    expect(sceneHistoryTarget(base, [{ op: 'set_text', element_id: 'title', text: '修改' }]))
      .toEqual({ kind: 'revision', revision_id: 'before' });
  });
});
