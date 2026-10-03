import { useEffect, useRef, useState } from 'react';
import { analyzeTemplate, assetUrl, bindPageTemplate, deleteTemplateReference, finishTemplateAnalysisRequest,
  finishTemplateUploadRequest, getSceneTaskGroup,
  getSceneProject, getTemplateDocument, listTemplateReferences, saveTemplateProfile,
  uploadTemplate, type TemplateDocumentStatus, type TemplateReference } from '@/features/editor/sceneApi';
import type { ScenePage, SceneProject } from '@/features/editor/sceneTypes';
import { suggestTemplateMatches } from './templateAutoMatch';

type Props = { projectId: string; projectVersion: number; page: ScenePage; pages: ScenePage[]; credentialId: string;
  onPageUpdated: (page: ScenePage) => void; onProjectUpdated: (project: SceneProject) => void;
  onError: (message: string) => void;
  canBind: () => boolean };

export function TemplateImport({ projectId, projectVersion, page, pages, credentialId, onPageUpdated,
  onProjectUpdated, onError, canBind }: Props) {
  const [items, setItems] = useState<TemplateReference[]>([]);
  const [busy, setBusy] = useState('');
  const removing = useRef(false);
  const [importReport, setImportReport] = useState<TemplateDocumentStatus | null>(null);
  const latestDocumentId = useRef<string | null>(null);
  const [styleText, setStyleText] = useState(page.template_style_text || '');
  const [editing, setEditing] = useState<string | null>(null);
  const [role, setRole] = useState('unknown');
  const [palette, setPalette] = useState({ background: '#FFFFFF', text: '#172A42', accent: '#F7CC35' });
  const [density, setDensity] = useState('medium');
  const [decorativeHints, setDecorativeHints] = useState('');
  const importDocumentKey = `banana-scene-template-document-v1:${projectId}`;
  useEffect(() => {
    let active = true;
    setImportReport(null);
    listTemplateReferences(projectId).then(references => { if (active) setItems(references); }).catch(() => {});
    try {
      const lastDocumentId = localStorage.getItem(importDocumentKey);
      latestDocumentId.current = lastDocumentId;
      if (lastDocumentId) getTemplateDocument(projectId, lastDocumentId)
        .then(report => { if (active && latestDocumentId.current === lastDocumentId) setImportReport(report); })
        .catch(() => { if (active && latestDocumentId.current === lastDocumentId) setImportReport(null); });
    } catch { /* Browser storage is optional. */ }
    return () => { active = false; };
  }, [projectId, importDocumentKey]);
  useEffect(() => { setStyleText(page.template_style_text || ''); }, [page.page_id, page.template_style_text]);
  const suggestions = suggestTemplateMatches(pages, items);
  const autoMatch = async () => {
    if (!canBind()) { onError('请先完成当前页保存，再应用模板匹配'); return; }
    const unbound = pages.filter(item => !item.template_asset_id).length;
    if (!suggestions.length || suggestions.length !== unbound) {
      onError('已分析的参考页不足以匹配所有未绑定页面；请先分析正文参考页或逐页手动选择'); return;
    }
    if (!window.confirm(`将按已分析的版式给 ${suggestions.length} 页设置参考模板；已手动选择的页面不会改动。继续吗？`)) return;
    let applied = 0;
    setBusy('正在应用自动匹配…');
    try {
      for (const item of suggestions) {
        if (!canBind()) throw new Error('自动匹配期间出现未保存的编辑，已停止后续页面以保留草稿');
        await bindPageTemplate(projectId, item.page, item.reference.template_asset_id,
          item.page.template_style_text || '');
        applied++;
      }
      onProjectUpdated(await getSceneProject(projectId));
      setBusy('');
    } catch (e: any) {
      try { onProjectUpdated(await getSceneProject(projectId)); } catch { /* Keep the error below. */ }
      setBusy('');
      onError(`模板自动匹配中断（已应用 ${applied}/${suggestions.length} 页）；${e?.response?.data?.error?.message || e.message}`);
    }
  };
  const upload = async (file?: File) => {
    if (!file) return;
    try {
      setBusy('正在导入模板…');
      const doc = await uploadTemplate(projectId, file);
      latestDocumentId.current = doc.document_id;
      setImportReport({ document_id: doc.document_id, status: doc.status || 'uploaded',
        source_page_count: 0, warnings: [], error_code: null, task_id: doc.task_id });
      try { localStorage.setItem(importDocumentKey, doc.document_id); } catch { /* Keep the in-memory report. */ }
      for (let i = 0; i < 120; i++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const status = await getTemplateDocument(projectId, doc.document_id);
        if (latestDocumentId.current === doc.document_id) setImportReport(status);
        if (status.status === 'preview_ready' || status.status === 'ready') {
          await finishTemplateUploadRequest(projectId, file);
          setItems(await listTemplateReferences(projectId)); setBusy(''); return;
        }
        if (status.status === 'failed') {
          await finishTemplateUploadRequest(projectId, file);
          throw new Error(`模板导入失败：${status.error_code}`);
        }
      }
      setBusy('');
    } catch (e: any) { setBusy(''); onError(e?.response?.data?.error?.message || e.message); }
  };
  const refreshImport = async () => {
    if (!importReport) return;
    try {
      const report = await getTemplateDocument(projectId, importReport.document_id);
      setImportReport(report);
      if (report.status === 'preview_ready' || report.status === 'ready')
        setItems(await listTemplateReferences(projectId));
    } catch (e: any) { onError(e?.response?.data?.error?.message || e.message); }
  };
  const bind = async (item: TemplateReference | null) => {
    if (removing.current) return;
    if (!canBind()) { onError('请等待当前页保存完成后再修改模板参考'); return; }
    if (item && item.analysis_status !== 'completed' && page.template_asset_id !== item.template_asset_id &&
        !window.confirm('此参考页未完成风格分析。生成时会把图片直接交给你的文本模型作为视觉参考；模型需支持图片输入，且可能增加费用。继续吗？')) return;
    try { onPageUpdated(await bindPageTemplate(projectId, page, item?.template_asset_id || null, styleText)); }
    catch (e: any) { onError(e?.response?.data?.error?.message || e.message); }
  };
  const analyze = async (item: TemplateReference) => {
    if (!credentialId) { onError('请先配置自己的模型 Key'); return; }
    try {
      setBusy(`分析参考页 ${item.source_page_index}…`);
      const task = await analyzeTemplate(projectId, item.template_document_id, credentialId, item.source_page_index);
      for (let i = 0; i < 120; i++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const group = await getSceneTaskGroup(task.task_group_id);
        const state = group.items[0]?.state;
        if (state === 'succeeded') {
          await finishTemplateAnalysisRequest(projectId, item.template_document_id, credentialId, item.source_page_index);
          setItems(await listTemplateReferences(projectId)); setBusy(''); return;
        }
        if (['failed', 'outcome_unknown', 'cancelled'].includes(state)) throw new Error(`风格分析失败：${group.items[0]?.error_code}`);
      }
      setBusy('分析任务仍在后台运行');
    } catch (e: any) { setBusy(''); onError(e?.response?.data?.error?.message || e.message); }
  };
  const removeReference = async (item: TemplateReference) => {
    if (removing.current || busy) return;
    if (!canBind()) { onError('请先完成当前页保存，再移除参考页'); return; }
    if (styleText !== (page.template_style_text || '')) {
      onError('请先保存风格补充说明，再移除参考页'); return;
    }
    const count = pages.filter(page => page.template_asset_id === item.template_asset_id).length;
    if (!window.confirm(`移除参考页 ${item.source_page_index}？将解除 ${count} 页的当前绑定，不改变正文和风格补充说明。历史计划、快照及素材仍会保留；此操作不会释放存储空间。`)) return;
    removing.current = true;
    setBusy('正在移除参考页…');
    try {
      const updated = await deleteTemplateReference(projectId, projectVersion, pages, item);
      setItems(current => current.filter(reference => reference.template_asset_id !== item.template_asset_id));
      if (editing === item.template_asset_id) setEditing(null);
      onProjectUpdated(updated);
    } catch (e: any) {
      const code = e?.response?.data?.error?.code;
      onError(code === 'ANALYSIS_IN_PROGRESS' ? '模板分析仍在排队或运行，请完成或取消任务后再移除。' :
        code?.endsWith('_VERSION_CONFLICT') ? '项目、绑定页面或风格分析已变化；请刷新并重新核对后再移除，当前草稿未改动。' :
          e?.response?.data?.error?.message || e.message);
    } finally { removing.current = false; setBusy(''); }
  };
  const saveRole = async (item: TemplateReference) => {
    const analysis = { schema_version: 1, role,
      palette,
      font_suggestions: item.analysis?.font_suggestions || [], layout_hints: item.analysis?.layout_hints || [],
      decorative_hints: decorativeHints.split('\n').map(x => x.trim()).filter(Boolean), content_density: density,
      warnings: item.analysis?.warnings || [] };
    try { await saveTemplateProfile(projectId, item, analysis); setItems(await listTemplateReferences(projectId)); setEditing(null); }
    catch (e: any) { onError(e?.response?.data?.error?.message || e.message); }
  };
  return <div className="scene-template-panel">
    <div className="scene-inspector-section">风格参考</div>
    <p className="scene-help">PPTX/图片仅作版式和配色参考，不会把模板示例文字当成新内容。</p>
    <label className="scene-template-upload">上传 PPTX 或图片<input type="file" disabled={!!busy} accept=".pptx,image/png,image/jpeg,image/webp"
      onChange={e => { void upload(e.target.files?.[0]); e.target.value = ''; }} /></label>
    {busy && <p className="scene-help" role="status">{busy}</p>}
    {importReport && <div className="scene-template-report" role="status">
      <b>最近模板导入</b> · {importReport.status === 'preview_ready' || importReport.status === 'ready' ? '参考页已就绪' :
        importReport.status === 'failed' ? `导入失败（${importReport.error_code || '未知错误'}）` :
          `后台处理中（${importReport.status}）`}
      {importReport.source_page_count > 0 && ` · ${importReport.source_page_count} 页`}
      {importReport.warnings.length > 0 && <ul>{importReport.warnings.map(code => <li key={code}>
        {code === 'EXTERNAL_RELATIONSHIP_IGNORED' ? '外部链接未刷新，静态预览可能与原文件不同。' :
          code === 'EMBEDDED_OBJECT_STATIC_ONLY' ? '嵌入对象仅按静态内容预览。' :
          code === 'FONT_FAMILY_UNAVAILABLE' ? '部分幻灯片显式指定的字体未安装在转换环境中；静态预览可能发生字体替换。' :
          code === 'FONT_AVAILABILITY_UNVERIFIED' ? '无法核验模板字体是否安装；请人工复核静态预览。' : code}
      </li>)}</ul>}
      <button disabled={!!busy} onClick={() => void refreshImport()}>刷新导入状态</button>
    </div>}
    <label>本页风格补充说明<textarea rows={2} disabled={!!busy} value={styleText} onChange={e => setStyleText(e.target.value)}
      onBlur={() => { if (styleText !== (page.template_style_text || '')) void bind(items.find(x => x.template_asset_id === page.template_asset_id) || null); }} /></label>
    {suggestions.length > 0 && <details className="scene-template-auto-match"><summary>自动匹配建议（{suggestions.length} 页）</summary>
      <p className="scene-help">只使用已分析的参考页；不会覆盖手工选定的页面。匹配依据是页位与大纲标题，可逐页改选。</p>
      <ul>{suggestions.map(item => <li key={item.page.page_id}>
        第 {pages.findIndex(p => p.page_id === item.page.page_id) + 1} 页 · {item.page.outline_content?.title || '未命名'}
        {' → '}参考页 {item.reference.source_page_index}（{item.role}）</li>)}</ul>
      <button disabled={!!busy} onClick={() => void autoMatch()}>应用到未绑定页面</button>
    </details>}
    {items.length > 0 && <div className="scene-template-grid">{items.map(item => <div key={item.template_asset_id}
      className={`scene-template-item ${page.template_asset_id === item.template_asset_id ? 'active' : ''}`}>
      {item.thumbnail_url && <img src={assetUrl(item.thumbnail_url)} alt={`参考模板第 ${item.source_page_index} 页`} />}
      <small>参考页 {item.source_page_index} · {item.analysis_status === 'completed' ? '已分析' : '未分析 · 生成需视觉模型'}</small>
      {item.preview_url && <a href={assetUrl(item.preview_url)} target="_blank" rel="noreferrer">查看参考页大图</a>}
      <div><button disabled={!!busy} onClick={() => void bind(item)}>用于本页</button><button disabled={!!busy} onClick={() => void analyze(item)}>分析</button></div>
      <button disabled={!!busy} onClick={() => {
        setEditing(item.template_asset_id);
        setRole(String(item.analysis?.role || 'unknown'));
        const colors = item.analysis?.palette as Record<string, string> | undefined;
        setPalette({ background: colors?.background || '#FFFFFF', text: colors?.text || '#172A42', accent: colors?.accent || '#F7CC35' });
        setDensity(String(item.analysis?.content_density || 'medium'));
        setDecorativeHints(Array.isArray(item.analysis?.decorative_hints) ? (item.analysis.decorative_hints as string[]).join('\n') : '');
      }}>修订风格</button>
      <button disabled={!!busy} aria-label={`移除参考页 ${item.source_page_index}`}
        onClick={() => void removeReference(item)}>移除参考页</button>
      {editing === item.template_asset_id && <div className="scene-template-edit"><select value={role} onChange={e => setRole(e.target.value)}>
        {['cover', 'agenda', 'section', 'content', 'closing', 'unknown'].map(x => <option value={x} key={x}>{x}</option>)}</select>
        {(['background', 'text', 'accent'] as const).map(key => <label key={key}>{key}
          <input type="color" value={palette[key]} onChange={e => setPalette(current => ({ ...current, [key]: e.target.value }))} /></label>)}
        <label>内容密度<select value={density} onChange={e => setDensity(e.target.value)}>
          <option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label>
        <label>装饰提示（每行一条）<textarea rows={3} value={decorativeHints} onChange={e => setDecorativeHints(e.target.value)} /></label>
        <button disabled={!!busy} onClick={() => void saveRole(item)}>保存分析</button></div>}
    </div>)}</div>}
    {page.template_asset_id && <button disabled={!!busy} onClick={() => void bind(null)}>清除本页参考</button>}
  </div>;
}
