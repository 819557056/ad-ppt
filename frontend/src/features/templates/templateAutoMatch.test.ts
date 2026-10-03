import { describe, expect, it } from 'vitest';
import type { ScenePage } from '@/features/editor/sceneTypes';
import type { TemplateReference } from '@/features/editor/sceneApi';
import { suggestTemplateMatches } from './templateAutoMatch';

const page = (id: string, index: number, title: string, bound: string | null = null): ScenePage => ({
  page_id: id, order_index: index, outline_content: { title, points: [] },
  page_version: 1, revision_id: `revision-${id}`, template_asset_id: bound
});
const reference = (id: string, index: number, role: string, completed = true): TemplateReference => ({
  template_asset_id: id, template_document_id: 'document-1', source_page_index: index,
  preview_url: null, thumbnail_url: null, analysis_status: completed ? 'completed' : 'pending',
  analysis_revision: completed ? 1 : 0, analysis: { role }
});

describe('template auto match', () => {
  it('uses analyzed roles, balances content references, and preserves manual choices', () => {
    const pages = [page('a', 0, '封面'), page('b', 1, '目录'), page('c', 2, '市场'),
      page('d', 3, '方案'), page('e', 4, '结束', 'manually-selected')];
    const refs = [reference('cover', 1, 'cover'), reference('agenda', 2, 'agenda'),
      reference('body-a', 3, 'content'), reference('body-b', 4, 'content'),
      reference('closing', 5, 'closing'), reference('unreviewed', 6, 'content', false)];
    const suggestions = suggestTemplateMatches(pages, refs);
    expect(suggestions.map(item => [item.page.page_id, item.reference.template_asset_id]))
      .toEqual([['a', 'cover'], ['b', 'agenda'], ['c', 'body-a'], ['d', 'body-b']]);
  });

  it('does not add a cover to a one-page deck or use unanalyzed examples', () => {
    const one = [page('single', 0, '试样')];
    expect(suggestTemplateMatches(one, [reference('cover', 1, 'cover')])).toEqual([]);
    expect(suggestTemplateMatches(one, [reference('pending', 1, 'content', false)])).toEqual([]);
    expect(suggestTemplateMatches(one, [reference('body', 2, 'content')])[0].reference.template_asset_id)
      .toBe('body');
  });
  it('uses the confirmed outline role instead of guessing from page position', () => {
    const first = { ...page('a', 0, '第一部分'), outline_content: { title: '第一部分', role: 'section' as const } };
    const suggestions = suggestTemplateMatches([first, page('b', 1, '结束')],
      [reference('cover', 1, 'cover'), reference('section', 2, 'section'), reference('closing', 3, 'closing')]);
    expect(suggestions[0].reference.template_asset_id).toBe('section');
  });

});
