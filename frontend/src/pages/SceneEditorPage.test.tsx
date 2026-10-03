import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Command, SceneProject, SceneResponse, SlideScene, TextElement } from '@/features/editor/sceneTypes';
import { SceneEditorPage } from './SceneEditorPage';

const mocks = vi.hoisted(() => ({
  accepted: false,
  templateProps: null as null | { projectVersion: number; canBind: () => boolean;
    onProjectUpdated: (project: SceneProject) => void },
  addScenePage: vi.fn(),
  acceptCandidate: vi.fn(),
  acceptCandidates: vi.fn(),
  getSceneProject: vi.fn(),
  getSceneTask: vi.fn(),
  getOutlineDraft: vi.fn(),
  listSceneProjectTasks: vi.fn(),
  getPageScene: vi.fn(),
  getCandidates: vi.fn(),
  restoreScene: vi.fn(),
  listModelCredentials: vi.fn(),
  getSceneFontManifest: vi.fn(),
  getSnapshotPreflight: vi.fn(),
  createSnapshot: vi.fn(),
  createExport: vi.fn(),
  saveSceneCommands: vi.fn(),
  startSceneAiEdit: vi.fn(),
}));

vi.mock('@/features/editor/StorageCapacity', () => ({ StorageCapacity: () => null }));
vi.mock('@/features/editor/QueueCapacity', () => ({ QueueCapacity: () => null }));
vi.mock('@/features/templates/TemplateImport', () => ({
  TemplateImport: (props: NonNullable<typeof mocks.templateProps>) => { mocks.templateProps = props; return null; }
}));
vi.mock('@/features/editor/sceneApi', () => ({
  assetUrl: (url: string) => url,
  addScenePage: mocks.addScenePage,
  acceptCandidate: mocks.acceptCandidate,
  acceptCandidates: mocks.acceptCandidates,
  getSceneProject: mocks.getSceneProject,
  getSceneTask: mocks.getSceneTask,
  getOutlineDraft: mocks.getOutlineDraft,
  getPageScene: mocks.getPageScene,
  getCandidates: mocks.getCandidates,
  restoreScene: mocks.restoreScene,
  listModelCredentials: mocks.listModelCredentials,
  getSceneFontManifest: mocks.getSceneFontManifest,
  getSnapshotPreflight: mocks.getSnapshotPreflight,
  createSnapshot: mocks.createSnapshot,
  createExport: mocks.createExport,
  saveSceneCommands: mocks.saveSceneCommands,
  startSceneAiEdit: mocks.startSceneAiEdit,
  listSceneProjectTasks: mocks.listSceneProjectTasks,
  listSceneExports: async () => ({ items: [], next_cursor: null }),
}));

const blank: SlideScene = { schema_version: 1, font_manifest_id: 'fonts-v1',
  canvas: { width_pt: 960, height_pt: 540 }, background: { kind: 'solid', color: '#FFFFFF' }, elements: [] };
const proposed: SlideScene = { ...blank, elements: [{ id: 'element-1', kind: 'text', role: 'title',
  frame: { x: 50, y: 50, w: 500, h: 80, rotation_deg: 0 }, text: 'AI 候选标题', locked: false,
  style: { font_family_id: 'noto-sans-sc', font_size_pt: 30, font_weight: 400,
    color: '#172A42', align: 'left', vertical_align: 'top', line_height: 1.2, padding_pt: 0 } }] };

function scene(revision: string, version: number, content: SlideScene): SceneResponse {
  return { page_id: 'page-1', revision_id: revision, page_version: version,
    scene_hash: `hash-${revision}`, scene: content, asset_urls: {} };
}

function project(): SceneProject {
  return { project_id: 'project-1', title: '历史测试', prompt: '', editor_mode: 'scene_v1',
    project_version: 1, canvas: blank.canvas, font_manifest_id: 'fonts-v1', active_plan_id: null,
    model_config: {}, pages: [{ page_id: 'page-1', order_index: 0, outline_content: { title: '第一页' },
      page_version: mocks.accepted ? 2 : 1, revision_id: mocks.accepted ? 'candidate-rev' : 'base-rev' }] };
}

