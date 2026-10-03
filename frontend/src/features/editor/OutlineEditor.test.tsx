import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OutlineEditor } from './OutlineEditor';
import type { SceneProject } from './sceneTypes';
import type { OutlineDraft } from './sceneApi';

const api = vi.hoisted(() => ({ getOutlineDraft: vi.fn(), getSceneProject: vi.fn(), getSceneTask: vi.fn(),
  saveSceneOutline: vi.fn(), startOutlineTask: vi.fn() }));
vi.mock('./sceneApi', () => api);
const project = (version = 1): SceneProject => ({ project_id: 'project', project_version: version,
  title: '业务汇报', prompt: '只使用已提供事实', editor_mode: 'scene_v1', active_plan_id: null,
  canvas: { width_pt: 960, height_pt: 540 }, font_manifest_id: 'fonts-v1', model_config: {},
  pages: [{ page_id: 'page-1', page_version: 1, revision_id: 'revision', order_index: 0,
    outline_content: { title: '原始页面', points: ['原始要点'], role: 'content', facts_needed: ['缺少收入'], sources: ['台账'] } }] });
const draft = (version: number | null = 1): OutlineDraft => ({ base_project_version: version,
  pages: [{ title: 'AI 页面', role: 'agenda', points: Array(20).fill('候选要点'),
    facts_needed: Array(20).fill('缺少成本'), sources: ['用户提供的资料'] }] });
const props = () => ({ project: project(), credentialId: 'fake', textModel: 'fake-text',
  configureProject: vi.fn().mockResolvedValue(project()), canSave: () => true,
  onSaved: vi.fn().mockResolvedValue(undefined), onClose: vi.fn(), onTask: vi.fn() });
const latest = (version: number): SceneProject => ({ ...project(version),
  pages: [{ ...project().pages[0], outline_content: { title: `服务器 v${version}`, points: [] } }] });
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

