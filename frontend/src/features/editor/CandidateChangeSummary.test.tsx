import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CandidateChangeSummary } from './CandidateChangeSummary';
import type { CandidateSummary } from './sceneTypes';

const empty: CandidateSummary = { schema_version: 1, added: [], removed: [], modified: [],
  background_changed: false, layer_order_changed: false, text_count: 1, image_count: 0 };

describe('CandidateChangeSummary', () => {
  it('shows bounded object labels, IDs, change categories and page-level changes', () => {
    render(<CandidateChangeSummary summary={{ ...empty,
      added: [{ element_id: 'new-id', kind: 'text', role: 'body', label: '新正文' }],
      removed: [{ element_id: 'old-id', kind: 'image', role: 'photo', label: '旧图片' }],
      modified: [{ element_id: 'edited-id', kind: 'text', role: 'title', label: '新标题', changed_fields: ['text', 'frame'] }],
      background_changed: true, layer_order_changed: true }} />);
    expect(screen.getByText('相对生成时的基版本：新增 1 · 删除 1 · 修改 1')).toBeInTheDocument();
    expect(screen.getByText('删除 1 个对象')).toBeInTheDocument();
    expect(screen.getByText('old-id')).toBeInTheDocument();
    expect(screen.getByText('修改：文字、位置/尺寸')).toBeInTheDocument();
    expect(screen.getByText('背景已更换')).toBeInTheDocument();
    expect(screen.getByText('已有对象的层级顺序已调整')).toBeInTheDocument();
  });

  it.each([{}, { text_count: 2, image_count: 1 }, { schema_version: 1 }, { ...empty, schema_version: 2 }])(
    'does not mistake absent/unknown historic diff metadata for no changes', summary => {
      render(<CandidateChangeSummary summary={summary} />);
      expect(screen.getByText('此历史候选未记录对象变更清单，请预览核对。')).toBeInTheDocument();
      expect(screen.queryByText('页面内容无变化')).not.toBeInTheDocument();
    });

  it('reports no-op and escapes untrusted model text instead of rendering markup', () => {
    const { rerender, container } = render(<CandidateChangeSummary summary={empty} />);
    expect(screen.getByText('页面内容无变化')).toBeInTheDocument();
    rerender(<CandidateChangeSummary summary={{ ...empty,
      added: [{ element_id: 'id', kind: 'text', role: 'body', label: '<img src=x onerror=alert(1)>' }] }} />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/<img src=x/)).toBeInTheDocument();
  });
});
