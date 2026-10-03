import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScenePage, SceneProject } from '@/features/editor/sceneTypes';
import { TemplateImport } from './TemplateImport';

const mocks = vi.hoisted(() => ({ list: vi.fn(), bind: vi.fn(), project: vi.fn(), document: vi.fn(), remove: vi.fn() }));
vi.mock('@/features/editor/sceneApi', () => ({
  listTemplateReferences: mocks.list,
  bindPageTemplate: mocks.bind,
  deleteTemplateReference: mocks.remove,
  getSceneProject: mocks.project,
  getTemplateDocument: mocks.document,
  assetUrl: (url: string) => url
}));

const pages: ScenePage[] = [
  { page_id: 'page-1', order_index: 0, outline_content: { title: '封面' },
    page_version: 1, revision_id: 'revision-1' },
  { page_id: 'page-2', order_index: 1, outline_content: { title: '结束' },
    page_version: 1, revision_id: 'revision-2' }
];
const references = [
  { template_asset_id: 'cover', template_document_id: 'document', source_page_index: 1,
    preview_url: null, thumbnail_url: null, analysis_status: 'completed', analysis_revision: 1,
    analysis: { role: 'cover' } },
  { template_asset_id: 'closing', template_document_id: 'document', source_page_index: 2,
    preview_url: null, thumbnail_url: null, analysis_status: 'completed', analysis_revision: 1,
    analysis: { role: 'closing' } }
];