describe('OutlineEditor candidate and planning-version boundary', () => {
  beforeEach(() => {
    vi.resetAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.getSceneProject.mockResolvedValue(project());
    api.startOutlineTask.mockResolvedValue({ task_id: 'outline-task' });
    api.getSceneTask.mockResolvedValue({ state: 'succeeded', error_code: null, possible_charge: true,
      result: { draft_asset_id: 'draft' } });
    api.getOutlineDraft.mockResolvedValue(draft());
    api.saveSceneOutline.mockResolvedValue(project(2));
  });

  it('does not overwrite typing when AI returns; separately saves 20 points and 20 missing facts', async () => {
    let finish!: (value: OutlineDraft) => void;
    api.getOutlineDraft.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<OutlineEditor {...props()} />);
    click('AI 草拟大纲');
    await waitFor(() => expect(api.getOutlineDraft).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('第 1 页标题'), { target: { value: '在途手工修改' } });
    await act(async () => finish(draft()));
    expect(screen.getByLabelText('第 1 页标题')).toHaveValue('在途手工修改');
    expect(screen.getByRole('region', { name: 'AI 大纲候选' })).toHaveTextContent('AI 页面');
    expect(api.saveSceneOutline).not.toHaveBeenCalled();
    click('已比较，载入候选继续编辑');
    await waitFor(() => expect(screen.getByLabelText('第 1 页标题')).toHaveValue('AI 页面'));
    click('保存大纲');
    await waitFor(() => expect(api.saveSceneOutline).toHaveBeenCalledWith(project(), [{
      page_id: 'page-1', outline: draft().pages[0] }]));
  });

  it('keeps its opened version after a parent refresh and requires a fresh comparison after 409', async () => {
    const p = props(); const view = render(<OutlineEditor {...p} />);
    fireEvent.change(screen.getByLabelText('第 1 页标题'), { target: { value: '本地草稿' } });
    view.rerender(<OutlineEditor {...p} project={latest(3)} />);
    api.saveSceneOutline.mockRejectedValueOnce({ response: { status: 409, data: { error: { message: 'Project changed' } } } });
    api.getSceneProject.mockResolvedValue(latest(3));
    click('保存大纲');
    await screen.findByRole('region', { name: '大纲版本冲突' });
    expect(api.saveSceneOutline.mock.calls[0][0].project_version).toBe(1);
    expect(screen.getByLabelText('第 1 页标题')).toHaveValue('本地草稿');
    expect(screen.getByRole('button', { name: '保存大纲' })).toBeDisabled();
    api.getSceneProject.mockResolvedValue(latest(4));
    click('已比较，基于新版本继续编辑');
    await screen.findByText('服务器再次变化，请重新比较后确认。');
    expect(screen.getByRole('region', { name: '服务器最新大纲' })).toHaveTextContent('服务器 v4');
    click('已比较，基于新版本继续编辑');
    await waitFor(() => expect(screen.queryByRole('region', { name: '大纲版本冲突' })).toBeNull());
    click('保存大纲');
    await waitFor(() => expect(api.saveSceneOutline).toHaveBeenCalledTimes(2));
    expect(api.saveSceneOutline.mock.calls[1][0].project_version).toBe(4);
    expect(api.saveSceneOutline.mock.calls[1][1][0].outline.title).toBe('本地草稿');
  });

  it.each([0, null])('requires explicit rebase for a stale/unknown draft baseline %s', async version => {
    api.getOutlineDraft.mockResolvedValue(draft(version));
    api.getSceneProject.mockResolvedValue(latest(3));
    const p = props(); render(<OutlineEditor {...p} taskId="history-task" />);
    await screen.findByRole('region', { name: 'AI 大纲候选' });
    click('已比较，载入候选继续编辑');
    await screen.findByRole('region', { name: '大纲版本冲突' });
    expect(screen.getByLabelText('第 1 页标题')).toHaveValue('原始页面');
    expect(api.startOutlineTask).not.toHaveBeenCalled();
    expect(p.configureProject).not.toHaveBeenCalled();
    click('已比较，基于新版本继续编辑');
    await waitFor(() => expect(screen.getByLabelText('第 1 页标题')).toHaveValue('AI 页面'));
    click('保存大纲');
    await waitFor(() => expect(api.saveSceneOutline).toHaveBeenCalled());
    expect(api.saveSceneOutline.mock.calls[0][0].project_version).toBe(3);
  });

  it('preserves structured metadata and keeps add/delete local until one save', async () => {
    render(<OutlineEditor {...props()} />);
    expect(screen.getByLabelText('第 1 页角色')).toHaveValue('content');
    expect(screen.getByLabelText('第 1 页待补事实')).toHaveValue('缺少收入');
    expect(screen.getByLabelText('第 1 页来源')).toHaveValue('台账');
    click('添加页面');
    fireEvent.change(screen.getByLabelText('第 2 页标题'), { target: { value: '新页' } });
    click('删除第 1 页');
    expect(api.saveSceneOutline).not.toHaveBeenCalled();
    click('保存大纲');
    await waitFor(() => expect(api.saveSceneOutline).toHaveBeenCalled());
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('移除 1 页'));
    expect(api.saveSceneOutline.mock.calls[0][1]).toEqual([{ page_id: undefined,
      outline: { title: '新页', points: [], role: 'unknown', facts_needed: [], sources: [] } }]);
  });

  it('does not submit a paid task when the editor closes while configuration is pending', async () => {
    let finish!: (value: SceneProject) => void;
    const p = props(); p.configureProject.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<OutlineEditor {...p} />);
    click('AI 草拟大纲');
    await waitFor(() => expect(p.configureProject).toHaveBeenCalledTimes(1));
    view.unmount(); await act(async () => finish(project()));
    expect(api.startOutlineTask).not.toHaveBeenCalled();
  });

  it('never reads a missing draft after a failed task or retries it itself', async () => {
    api.getSceneTask.mockResolvedValue({ state: 'outcome_unknown', error_code: 'MODEL_OUTCOME_UNKNOWN', possible_charge: true });
    render(<OutlineEditor {...props()} taskId="unknown-task" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('不会自动重新计费');
    expect(api.getOutlineDraft).not.toHaveBeenCalled();
    expect(api.startOutlineTask).not.toHaveBeenCalled();
  });
});
