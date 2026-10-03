import { useEffect, useRef, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { getOutlineDraft, getSceneProject, getSceneTask, saveSceneOutline, startOutlineTask,
  type OutlineDraft } from './sceneApi';
import type { OutlineContent, OutlineRole, SceneProject } from './sceneTypes';

type Row = { local_id: string; page_id?: string; title: string; points: string;
  role: OutlineRole; facts_needed: string; sources: string };
type TaskStatus = { task_id: string; state: string; error_code: string | null; possible_charge: boolean; operation: string };
type Props = { project: SceneProject; credentialId: string; textModel: string; taskId?: string;
  configureProject: () => Promise<SceneProject>; canSave: () => boolean;
  onSaved: (project: SceneProject) => Promise<void>; onClose: () => void;
  onTask: (task: TaskStatus) => void };
const roles: Record<OutlineRole, string> = { unknown: '未指定', cover: '封面', agenda: '目录',
  section: '章节', content: '正文', closing: '结束页' };
const lines = (value: string) => value.split('\n').map(x => x.trim()).filter(Boolean);
function row(content: OutlineContent | null, pageId?: string): Row {
  return { local_id: crypto.randomUUID(), page_id: pageId, title: content?.title || '',
    points: (content?.points || []).join('\n'), role: content?.role || 'unknown',
    facts_needed: (content?.facts_needed || []).join('\n'), sources: (content?.sources || []).join('\n') };
}
function content(item: Row): OutlineContent {
  return { title: item.title, points: lines(item.points), role: item.role,
    facts_needed: lines(item.facts_needed), sources: lines(item.sources) };
}
function Summary({ pages, label }: { pages: OutlineContent[]; label: string }) {
  return <section aria-label={label}><h3>{label}</h3><ol>{pages.map((page, index) => <li key={index}>
    <strong>{page.title || '未命名页面'}</strong><small>{roles[page.role || 'unknown']}</small>
    <ul>{page.points?.map((point, i) => <li key={i}>{point}</li>)}</ul>
    {!!page.facts_needed?.length && <p>待补事实：{page.facts_needed.join('；')}</p>}
    {!!page.sources?.length && <p>来源（未核验）：{page.sources.join('；')}</p>}
  </li>)}</ol></section>;
}

export function OutlineEditor(props: Props) {
  const [baseline, setBaseline] = useState(props.project);
  const [rows, setRows] = useState(() => props.project.pages.map(page => row(page.outline_content, page.page_id)));
  const [brief, setBrief] = useState(props.project.prompt || '');
  const [count, setCount] = useState(10);
  const [candidate, setCandidate] = useState<OutlineDraft | null>(null);
  const [conflict, setConflict] = useState<{ current: SceneProject; draft: OutlineDraft | null } | null>(null);
  const [busy, setBusy] = useState<'generate' | 'save' | 'compare' | null>(null);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const readTask = async (taskId: string, poll: boolean) => {
    for (let i = 0; i < (poll ? 180 : 1); i++) {
      const result = await getSceneTask(taskId);
      if (!mounted.current) return;
      props.onTask({ task_id: taskId, state: result.state, error_code: result.error_code,
        possible_charge: result.possible_charge, operation: 'generate_outline' });
      if (result.state === 'succeeded') {
        const draft = await getOutlineDraft(props.project.project_id, result.result.draft_asset_id);
        if (mounted.current) setCandidate(draft); // Never replace an in-progress manual draft.
        return;
      }
      if (['failed', 'outcome_unknown', 'cancelled'].includes(result.state)) {
        throw new Error(`大纲任务未成功：${result.error_code || result.state}。请在任务列表处理，不会自动重新计费。`);
      }
      if (!poll) break;
      await new Promise(resolve => setTimeout(resolve, 2000));
      if (!mounted.current) return;
    }
    throw new Error('大纲任务仍在运行，可稍后从任务列表查看；不要重复生成。');
  };
  useEffect(() => {
    if (!props.taskId) return;
    let active = true;
    pending.current = true; setBusy('compare');
    void readTask(props.taskId, false).catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) { pending.current = false; setBusy(null); } });
    return () => { active = false; };
  }, [props.taskId]);
  const run = async (kind: NonNullable<typeof busy>, action: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true; setBusy(kind); setError('');
    try { await action(); }
    catch (e: any) { if (mounted.current) setError(e?.response?.data?.error?.message || e.message); }
    finally { pending.current = false; if (mounted.current) setBusy(null); }
  };
  const generate = () => run('generate', async () => {
    if (!props.credentialId || !brief.trim()) throw new Error('请输入需求并选择自己的模型 Key');
    if (!Number.isInteger(count) || count < 1 || count > 30) throw new Error('大纲总页数须在 1–30 页之间');
    if (!window.confirm(`将使用你自己的模型 Key 和文本模型 ${props.textModel.trim() || '（未配置）'} 生成 ${count} 页大纲候选；可能计费，费用以模型网关账单为准。继续吗？`)) return;
    const configured = await props.configureProject();
    if (!mounted.current) return;
    // Updating model configuration alone may advance this very baseline.
    // Never adopt a newer outline baseline merely because the parent refreshed.
    if (baseline.project_version === props.project.project_version) setBaseline(configured);
    const task = await startOutlineTask(configured, props.credentialId, brief.trim(), count);
    props.onTask({ task_id: task.task_id, state: 'queued', error_code: null, possible_charge: false, operation: 'generate_outline' });
    if (mounted.current) await readTask(task.task_id, true);
  });
  const loadCandidate = () => run('compare', async () => {
    if (!candidate) return;
    const latest = await getSceneProject(baseline.project_id);
    if (!mounted.current) return;
    if (candidate.base_project_version == null || latest.project_version !== candidate.base_project_version) {
      setConflict({ current: latest, draft: candidate }); return;
    }
    setRows(candidate.pages.map((page, index) => row(page, latest.pages[index]?.page_id)));
    setBaseline(latest); setCandidate(null); setConflict(null);
  });
  const rebase = () => run('compare', async () => {
    if (!conflict) return;
    const latest = await getSceneProject(baseline.project_id);
    if (!mounted.current) return;
    if (latest.project_version !== conflict.current.project_version) {
      setConflict({ ...conflict, current: latest });
      throw new Error('服务器再次变化，请重新比较后确认。');
    }
    if (conflict.draft) {
      setRows(conflict.draft.pages.map((page, index) => row(page, latest.pages[index]?.page_id)));
      setCandidate(null);
    } else {
      const existing = new Set(latest.pages.map(page => page.page_id));
      setRows(items => items.map(item => ({ ...item, page_id: item.page_id && existing.has(item.page_id) ? item.page_id : undefined })));
    }
    setBaseline(latest); setConflict(null);
  });
  const save = () => run('save', async () => {
    if (!props.canSave()) throw new Error('请先完成当前页面编辑并保存');
    if (conflict) throw new Error('请先比较并确认服务器的新版本');
    if (rows.some(item => [item.points, item.facts_needed, item.sources].some(value =>
      lines(value).length > 20 || lines(value).some(line => line.length > 1000)))) {
      throw new Error('每页要点、待补事实、来源分别最多 20 条，每条不超过 1000 字。');
    }
    const kept = new Set(rows.map(item => item.page_id).filter(Boolean));
    const removed = baseline.pages.filter(page => !kept.has(page.page_id));
    if (removed.length && !window.confirm(`本次大纲将移除 ${removed.length} 页；历史版本和快照保留。确认保存？`)) return;
    try {
      const updated = await saveSceneOutline(baseline, rows.map(item => ({ page_id: item.page_id, outline: content(item) })));
      if (!mounted.current) return;
      await props.onSaved(updated); props.onClose();
    } catch (e: any) {
      if (e?.response?.status === 409) {
        const latest = await getSceneProject(baseline.project_id);
        if (mounted.current) setConflict({ current: latest, draft: null });
      }
      throw e;
    }
  });
  const update = (id: string, field: keyof OutlineContent, value: string) =>
    setRows(items => items.map(item => item.local_id === id ? { ...item, [field]: value } : item));
  return <div className="scene-modal-backdrop"><div className="scene-modal scene-outline-modal" role="dialog" aria-modal="true" aria-label="编辑大纲">
    <button className="scene-modal-close" aria-label="关闭大纲" disabled={busy === 'save'} onClick={props.onClose}><X size={18} /></button>
    <h2>编辑大纲与页序</h2><div className="scene-outline-body"><p>AI 结果先比较再载入；保存大纲不改写页面对象。来源为用户或模型提供的线索，尚未经核验。</p>
    <div className="scene-outline-ai"><label>演示需求<textarea rows={2} value={brief} onChange={e => setBrief(e.target.value)} /></label>
      <label>总页数<input type="number" min="1" max="30" value={count} onChange={e => setCount(Number(e.target.value))} /></label>
      <button disabled={!!busy} onClick={() => void generate()}>{busy === 'generate' ? 'AI 草拟中…' : 'AI 草拟大纲'}</button></div>
    {busy === 'generate' && <p role="status">关闭窗口不会取消已提交的任务；可从任务列表查看大纲候选。</p>}
    {error && <p className="scene-outline-error" role="alert">{error}</p>}
    {candidate && !conflict && <details className="scene-outline-review" open><summary>大纲候选 · 尚未覆盖编辑内容</summary>
      <div className="scene-outline-comparison"><Summary label="正在编辑的大纲" pages={rows.map(content)} />
        <Summary label="AI 大纲候选" pages={candidate.pages} /></div>
      <button disabled={!!busy} onClick={() => void loadCandidate()}>已比较，载入候选继续编辑</button>
    </details>}
    {conflict && <section className="scene-outline-review" aria-label="大纲版本冲突"><h3>项目已变化 · 请比较新版本</h3>
      <p>不会自动保存。确认后以服务器版本 {conflict.current.project_version} 继续编辑；未保留的服务器页面将在保存时移除。</p>
      <div className="scene-outline-comparison"><Summary label="服务器最新大纲" pages={conflict.current.pages.map(page => page.outline_content || {})} />
        <Summary label={conflict.draft ? '待载入候选' : '保留的本地大纲'} pages={conflict.draft?.pages || rows.map(content)} /></div>
      <button disabled={!!busy} onClick={() => void rebase()}>已比较，基于新版本继续编辑</button>
    </section>}
    <fieldset className="scene-outline-fields" disabled={busy === 'save' || busy === 'compare'}><div className="scene-outline-list">
      {rows.map((item, index) => <div className="scene-outline-row" key={item.local_id}>
        <span>{index + 1}</span><div><input aria-label={`第 ${index + 1} 页标题`} value={item.title} maxLength={500}
          onChange={e => update(item.local_id, 'title', e.target.value)} placeholder="页面标题" />
          <textarea aria-label={`第 ${index + 1} 页要点`} rows={2} value={item.points} onChange={e => update(item.local_id, 'points', e.target.value)} placeholder="每行一个要点，最多 20 条" />
          <label>页面角色<select aria-label={`第 ${index + 1} 页角色`} value={item.role} onChange={e => update(item.local_id, 'role', e.target.value)}>
            {Object.entries(roles).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <details><summary>待补事实与来源</summary><label>待补事实<textarea aria-label={`第 ${index + 1} 页待补事实`} rows={2} value={item.facts_needed}
            onChange={e => update(item.local_id, 'facts_needed', e.target.value)} placeholder="每行一项，不能用虚构数值代替" /></label>
            <label>来源（未核验）<textarea aria-label={`第 ${index + 1} 页来源`} rows={2} value={item.sources}
              onChange={e => update(item.local_id, 'sources', e.target.value)} placeholder="仅填写已提供的资料出处；每行一项" /></label></details></div>
        <div className="scene-outline-actions"><button aria-label={`上移第 ${index + 1} 页`} disabled={index === 0} onClick={() => setRows(items => {
          const next = [...items]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; return next;
        })}>↑</button><button aria-label={`下移第 ${index + 1} 页`} disabled={index === rows.length - 1} onClick={() => setRows(items => {
          const next = [...items]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; return next;
        })}>↓</button><button aria-label={`删除第 ${index + 1} 页`} disabled={rows.length <= 1}
          onClick={() => setRows(items => items.filter(x => x.local_id !== item.local_id))}>删除</button></div>
      </div>)}
    </div></fieldset></div>
    <div className="scene-modal-actions"><button disabled={!!busy || rows.length >= 30} onClick={() => setRows(items => [...items, row(null)])}><Plus size={16} /> 添加页面</button>
      <button disabled={!!busy || !!conflict} onClick={() => void save()}>保存大纲</button></div>
  </div></div>;
}
