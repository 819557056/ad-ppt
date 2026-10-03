import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Plus } from 'lucide-react';
import { archiveSceneProject, createSceneProject, listSceneProjects } from '@/features/editor/sceneApi';
import type { SceneProject } from '@/features/editor/sceneTypes';
import './SceneProjectsPage.css';

export function SceneProjectsPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<SceneProject[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [title, setTitle] = useState('');
  const [prompt, setPrompt] = useState('');
  const [outline, setOutline] = useState('');
  const [aspect, setAspect] = useState<'16:9' | '4:3'>('16:9');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { listSceneProjects().then(page => { setProjects(page.items); setNextCursor(page.next_cursor); })
    .catch(e => setError(e?.response?.data?.error?.message || e.message)); }, []);
  const create = async () => {
    if (!title.trim()) { setError('请填写项目名称'); return; }
    const lines = outline.split('\n').map(x => x.trim()).filter(Boolean);
    const pages = lines.length ? lines.map(line => ({ title: line.replace(/^[-\d.、.\s]+/, ''), points: [] as string[] }))
      : [{ title: '新页面', points: [] as string[] }];
    setBusy(true);
    try { const project = await createSceneProject(title.trim(), prompt.trim(), pages,
      aspect === '16:9' ? { width_pt: 960, height_pt: 540 } : { width_pt: 720, height_pt: 540 });
      navigate(`/projects/${project.project_id}/editor`); }
    catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
    finally { setBusy(false); }
  };
  return <div className="scene-projects-page">
    <header><button onClick={() => navigate('/')}><ArrowLeft size={18} /> 返回蕉幻</button><strong>蕉幻 · 对象编辑</strong></header>
    <div className="scene-projects-layout"><section className="scene-create">
      <span className="scene-projects-label">创建可编辑演示文稿</span>
      <h1>从空白画布开始，<br />把每一页变成可编辑的作品。</h1>
      <p>文字保持为原生对象。图片、图表和图标可整体移动、缩放与替换。</p>
      <label>项目名称<input value={title} onChange={e => setTitle(e.target.value)} placeholder="例如：第三季度团队工作汇报" maxLength={255} /></label>
      <label>创作需求（可选）<textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="这份演示文稿面向谁？需要表达什么？" rows={3} /></label>
      <label>页面标题（每行一页，可选）<textarea value={outline} onChange={e => setOutline(e.target.value)}
        placeholder={'季度概览\n重点成果\n下一步计划'} rows={5} /></label>
      <label>页面画幅<select value={aspect} onChange={e => setAspect(e.target.value as '16:9' | '4:3')}>
        <option value="16:9">16:9 宽屏</option><option value="4:3">4:3 标准</option>
      </select></label>
      <button className="scene-create-button" disabled={busy} onClick={() => void create()}><Plus size={18} /> {busy ? '创建中…' : '创建项目'}</button>
      {error && <p className="scene-projects-error" role="alert">{error}</p>}
    </section><section className="scene-recent"><h2>继续编辑</h2>
      {projects.length === 0 ? <div className="scene-empty">尚无对象编辑项目。左侧创建后即可开始添加文字和图片。</div>
        : projects.map(project => <div className="scene-recent-row" key={project.project_id}><button onClick={() => navigate(`/projects/${project.project_id}/editor`)}>
          <span className="scene-recent-icon">{project.title.slice(0, 1)}</span><span><strong>{project.title}</strong><small>{project.pages?.length ?? '—'} 页 · Scene v1</small></span><ArrowRight size={18} />
        </button><button className="scene-recent-archive" onClick={async () => {
          if (!window.confirm(`归档项目「${project.title}」？`)) return;
          try { await archiveSceneProject(project.project_id); setProjects(items => items.filter(p => p.project_id !== project.project_id)); }
          catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
        }}>归档</button></div>)}
      {nextCursor && <button className="scene-load-more" onClick={async () => {
        try { const page = await listSceneProjects(nextCursor);
          setProjects(items => [...items, ...page.items]); setNextCursor(page.next_cursor); }
        catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
      }}>加载更多项目</button>}
    </section></div>
  </div>;
}