describe('SceneEditorPage candidate history', () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.saveSceneCommands.mockReset(); localStorage.clear(); sessionStorage.clear(); mocks.accepted = false;
    mocks.getSceneProject.mockImplementation(async () => project());
    mocks.getPageScene.mockImplementation(async (_projectId: string, _pageId: string, revisionId?: string) =>
      revisionId || mocks.accepted ? scene('candidate-rev', 2, proposed) : scene('base-rev', 1, blank));
    mocks.getCandidates.mockImplementation(async () => [{ candidate_id: 'candidate-1', page_id: 'page-1',
      state: mocks.accepted ? 'accepted' : 'pending', revision_id: 'candidate-rev', change_summary: {} }]);
    mocks.listModelCredentials.mockResolvedValue([]);
    mocks.listSceneProjectTasks.mockResolvedValue([]);
    mocks.getSceneFontManifest.mockResolvedValue({ font_manifest_id: 'fonts-v1',
      family: 'Noto Sans CJK SC', style: 'Regular', format: 'OpenType',
      sha256: 'fixed-font-hash', download_url: '/api/v2/fonts/noto-sans-sc?download=1' });
    mocks.getSnapshotPreflight.mockResolvedValue({ ready: true, page_count: 1, blockers: [] });
    mocks.createSnapshot.mockResolvedValue({ snapshot_id: 'snapshot-1' });
    mocks.createExport.mockRejectedValue(new Error('test export stop'));
    mocks.startSceneAiEdit.mockRejectedValue(new Error('test AI stop'));
    mocks.acceptCandidate.mockImplementation(async () => { mocks.accepted = true; return {}; });
    mocks.addScenePage.mockResolvedValue({ page_id: 'page-copy', order_index: 1 });
    mocks.restoreScene.mockResolvedValue(scene('restored-rev', 3, blank));
  });

  it('reopens a completed outline candidate from persisted task history after refresh', async () => {
    mocks.listSceneProjectTasks.mockResolvedValue([{ task_id: 'historical-outline', operation: 'generate_outline',
      state: 'succeeded', error_code: null, possible_charge: false }]);
    mocks.getSceneTask.mockResolvedValue({ state: 'succeeded', error_code: null, possible_charge: false,
      result: { draft_asset_id: 'historical-draft' } });
    mocks.getOutlineDraft.mockResolvedValue({ base_project_version: 0, pages: [{ role: 'content', title: '历史大纲候选',
      points: ['保留正文'], facts_needed: ['待提供真实数字'], sources: [] }] });
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '查看大纲候选' }));
    await waitFor(() => expect(screen.getByRole('region', { name: 'AI 大纲候选' })).toHaveTextContent('历史大纲候选'));
    expect(screen.getByLabelText('第 1 页标题')).toHaveValue('第一页');
    expect(mocks.getOutlineDraft).toHaveBeenCalledWith('project-1', 'historical-draft');
    expect(mocks.saveSceneCommands).not.toHaveBeenCalled();
  });

  it('keeps an IME draft when reference-removal refresh finishes after editing starts', async () => {
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    expect(mocks.templateProps?.projectVersion).toBe(1);
    expect(mocks.templateProps?.canBind()).toBe(true);
    let finish!: (response: SceneResponse) => void;
    mocks.getPageScene.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    act(() => mocks.templateProps!.onProjectUpdated({ ...project(), project_version: 2 }));
    fireEvent.doubleClick(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!);
    const editor = container.querySelector('.scene-text-overlay')!;
    fireEvent.compositionStart(editor);
    expect(mocks.templateProps?.canBind()).toBe(false);
    fireEvent.change(editor, { target: { value: '删除响应在途时的草稿' } });
    await act(async () => finish(scene('base-rev', 2, proposed)));
    expect(container.querySelector('.scene-text-overlay')).toHaveValue('删除响应在途时的草稿');
    expect(mocks.saveSceneCommands).not.toHaveBeenCalled();
  });

  it.each(['blur', 'debounce'])('preserves typing on a new object before its add save resolves (%s)', async trigger => {
    let finishAdd!: (response: SceneResponse) => void;
    mocks.saveSceneCommands.mockImplementationOnce(() => new Promise(resolve => { finishAdd = resolve; }));
    const view = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(view.container.querySelector('.scene-workspace')).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: '文字' }));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1));
    const add = mocks.saveSceneCommands.mock.calls[0][3][0] as Command;
    if (add.op !== 'add_element' || add.element.kind !== 'text') throw new Error('Expected new text');
    const original = structuredClone(add.element);
    const changed = { ...original, text: '新增对象保存期间输入的中文' };
    const final = scene('edited-rev', 3, { ...blank, elements: [changed] });
    mocks.saveSceneCommands.mockResolvedValue(final);
    const editor = view.container.querySelector('textarea[rows="6"]')!;
    fireEvent.change(editor, { target: { value: changed.text } });
    if (trigger === 'blur') fireEvent.blur(editor);
    else await act(async () => { await new Promise(resolve => setTimeout(resolve, 850)); });
    expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('banana-scene-draft-v1:project-1:page-1')).toContain(changed.text);
    await act(async () => finishAdd(scene('added-rev', 2, { ...blank, elements: [original] })));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(2));
    expect(mocks.saveSceneCommands).toHaveBeenLastCalledWith('project-1', 'page-1',
      expect.objectContaining({ revision_id: 'added-rev', page_version: 2 }),
      [{ op: 'set_text', element_id: original.id, text: changed.text }]);
    await waitFor(() => expect(view.container.querySelector('.scene-save-state')).toHaveTextContent('已保存'));
    expect(view.container.querySelector('.scene-canvas-scaled')).toHaveTextContent(changed.text);
    view.unmount();
    mocks.getPageScene.mockResolvedValue(final);
    const reloaded = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(reloaded.container.querySelector('.scene-canvas-scaled')).toHaveTextContent(changed.text));
  });

  it('does not enqueue an unchanged property on blur before Undo', async () => {
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    const changed = structuredClone(proposed);
    (changed.elements[0] as TextElement).text = '手工修改';
    mocks.saveSceneCommands.mockResolvedValue(scene('edited-rev', 2, changed));
    mocks.restoreScene.mockResolvedValue(scene('restored-rev', 3, proposed));
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.pointerDown(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!, { shiftKey: true });
    const editor = container.querySelector('textarea[rows="6"]')!;
    fireEvent.change(editor, { target: { value: '手工修改' } });
    fireEvent.blur(editor);
    await waitFor(() => expect(container.querySelector('.scene-save-state')).toHaveTextContent('已保存'));
    expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1);
    fireEvent.blur(screen.getByLabelText('字号 (pt)'));
    fireEvent.click(screen.getByTitle('撤销'));
    await waitFor(() => expect(mocks.restoreScene).toHaveBeenCalledWith('project-1', 'page-1',
      expect.objectContaining({ revision_id: 'edited-rev' }), 'base-rev'));
    expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled')).toHaveTextContent('AI 候选标题'));
  });

  it('records the previous head when accepting a candidate so Undo restores it', async () => {
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '查看 AI 候选' }));
    expect(await screen.findByText('此历史候选未记录对象变更清单，请预览核对。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接受候选' })).toBeDisabled();
    fireEvent.click(await screen.findByRole('button', { name: '预览' }));
    fireEvent.click(await screen.findByRole('button', { name: '接受候选' }));
    await waitFor(() => expect(mocks.acceptCandidate).toHaveBeenCalledWith('project-1', 'candidate-1'));
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled')?.textContent).toContain('AI 候选标题'));
    fireEvent.click(container.querySelector('[aria-label="AI 候选"] .scene-modal-close')!);
    fireEvent.click(screen.getByTitle('撤销'));
    await waitFor(() => expect(mocks.restoreScene).toHaveBeenCalledWith('project-1', 'page-1',
      expect.objectContaining({ revision_id: 'candidate-rev' }), 'base-rev'));
  });

  it.each([false, true])('undoes and redoes an explicit lock gesture from locked=%s', async (initiallyLocked) => {
    const initial = structuredClone(proposed);
    initial.elements[0].locked = initiallyLocked;
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, initial));
    let completeUndo: (() => void) | undefined;
    let calls = 0;
    mocks.saveSceneCommands.mockImplementation(async (_projectId: string, _pageId: string,
      base: SceneResponse, commands: Command[]) => {
      const next = structuredClone(base.scene);
      for (const command of commands) {
        if (command.op !== 'set_locked') throw new Error('Unexpected edit during history operation');
        next.elements.find(item => item.id === command.element_id)!.locked = command.locked;
      }
      calls++;
      if (calls === 2) await new Promise<void>(resolve => { completeUndo = resolve; });
      return scene(`lock-rev-${base.page_version + 1}`, base.page_version + 1, next);
    });
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.pointerDown(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!, { shiftKey: true });
    fireEvent.click(screen.getByRole('button', { name: initiallyLocked ? '解锁' : '锁定' }));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(container.querySelector('.scene-save-state')).toHaveTextContent('已保存'));
    fireEvent.click(screen.getByTitle('撤销'));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(2));
    expect(mocks.saveSceneCommands).toHaveBeenLastCalledWith('project-1', 'page-1',
      expect.objectContaining({ page_version: 2 }),
      [{ op: 'set_locked', element_id: 'element-1', locked: initiallyLocked }]);
    expect(mocks.restoreScene).not.toHaveBeenCalled();
    expect(screen.getByTitle('撤销')).toBeDisabled();
    fireEvent.click(screen.getByTitle('撤销'));
    fireEvent.click(screen.getByRole('button', { name: '文字' }));
    expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('alert')).toHaveTextContent('请等待撤销或重做完成后再编辑');
    completeUndo?.();
    await waitFor(() => expect(screen.getByTitle('重做')).toBeEnabled());
    fireEvent.click(screen.getByTitle('重做'));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(3));
    expect(mocks.saveSceneCommands).toHaveBeenLastCalledWith('project-1', 'page-1',
      expect.objectContaining({ page_version: 3 }),
      [{ op: 'set_locked', element_id: 'element-1', locked: !initiallyLocked }]);
    await waitFor(() => expect(container.querySelector('.scene-save-state')).toHaveTextContent('已保存'));
    expect(container.querySelectorAll('.scene-canvas-scaled [data-element-id]')).toHaveLength(1);
  });

  it('saves pending text before locking the same object', async () => {
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    let completeTextSave: (() => void) | undefined;
    mocks.saveSceneCommands.mockImplementation(async (_projectId: string, _pageId: string,
      base: SceneResponse, commands: Command[]) => {
      const next = structuredClone(base.scene);
      const command = commands[0];
      if (command.op === 'set_text') {
        (next.elements[0] as TextElement).text = command.text;
        await new Promise<void>(resolve => { completeTextSave = resolve; });
      } else if (command.op === 'set_locked') next.elements[0].locked = command.locked;
      return scene(`saved-${base.page_version + 1}`, base.page_version + 1, next);
    });
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.pointerDown(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!, { shiftKey: true });
    fireEvent.change(screen.getByRole('textbox', { name: '内容' }), { target: { value: '锁定前的最后修改' } });
    fireEvent.click(screen.getByRole('button', { name: '锁定' }));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1));
    expect(mocks.saveSceneCommands.mock.calls[0][3]).toEqual([
      { op: 'set_text', element_id: 'element-1', text: '锁定前的最后修改' }]);
    completeTextSave?.();
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(2));
    expect(mocks.saveSceneCommands).toHaveBeenLastCalledWith('project-1', 'page-1',
      expect.objectContaining({ revision_id: 'saved-2', page_version: 2 }),
      [{ op: 'set_locked', element_id: 'element-1', locked: true }]);
    await waitFor(() => expect(container.querySelector('.scene-save-state')).toHaveTextContent('已保存'));
    expect(screen.getByRole('textbox', { name: '内容' })).toHaveValue('锁定前的最后修改');
    expect(screen.getByRole('textbox', { name: '内容' })).toBeDisabled();
  });

  it('shows a candidate preview fetch failure instead of treating it as reviewed', async () => {
    mocks.getPageScene.mockImplementation(async (_projectId: string, _pageId: string, revisionId?: string) => {
      if (revisionId) throw new Error('候选预览暂不可用');
      return scene('base-rev', 1, blank);
    });
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '查看 AI 候选' }));
    fireEvent.click(await screen.findByRole('button', { name: '预览' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('候选预览暂不可用');
    expect(screen.queryByText('当前版本与候选对照')).not.toBeInTheDocument();
  });

  it('accepts multiple reviewed page candidates in one atomic request', async () => {
    const first = project();
    mocks.getSceneProject.mockImplementation(async () => ({ ...first, pages: [...first.pages,
      { ...first.pages[0], page_id: 'page-2', order_index: 1,
        outline_content: { title: '第二页' }, revision_id: 'base-rev-2' }] }));
    mocks.getPageScene.mockImplementation(async (_projectId: string, currentPageId: string, revisionId?: string) =>
      ({ ...scene(revisionId || `base-${currentPageId}`, 1, revisionId ? proposed : blank), page_id: currentPageId }));
    mocks.getCandidates.mockImplementation(async () => ['page-1', 'page-2'].map((currentPageId, index) => ({
      candidate_id: `candidate-${index + 1}`, page_id: currentPageId,
      state: mocks.accepted ? 'accepted' : 'pending', revision_id: `candidate-rev-${index + 1}`, change_summary: {} })));
    mocks.acceptCandidates.mockImplementation(async () => {
      mocks.accepted = true;
      return ['page-1', 'page-2'].map((currentPageId, index) => ({ ...scene(`candidate-rev-${index + 1}`, 2, proposed),
        page_id: currentPageId }));
    });
    vi.stubGlobal('confirm', vi.fn(() => true));
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '查看 AI 候选' }));
    const previews = await screen.findAllByRole('button', { name: '预览' });
    fireEvent.click(previews[0]);
    expect(await screen.findByText('当前版本与候选对照')).toBeInTheDocument();
    fireEvent.click(previews[1]);
    fireEvent.click(await screen.findByRole('button', { name: '批量接受已预览候选（2 页）' }));
    await waitFor(() => expect(mocks.acceptCandidates).toHaveBeenCalledWith('project-1', ['candidate-1', 'candidate-2']));
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled')?.textContent).toContain('AI 候选标题'));
    expect(screen.queryByRole('button', { name: '批量接受已预览候选（2 页）' })).not.toBeInTheDocument();
  });

  it('offers copying the current page immediately after it', async () => {
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '复制当前页' }));
    await waitFor(() => expect(mocks.addScenePage).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: 'project-1' }),
      { insertAt: 1, copyFrom: expect.objectContaining({ page_id: 'page-1', revision_id: 'base-rev' }) }));
  });

  it('shows the exact font installation and hash needed by PPTX clients', async () => {
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    const pptx = screen.getByRole('button', { name: '导出 PPTX' });
    expect(pptx).toBeDisabled();
    expect(await screen.findByText('Noto Sans CJK SC Regular')).toBeInTheDocument();
    expect(pptx).toBeEnabled();
    expect(screen.getByText('fixed-font-hash')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '下载当前固定字体' }))
      .toHaveAttribute('href', '/api/v2/fonts/noto-sans-sc?download=1');
  });

  it('lists a preflight blocker and prevents submitting the affected page', async () => {
    mocks.getSnapshotPreflight.mockResolvedValue({ ready: false, page_count: 1,
      blockers: [{ code: 'TEXT_OVERFLOW', message: 'Text overflow', page_id: 'page-1', details: {} }] });
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    expect(await screen.findByText(/第 1 页 · TEXT_OVERFLOW/)).toBeInTheDocument();
    expect(screen.getByText(/文字溢出，请扩大文字框/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出 PDF' })).toBeDisabled();
    expect(mocks.getSnapshotPreflight).toHaveBeenCalledWith('project-1', ['page-1']);
    expect(mocks.createSnapshot).not.toHaveBeenCalled();
  });

  it('does not apply a stale blocker after the selected pages change', async () => {
    const first = project();
    mocks.getSceneProject.mockResolvedValue({ ...first, pages: [...first.pages,
      { ...first.pages[0], page_id: 'page-2', order_index: 1,
        outline_content: { title: '第二页' }, revision_id: 'revision-2' }] });
    mocks.getPageScene.mockImplementation(async (_projectId: string, currentPageId: string) =>
      ({ ...scene('base-rev', 1, blank), page_id: currentPageId }));
    mocks.getSnapshotPreflight.mockImplementation(async (_projectId: string, pageIds: string[]) =>
      pageIds.includes('page-1') ? { ready: false, page_count: pageIds.length,
        blockers: [{ code: 'TEXT_OVERFLOW', message: 'Text overflow', page_id: 'page-1', details: {} }] } :
        { ready: true, page_count: pageIds.length, blockers: [] });
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    expect(await screen.findByText(/第 1 页 · TEXT_OVERFLOW/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: '第 1 页：第一页' }));
    expect(screen.queryByText(/第 1 页 · TEXT_OVERFLOW/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出 PDF' })).toBeEnabled();
    expect(await screen.findByText(/已检查 1 页，未发现结构阻断项/)).toBeInTheDocument();
    expect(mocks.getSnapshotPreflight).toHaveBeenLastCalledWith('project-1', ['page-2']);
  });

  it('explains unsupported glyphs instead of suggesting client font installation', async () => {
    mocks.getCandidates.mockResolvedValue([]);
    mocks.createSnapshot.mockRejectedValue({ response: { data: { error: {
      code: 'FONT_GLYPH_UNAVAILABLE', details: { codepoints: ['U+1F600'] } } } } });
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    const pptx = screen.getByRole('button', { name: '导出 PPTX' });
    await waitFor(() => expect(pptx).toBeEnabled());
    fireEvent.click(pptx);
    expect(await screen.findByRole('alert')).toHaveTextContent('U+1F600');
    expect(screen.getByRole('alert')).toHaveTextContent('请修改对应文字后重新导出');
  });

  it('exports an explicit page subset and keeps the selection after reopening', async () => {
    const first = project();
    const second = { ...first.pages[0], page_id: 'page-2', order_index: 1,
      outline_content: { title: '第二页' }, revision_id: 'revision-2' };
    mocks.getSceneProject.mockResolvedValue({ ...first, pages: [...first.pages, second] });
    mocks.getPageScene.mockImplementation(async (_projectId: string, currentPageId: string) =>
      ({ ...scene('base-rev', 1, blank), page_id: currentPageId }));
    mocks.getCandidates.mockResolvedValue([]);
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '第 1 页：第一页' }));
    expect(screen.getByRole('checkbox', { name: '第 2 页：第二页' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '导出 PDF' }));
    await waitFor(() => expect(mocks.createSnapshot).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: 'project-1' }), undefined, ['page-2']));
    expect(await screen.findByRole('alert')).toHaveTextContent('test export stop');
    fireEvent.click(screen.getByRole('dialog', { name: '确认导出' }).querySelector('.scene-modal-close')!);
    fireEvent.click(screen.getByRole('button', { name: '导出' }));
    expect(screen.getByRole('checkbox', { name: '第 1 页：第一页' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: '第 2 页：第二页' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '清空' }));
    expect(screen.getByRole('button', { name: '导出 PDF' })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole('link', { name: '下载当前固定字体' })).toBeInTheDocument());
  });

  it('flushes pending text before freezing an export snapshot', async () => {
    const changed: SlideScene = structuredClone(proposed);
    (changed.elements[0] as TextElement).text = '已自动保存';
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    mocks.getCandidates.mockResolvedValue([]);
    let completeSave: ((value: SceneResponse) => void) | undefined;
    mocks.saveSceneCommands.mockImplementation(() => new Promise<SceneResponse>(resolve => { completeSave = resolve; }));
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.doubleClick(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!);
    fireEvent.change(container.querySelector('.scene-text-overlay')!, { target: { value: '已自动保存' } });
    fireEvent.click(screen.getByRole('button', { name: '导出' }));
    fireEvent.click(screen.getByRole('button', { name: '导出 PDF' }));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalled());
    expect(mocks.createSnapshot).not.toHaveBeenCalled();
    completeSave?.(scene('saved-rev', 2, changed));
    await waitFor(() => expect(mocks.createSnapshot).toHaveBeenCalledWith(
      expect.objectContaining({ pages: [expect.objectContaining({
        revision_id: 'saved-rev', page_version: 2 })] }), undefined, ['page-1']));
    expect(await screen.findByRole('alert')).toHaveTextContent('test export stop');
  });

  it('does not create a snapshot when the pending save fails', async () => {
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    mocks.saveSceneCommands.mockRejectedValue(new Error('offline'));
    mocks.getCandidates.mockResolvedValue([]);
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.doubleClick(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!);
    fireEvent.change(container.querySelector('.scene-text-overlay')!, { target: { value: '离线修改' } });
    fireEvent.click(screen.getByRole('button', { name: '导出' }));
    fireEvent.click(screen.getByRole('button', { name: '导出 PDF' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('当前页未能保存'));
    expect(mocks.createSnapshot).not.toHaveBeenCalled();
    expect(mocks.saveSceneCommands).toHaveBeenCalledTimes(1);
  });

  it('does not switch or copy a page during Chinese IME composition', async () => {
    const first = project();
    mocks.getSceneProject.mockResolvedValue({ ...first, pages: [...first.pages,
      { ...first.pages[0], page_id: 'page-2', order_index: 1,
        outline_content: { title: '第二页' } }] });
    mocks.getPageScene.mockImplementation(async (_projectId: string, currentPageId: string) =>
      ({ ...scene('base-rev', 1, proposed), page_id: currentPageId }));
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')).not.toBeNull());
    fireEvent.doubleClick(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!);
    const editor = container.querySelector('.scene-text-overlay')!;
    fireEvent.compositionStart(editor);
    expect(fireEvent.pointerDown(container.querySelectorAll('.scene-page-tile')[1])).toBe(false);
    fireEvent.click(container.querySelectorAll('.scene-page-tile')[1]);
    expect(container.querySelectorAll('.scene-page-tile')[0]).toHaveClass('active');
    expect(screen.getByRole('alert')).toHaveTextContent('请先完成当前对象操作或中文输入再切换页面');
    expect(container.querySelector('.scene-text-overlay')).not.toBeNull();
    expect(fireEvent.pointerDown(screen.getByRole('button', { name: '复制当前页' }))).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '复制当前页' }));
    expect(mocks.addScenePage).not.toHaveBeenCalled();
  });

  it('blocks exporting a selected page whose local draft is still unresolved', async () => {
    const first = project();
    mocks.getSceneProject.mockResolvedValue({ ...first, pages: [...first.pages,
      { ...first.pages[0], page_id: 'page-2', order_index: 1, outline_content: { title: '第二页' } }] });
    mocks.getPageScene.mockImplementation(async (_projectId: string, currentPageId: string) =>
      ({ ...scene('base-rev', 1, blank), page_id: currentPageId }));
    localStorage.setItem('banana-scene-draft-v1:project-1:page-2', '{"pending":true}');
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '导出' }));
    fireEvent.click(screen.getByRole('button', { name: '导出 PDF' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('第 2 页有未确认的本地草稿');
    expect(mocks.createSnapshot).not.toHaveBeenCalled();
  });

  it('passes the newly saved revision to AI edit only after text flush succeeds', async () => {
    const configured = project();
    configured.model_config = { text_model: 'text-model' };
    mocks.getSceneProject.mockResolvedValue(configured);
    mocks.getPageScene.mockResolvedValue(scene('base-rev', 1, proposed));
    mocks.listModelCredentials.mockResolvedValue([{ credential_id: 'mine', label: 'Mine',
      key_suffix: '1234', status: 'connected', base_url: 'https://model.example/v1' }]);
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    let completeSave: ((value: SceneResponse) => void) | undefined;
    mocks.saveSceneCommands.mockImplementation(() => new Promise<SceneResponse>(resolve => { completeSave = resolve; }));
    const changed = structuredClone(proposed);
    (changed.elements[0] as TextElement).text = 'AI 前保存';
    const { container } = render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText('模型凭据')).toHaveValue('mine'));
    fireEvent.doubleClick(container.querySelector('.scene-canvas-scaled [data-element-id="element-1"]')!);
    fireEvent.change(container.querySelector('.scene-text-overlay')!, { target: { value: 'AI 前保存' } });
    fireEvent.change(screen.getByLabelText('自然语言修改'), { target: { value: '精简标题' } });
    fireEvent.click(screen.getByRole('button', { name: '生成修改候选（当前页）' }));
    await waitFor(() => expect(mocks.saveSceneCommands).toHaveBeenCalled());
    expect(mocks.startSceneAiEdit).not.toHaveBeenCalled();
    completeSave?.(scene('saved-rev', 2, changed));
    await waitFor(() => expect(mocks.startSceneAiEdit).toHaveBeenCalledWith(
      'project-1', 'page-1', expect.objectContaining({ revision_id: 'saved-rev', page_version: 2 }),
      'mine', '精简标题', []));
    expect(confirm).toHaveBeenCalled();
    expect(await screen.findByRole('alert')).toHaveTextContent('test AI stop');
  });

  it('clears a cached draft already committed when its save response was lost', async () => {
    const cacheKey = 'banana-scene-draft-v1:project-1:page-1';
    localStorage.setItem(cacheKey, JSON.stringify({ base_revision_id: 'base-rev',
      commands: [[{ op: 'add_element', element: proposed.elements[0] }]],
      scene: proposed, asset_urls: {} }));
    mocks.getPageScene.mockResolvedValue(scene('saved-rev', 2, proposed));
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(screen.getByText('已保存')).toBeInTheDocument());
    expect(localStorage.getItem(cacheKey)).toBeNull();
    expect(screen.queryByText('未保存草稿 · 请确认恢复')).not.toBeInTheDocument();
  });

  it('shows page and potential image-call count before a paid generation request', async () => {
    mocks.listModelCredentials.mockResolvedValue([{ credential_id: 'mine', label: 'Mine',
      key_suffix: '1234', status: 'connected', base_url: 'https://model.example/v1' }]);
    const confirm = vi.fn((_message: string) => false);
    vi.stubGlobal('confirm', confirm);
    render(<MemoryRouter initialEntries={['/projects/project-1/editor']}>
      <Routes><Route path="/projects/:id/editor" element={<SceneEditorPage />} /></Routes>
    </MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText('模型凭据')).toHaveValue('mine'));
    fireEvent.click(screen.getByText('确认大纲并生成当前页'));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.stringContaining('生成 1 页候选')));
    expect(confirm.mock.calls[0][0]).toContain('每页预计 0–6 个图片请求');
  });
});