describe('TemplateImport auto match', () => {
  beforeEach(() => {
    localStorage.clear();
    mocks.list.mockReset().mockResolvedValue(references);
    mocks.bind.mockReset().mockImplementation(async (_id: string, page: ScenePage) => page);
    mocks.project.mockReset().mockResolvedValue({ project_id: 'project-1', pages } as SceneProject);
    mocks.document.mockReset();
    mocks.remove.mockReset();
    vi.stubGlobal('confirm', vi.fn(() => true));
  });

  it('confirms the affected bindings, removes the reference, and keeps history explicitly', async () => {
    const boundPages = pages.map(page => ({ ...page, template_asset_id: 'cover' }));
    const updated = { project_id: 'project-1', project_version: 4, pages } as SceneProject;
    mocks.remove.mockResolvedValue(updated);
    const onProjectUpdated = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={boundPages[0]} pages={boundPages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={onProjectUpdated} onError={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: '移除参考页 1' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('解除 2 页的当前绑定'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('历史计划、快照及素材仍会保留'));
    await waitFor(() => expect(onProjectUpdated).toHaveBeenCalledWith(updated));
    expect(mocks.remove).toHaveBeenCalledWith('project-1', 3, boundPages, references[0]);
    expect(screen.queryByRole('button', { name: '移除参考页 1' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '移除参考页 2' })).toBeInTheDocument();
  });

  it('does not remove a reference with unsaved edits or without confirmation', async () => {
    const canBind = vi.fn(() => false);
    const onError = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={canBind} onPageUpdated={() => {}} onProjectUpdated={() => {}} onError={onError} />);
    const button = await screen.findByRole('button', { name: '移除参考页 1' });
    fireEvent.click(button);
    expect(onError).toHaveBeenCalledWith('请先完成当前页保存，再移除参考页');
    expect(window.confirm).not.toHaveBeenCalled();
    canBind.mockReturnValue(true);
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(button);
    expect(mocks.remove).not.toHaveBeenCalled();
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.change(screen.getByLabelText('本页风格补充说明'), { target: { value: '尚未保存的说明' } });
    fireEvent.click(button);
    expect(onError).toHaveBeenCalledWith('请先保存风格补充说明，再移除参考页');
    expect(mocks.remove).not.toHaveBeenCalled();
  });

  it('retains the reference and draft on version conflict', async () => {
    mocks.remove.mockRejectedValue({ response: { data: { error: { code: 'PAGE_VERSION_CONFLICT' } } } });
    const onError = vi.fn();
    const updated = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={updated} onError={onError} />);
    fireEvent.click(await screen.findByRole('button', { name: '移除参考页 1' }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining('当前草稿未改动')));
    expect(updated).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '移除参考页 1' })).toBeEnabled();
  });

  it('serializes removal and explains an in-flight analysis rejection', async () => {
    let reject!: (error: unknown) => void;
    mocks.remove.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    const onError = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={() => {}} onError={onError} />);
    const button = await screen.findByRole('button', { name: '移除参考页 1' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mocks.remove).toHaveBeenCalledOnce();
    expect(button).toBeDisabled();
    expect(screen.getAllByRole('button', { name: '用于本页' })[0]).toBeDisabled();
    reject({ response: { data: { error: { code: 'ANALYSIS_IN_PROGRESS' } } } });
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining('请完成或取消任务')));
    expect(button).toBeEnabled();
  });

  it('shows a reviewable mapping and applies it only after confirmation', async () => {
    const updated = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={updated} onError={() => {}} />);
    fireEvent.click(await screen.findByText('自动匹配建议（2 页）'));
    expect(screen.getByText(/第 1 页 · 封面 → 参考页 1/)).toBeTruthy();
    expect(screen.getByText(/第 2 页 · 结束 → 参考页 2/)).toBeTruthy();
    expect(mocks.bind).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('应用到未绑定页面'));
    await waitFor(() => expect(updated).toHaveBeenCalledOnce());
    expect(mocks.bind.mock.calls.map(call => [call[1].page_id, call[2]]))
      .toEqual([['page-1', 'cover'], ['page-2', 'closing']]);
  });

  it('stops on a page conflict and reports the partial application', async () => {
    mocks.bind.mockResolvedValueOnce(pages[0]).mockRejectedValueOnce(new Error('版本冲突'));
    const onError = vi.fn();
    const updated = vi.fn();
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={updated} onError={onError} />);
    fireEvent.click(await screen.findByText('自动匹配建议（2 页）'));
    fireEvent.click(screen.getByText('应用到未绑定页面'));
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining('已应用 1/2 页')));
    expect(updated).toHaveBeenCalledOnce();
  });

  it('requires explicit acknowledgement for an unanalysed image-only reference', async () => {
    mocks.list.mockResolvedValue([{ ...references[0], analysis_status: 'pending', analysis: null }]);
    const confirm = vi.fn(() => false);
    vi.stubGlobal('confirm', confirm);
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={() => {}} onError={() => {}} />);
    expect(await screen.findByText(/未分析 · 生成需视觉模型/)).toBeTruthy();
    fireEvent.click(screen.getByText('用于本页'));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('模型需支持图片输入'));
    expect(mocks.bind).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByText('用于本页'));
    await waitFor(() => expect(mocks.bind).toHaveBeenCalledOnce());
  });

  it('restores the latest import report, shows conversion warnings, and refreshes after a long task', async () => {
    localStorage.setItem('banana-scene-template-document-v1:project-1', 'document-1');
    mocks.document.mockResolvedValueOnce({ document_id: 'document-1', status: 'converting',
      source_page_count: 33, warnings: ['EXTERNAL_RELATIONSHIP_IGNORED', 'EMBEDDED_OBJECT_STATIC_ONLY',
        'FONT_FAMILY_UNAVAILABLE'],
      error_code: null, task_id: 'task-1' }).mockResolvedValueOnce({ document_id: 'document-1',
      status: 'preview_ready', source_page_count: 33,
      warnings: ['EXTERNAL_RELATIONSHIP_IGNORED', 'EMBEDDED_OBJECT_STATIC_ONLY',
        'FONT_FAMILY_UNAVAILABLE'],
      error_code: null, task_id: 'task-1' });
    render(<TemplateImport projectId="project-1" projectVersion={3} page={pages[0]} pages={pages} credentialId=""
      canBind={() => true} onPageUpdated={() => {}} onProjectUpdated={() => {}} onError={() => {}} />);
    expect(await screen.findByText(/后台处理中（converting） · 33 页/)).toBeInTheDocument();
    expect(screen.getByText('外部链接未刷新，静态预览可能与原文件不同。')).toBeInTheDocument();
    expect(screen.getByText('嵌入对象仅按静态内容预览。')).toBeInTheDocument();
    expect(screen.getByText(/部分幻灯片显式指定的字体未安装/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新导入状态' }));
    expect(await screen.findByText(/参考页已就绪 · 33 页/)).toBeInTheDocument();
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });
});
