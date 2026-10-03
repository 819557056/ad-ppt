import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Copy, Download, FileText, ImagePlus, Lock, Plus, Redo2, Trash2, Undo2, Unlock, X } from 'lucide-react';
import { OutlineEditor } from '@/features/editor/OutlineEditor';
import { CandidateChangeSummary } from '@/features/editor/CandidateChangeSummary';
import { SceneRenderer } from '@/features/editor/SceneRenderer';
import { draggedFrame, proportionalFrame } from '@/features/editor/sceneGeometry';
import { canonicalScene } from '@/features/editor/sceneHash';
import { sceneHistoryTarget, type SceneHistoryTarget } from '@/features/editor/sceneHistory';
import { StorageCapacity } from '@/features/editor/StorageCapacity';
import { QueueCapacity } from '@/features/editor/QueueCapacity';
import { TemplateImport } from '@/features/templates/TemplateImport';
import { acceptCandidate, acceptCandidates, acceptExportReview, addModelCredential, addScenePage, assetUrl, cancelSceneTask, checkModelCredential, confirmGenerationPlan,
  createExport, createSnapshot, finishExportRequest, finishGenerationRequest, getCandidates, getSceneTask, getSceneTaskGroup,
  getExport, getPageScene, getSceneProject, getSceneFontManifest, getSnapshotPreflight, getRevisions, listSceneExports,
  rejectCandidate, restoreScene, revokeModelCredential, saveSceneCommands,
  saveModelConfig, startSceneAiEdit, startSceneGeneration, listModelCredentials, listSceneProjectTasks, retrySceneTask,
  uploadSceneImage, type ModelCredentialSummary, type SceneExportSummary, type SceneFontManifest,
  type SnapshotPreflight } from '@/features/editor/sceneApi';
import type { Command, Crop, Frame, SceneCandidate, SceneElement, SceneProject, SceneResponse, SlideScene,
  TextElement, TextStyle } from '@/features/editor/sceneTypes';
import './SceneEditorPage.css';

const defaultStyle: TextStyle = { font_family_id: 'noto-sans-sc', font_size_pt: 30,
  font_weight: 400, color: '#172A42', align: 'left', vertical_align: 'top',
  line_height: 1.2, padding_pt: 0 };

function applyLocal(scene: SlideScene, commands: Command[]): SlideScene {
  const next: SlideScene = structuredClone(scene);
  for (const command of commands) {
    if (command.op === 'add_element') { next.elements.push(command.element); continue; }
    if (command.op === 'set_background') { next.background = command.background; continue; }
    if (command.op === 'reorder_elements') {
      const byId = new Map(next.elements.map(e => [e.id, e]));
      next.elements = command.element_ids.map(id => byId.get(id)!).filter(Boolean); continue;
    }
    const element = next.elements.find(e => e.id === command.element_id);
    if (!element) continue;
    if (command.op === 'delete_element') next.elements = next.elements.filter(e => e.id !== command.element_id);
    if (command.op === 'set_locked') element.locked = command.locked;
    if (command.op === 'set_frame') element.frame = command.frame;
    if (command.op === 'set_text' && element.kind === 'text') element.text = command.text;
    if (command.op === 'set_text_style' && element.kind === 'text') element.style = command.style;
    if (command.op === 'replace_image' && element.kind === 'image') element.asset_id = command.asset_id;
    if (command.op === 'set_crop' && element.kind === 'image') { element.crop = command.crop; element.fit = command.fit; }
    if (command.op === 'set_opacity' && element.kind === 'image') element.opacity = command.opacity;
  }
  return next;
}

type Drag = { mode: 'move' | 'resize'; x: number; y: number; frames: Record<string, Frame>; ids: string[] };
type TaskRow = { task_id: string; page_id?: string | null; state: string;
  operation?: string; error_code: string | null; possible_charge: boolean };
type CachedDraft = { base_revision_id: string; commands: Command[][]; scene: SlideScene;
  asset_urls: Record<string, string> };

export function SceneEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<SceneProject | null>(null);
  const [pageId, setPageId] = useState('');
  const [base, setBase] = useState<SceneResponse | null>(null);
  const baseRef = useRef<SceneResponse | null>(null);
  const [scene, setScene] = useState<SlideScene | null>(null);
  const sceneRef = useRef<SlideScene | null>(null);
  const [thumbs, setThumbs] = useState<Record<string, SceneResponse>>({});
  const [assetUrls, setAssetUrls] = useState<Record<string, string>>({});
  const assetUrlsRef = useRef<Record<string, string>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [editingText, setEditingText] = useState<string | null>(null);
  const [zoom, setZoom] = useState(0.65);
  const [status, setStatus] = useState('已保存');
  const [conflictServer, setConflictServer] = useState<SceneResponse | null>(null);
  const conflictBlocked = useRef(false);
  const [error, setError] = useState('');
  const [exportOpen, setExportOpen] = useState(false);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [outlineTaskId, setOutlineTaskId] = useState<string | undefined>();
  const [exportBusy, setExportBusy] = useState<'pptx' | 'pdf' | null>(null);
  const [lastExport, setLastExport] = useState<{ exportId: string; downloadUrl: string | null;
    reviewFileUrl: string | null; reportUrl: string | null; reportHash: string | null;
    visualComparison: string | null; evidence: { page_id: string; url: string }[];
    format: string; status: string } | null>(null);
  const [recentExports, setRecentExports] = useState<SceneExportSummary[]>([]);
  const [exportCursor, setExportCursor] = useState<string | null>(null);
  const [exportPageIds, setExportPageIds] = useState<string[]>([]);
  const [preflightResponse, setPreflightResponse] = useState<{
    key: string; status: 'checking' | 'ready' | 'error'; result: SnapshotPreflight | null;
  } | null>(null);
  const [fontManifest, setFontManifest] = useState<SceneFontManifest | null>(null);
  const [fontManifestError, setFontManifestError] = useState('');
  const [candidateOpen, setCandidateOpen] = useState(false);
  const [candidatePreview, setCandidatePreview] = useState<SceneResponse | null>(null);
  const [reviewedCandidateId, setReviewedCandidateId] = useState<string | null>(null);
  const [reviewedCandidateIds, setReviewedCandidateIds] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<SceneCandidate[]>([]);
  const [revisions, setRevisions] = useState<{ revision_id: string; seq: number; origin: string; created_at: string }[]>([]);
  const [credentials, setCredentials] = useState<ModelCredentialSummary[]>([]);
  const [credentialId, setCredentialId] = useState('');
  const [credentialCheckBusy, setCredentialCheckBusy] = useState(false);
  const [credentialLabel, setCredentialLabel] = useState('');
  const [gatewayUrl, setGatewayUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [textModel, setTextModel] = useState('');
  const [imageModel, setImageModel] = useState('');
  const [aiInstruction, setAiInstruction] = useState('');
  const [generationPageIds, setGenerationPageIds] = useState<string[]>([]);
  const [aiTask, setAiTask] = useState('');
  const [aiBusy, setAiBusy] = useState(false);
  const [taskRows, setTaskRows] = useState<TaskRow[]>([]);
  const queue = useRef<Command[][]>([]);
  const draining = useRef(false);
  const drainPromise = useRef<Promise<void> | null>(null);
  const lastDrainFailed = useRef(false);
  const undo = useRef<SceneHistoryTarget[]>([]);
  const redo = useRef<SceneHistoryTarget[]>([]);
  const historyMoving = useRef(false);
  const lockChanging = useRef(false);
  const [historyPending, setHistoryPending] = useState(false);
  const drag = useRef<Drag | null>(null);
  const jobSubmitting = useRef(false);
  const exportSubmitting = useRef(false);
  const textTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingTextId = useRef<string | null>(null);
  const composingText = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const backgroundInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!exportOpen) return;
    let active = true;
    setFontManifest(null); setFontManifestError('');
    getSceneFontManifest().then(result => { if (active) setFontManifest(result); })
      .catch(() => { if (active) setFontManifestError('无法核验固定字体包；请稍后重试。'); });
    return () => { active = false; };
  }, [exportOpen]);

  // A response for the previous page selection/head must never block (or clear)
  // the current selection while React is waiting to run the effect cleanup.
  const preflightKey = project && exportOpen && exportPageIds.length ? JSON.stringify({
    project_id: project.project_id, project_version: project.project_version,
    pages: exportPageIds.map(id => {
      const page = project.pages.find(item => item.page_id === id);
      return [id, page?.page_version, page?.revision_id];
    }),
  }) : '';
  const currentPreflight = preflightResponse?.key === preflightKey ? preflightResponse : null;
  const exportPreflight = currentPreflight?.result;
  const exportPreflightStatus = currentPreflight?.status || 'idle';

  useEffect(() => {
    if (!exportOpen || !project || !exportPageIds.length) { setPreflightResponse(null); return; }
    let active = true;
    setPreflightResponse({ key: preflightKey, status: 'checking', result: null });
    const timer = setTimeout(() => {
      getSnapshotPreflight(project.project_id, exportPageIds).then(result => {
        if (active) setPreflightResponse({ key: preflightKey, status: 'ready', result });
      }).catch(() => { if (active) setPreflightResponse({ key: preflightKey, status: 'error', result: null }); });
    }, 250);
    return () => { active = false; clearTimeout(timer); };
  }, [exportOpen, project, exportPageIds, preflightKey]);

  const replaceScene = useCallback((response: SceneResponse) => {
    baseRef.current = response; setBase(response);
    sceneRef.current = response.scene; setScene(response.scene);
    assetUrlsRef.current = { ...assetUrlsRef.current, ...response.asset_urls };
    setAssetUrls(assetUrlsRef.current);
    setThumbs(current => ({ ...current, [response.page_id]: { ...response, asset_urls: assetUrlsRef.current } }));
    setProject(current => current ? { ...current, pages: current.pages.map(p => p.page_id === response.page_id
      ? { ...p, page_version: response.page_version, revision_id: response.revision_id } : p) } : null);
  }, []);

  const draftKey = id && pageId ? `banana-scene-draft-v1:${id}:${pageId}` : null;
  const persistDraft = useCallback(() => {
    if (!draftKey || !baseRef.current || !sceneRef.current) return;
    try {
      const commands = [...queue.current];
      const expected = applyLocal(baseRef.current.scene, commands.flat());
      for (const element of sceneRef.current.elements) {
        const previous = expected.elements.find(item => item.id === element.id);
        if (element.kind === 'text' && previous?.kind === 'text' && element.text !== previous.text)
          commands.push([{ op: 'set_text', element_id: element.id, text: element.text }]);
      }
      if (!commands.length) { localStorage.removeItem(draftKey); return; }
      const cache: CachedDraft = { base_revision_id: baseRef.current.revision_id, commands,
        scene: sceneRef.current, asset_urls: assetUrlsRef.current };
      localStorage.setItem(draftKey, JSON.stringify(cache));
    } catch { /* Storage may be disabled or full; the in-memory draft remains available. */ }
  }, [draftKey]);

  useEffect(() => {
    if (!id) return;
    let active = true;
    getSceneProject(id).then(async result => {
      if (!active) return;
      setProject(result);
      setTextModel(result.model_config?.text_model || '');
      setImageModel(result.model_config?.image_model || '');
      setPageId(result.pages[0]?.page_id || '');
      listModelCredentials().then(items => { if (active) { setCredentials(items); setCredentialId(items.find(c => c.status !== 'revoked')?.credential_id || ''); } }).catch(() => {});
      listSceneProjectTasks(id).then(items => { if (active) setTaskRows(items.filter(item =>
        ['generate_scene', 'generate_asset', 'ai_edit', 'generate_outline', 'analyze_template'].includes(item.operation)).slice(0, 30)); }).catch(() => {});
      listSceneExports(id).then(page => { if (active) { setRecentExports(page.items); setExportCursor(page.next_cursor); } }).catch(() => {});
      const entries = await Promise.all(result.pages.map(async p => [p.page_id, await getPageScene(id, p.page_id)] as const));
      if (active) setThumbs(Object.fromEntries(entries));
    }).catch(e => setError(e?.response?.data?.error?.message || e.message));
    return () => { active = false; };
  }, [id]);

  useEffect(() => {
    if (!id || !pageId) return;
    let active = true;
    setSelected([]); setEditingText(null); undo.current = []; redo.current = [];
    conflictBlocked.current = false; setConflictServer(null);
    getPageScene(id, pageId).then(result => { if (active) {
      replaceScene(result); setStatus('已保存');
      try {
        const raw = localStorage.getItem(`banana-scene-draft-v1:${id}:${pageId}`);
        const cached = raw ? JSON.parse(raw) as CachedDraft : null;
        if (cached && typeof cached.base_revision_id === 'string' && Array.isArray(cached.commands) &&
          cached.commands.length && cached.scene?.schema_version === 1 && Array.isArray(cached.scene.elements)) {
          // A save may have committed even though its HTTP response was lost.
          // Do not replay an already-applied add/delete command on refresh.
          if (canonicalScene(cached.scene) === canonicalScene(result.scene)) {
            queue.current = [];
            localStorage.removeItem(`banana-scene-draft-v1:${id}:${pageId}`);
          } else {
            queue.current = cached.commands;
            sceneRef.current = cached.scene; setScene(cached.scene);
            assetUrlsRef.current = { ...assetUrlsRef.current, ...cached.asset_urls };
            setAssetUrls(assetUrlsRef.current);
            conflictBlocked.current = true; setConflictServer(result);
            setStatus('未保存草稿 · 请确认恢复');
          }
        }
      } catch { /* Ignore damaged or unavailable browser storage. */ }
    } })
      .catch(e => setError(e?.response?.data?.error?.message || e.message));
    return () => { active = false; };
  }, [id, pageId, replaceScene]);

  const drain = useCallback((): Promise<void> => {
    if (drainPromise.current) return drainPromise.current;
    if (conflictBlocked.current || historyMoving.current || !id || !pageId) return Promise.resolve();
    const task = (async () => {
      draining.current = true;
      lastDrainFailed.current = false;
      try {
        while (queue.current.length) {
          const commands = queue.current[0];
          const current = baseRef.current;
          if (!current) break;
          setStatus('保存中');
          const response = await saveSceneCommands(id, pageId, current, commands);
          undo.current.push(sceneHistoryTarget(current, commands));
          redo.current = [];
          queue.current.shift();
          baseRef.current = response; setBase(response);
          assetUrlsRef.current = { ...assetUrlsRef.current, ...response.asset_urls };
          setAssetUrls(assetUrlsRef.current);
          setThumbs(items => ({ ...items, [pageId]: { ...response, asset_urls: assetUrlsRef.current } }));
          setProject(p => p ? { ...p, pages: p.pages.map(item => item.page_id === pageId
            ? { ...item, revision_id: response.revision_id, page_version: response.page_version } : item) } : null);
          if (!queue.current.length && !pendingTextId.current) { sceneRef.current = response.scene; setScene(response.scene); }
          persistDraft();
        }
        setStatus(pendingTextId.current ? '未保存' : '已保存');
      } catch (e: any) {
        lastDrainFailed.current = true;
        setStatus(e?.response?.status === 409 ? '版本冲突 · 草稿已保留' : '离线草稿 · 未保存');
        setError(e?.response?.data?.error?.message || e.message);
        persistDraft();
        if (e?.response?.status === 409) {
          conflictBlocked.current = true;
          try { setConflictServer(await getPageScene(id, pageId)); }
          catch { setError('版本冲突；暂时无法读取服务器版本。请恢复网络后重新加载页面，当前草稿仍在本标签页内。'); }
        }
      } finally { draining.current = false; }
    })().finally(() => { drainPromise.current = null; });
    drainPromise.current = task;
    return task;
  }, [id, pageId, persistDraft]);

  const commit = useCallback((commands: Command[]) => {
    if (historyMoving.current) { setError('请等待撤销或重做完成后再编辑'); return; }
    if (!sceneRef.current || !commands.length) return;
    const draft = applyLocal(sceneRef.current, commands);
    // Blurring an unchanged property must not create a save that swallows the
    // immediately following Undo click or adds an empty history entry.
    if (canonicalScene(draft) === canonicalScene(sceneRef.current)) return;
    sceneRef.current = draft; setScene(draft);
    queue.current.push(commands); persistDraft(); if (!conflictBlocked.current) void drain();
  }, [drain, persistDraft]);

  const flushText = useCallback((elementId: string | null) => {
    if (composingText.current) return;
    if (pendingTextId.current === elementId) {
      if (textTimer.current) clearTimeout(textTimer.current);
      textTimer.current = null;
      pendingTextId.current = null;
    }
    const value = sceneRef.current?.elements.find(e => e.id === elementId);
    const saved = baseRef.current?.scene.elements.find(e => e.id === elementId);
    // A newly inserted text object may still be awaiting its first save. Its
    // queued add_element is the text baseline until the server returns a head.
    const queued = queue.current.flat().reverse().find(command =>
      (command.op === 'set_text' && command.element_id === elementId) ||
      (command.op === 'add_element' && command.element.id === elementId && command.element.kind === 'text'));
    const expectedText = queued?.op === 'set_text' ? queued.text
      : queued?.op === 'add_element' && queued.element.kind === 'text' ? queued.element.text
      : saved?.kind === 'text' ? saved.text : null;
    if (elementId && value?.kind === 'text' && expectedText !== null && value.text !== expectedText) {
      queue.current.push([{ op: 'set_text', element_id: elementId, text: value.text }]);
      persistDraft();
      void drain();
    }
  }, [drain, persistDraft]);

  const ensureSaved = async (source: SceneProject, intent = '提交生成或导出'): Promise<SceneProject | null> => {
    if (historyMoving.current) { setError('请等待撤销或重做完成后再提交'); return null; }
    if (drag.current) { setError(`请先结束当前对象拖动或缩放，再${intent}`); return null; }
    if (composingText.current) { setError(`请先完成中文输入，再${intent}`); return null; }
    if (pendingTextId.current) flushText(pendingTextId.current);
    for (let attempt = 0; attempt < 3; attempt++) {
      await drain();
      if (!queue.current.length && !draining.current && !pendingTextId.current && !conflictBlocked.current) {
        const head = baseRef.current;
        return head ? { ...source, pages: source.pages.map(page => page.page_id === head.page_id
          ? { ...page, page_version: head.page_version, revision_id: head.revision_id } : page) } : source;
      }
      if (conflictBlocked.current || lastDrainFailed.current) break;
    }
    setError('当前页未能保存；已保留草稿，请解决冲突或网络问题后重试');
    return null;
  };

  const toggleLock = async (elementId: string) => {
    if (!project || lockChanging.current || historyMoving.current) return;
    lockChanging.current = true;
    try {
      if (!await ensureSaved(project, '更改对象锁定状态')) return;
      const element = sceneRef.current?.elements.find(item => item.id === elementId);
      if (!element) { setError('对象已不存在，请重新选择'); return; }
      commit([{ op: 'set_locked', element_id: elementId, locked: !element.locked }]);
      await drain();
    } finally { lockChanging.current = false; }
  };

  const setTextDraft = (elementId: string, value: string) => {
    if (historyMoving.current) { setError('请等待撤销或重做完成后再编辑'); return; }
    if (!sceneRef.current) return;
    if (pendingTextId.current && pendingTextId.current !== elementId) flushText(pendingTextId.current);
    const next = structuredClone(sceneRef.current);
    const element = next.elements.find(e => e.id === elementId);
    if (element?.kind === 'text') element.text = value;
    sceneRef.current = next; setScene(next); setStatus('未保存');
    if (textTimer.current) clearTimeout(textTimer.current);
    pendingTextId.current = elementId;
    persistDraft();
    if (!composingText.current) textTimer.current = setTimeout(() => flushText(elementId), 800);
  };

  const beginComposition = () => {
    composingText.current = true;
    if (textTimer.current) clearTimeout(textTimer.current);
    textTimer.current = null;
  };
  const endComposition = (elementId: string) => {
    composingText.current = false;
    if (textTimer.current) clearTimeout(textTimer.current);
    pendingTextId.current = elementId;
    textTimer.current = setTimeout(() => flushText(elementId), 800);
  };

  const selectedElement = scene?.elements.find(e => e.id === selected[0]);
  const selectedElements = scene?.elements.filter(e => selected.includes(e.id)) || [];
  const canEdit = selectedElement && !selectedElement.locked && !historyPending;
  const select = (elementId: string, additive: boolean) => setSelected(current => additive
    ? current.includes(elementId) ? current.filter(x => x !== elementId) : [...current, elementId] : [elementId]);

  const pointerStart = (event: PointerEvent<HTMLDivElement>, elementId: string) => {
    if (historyMoving.current || event.shiftKey || event.button !== 0 || editingText || !sceneRef.current) return;
    const ids = selected.includes(elementId) ? selected : [elementId];
    const elements = sceneRef.current.elements.filter(e => ids.includes(e.id) && !e.locked);
    if (!elements.length) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { mode: 'move', x: event.clientX, y: event.clientY,
      frames: Object.fromEntries(elements.map(e => [e.id, { ...e.frame }])), ids: elements.map(e => e.id) };
  };

  useEffect(() => {
    const move = (event: globalThis.PointerEvent) => {
      if (!drag.current || !sceneRef.current) return;
      const ratio = zoom * 96 / 72;
      const dx = (event.clientX - drag.current.x) / ratio, dy = (event.clientY - drag.current.y) / ratio;
      const next = structuredClone(sceneRef.current);
      for (const element of next.elements) {
        const start = drag.current.frames[element.id];
        if (!start) continue;
        element.frame = draggedFrame(start, dx, dy, drag.current.mode, element.kind, next.canvas);
      }
      setScene(next);
    };
    const end = (event: globalThis.PointerEvent) => {
      const active = drag.current;
      if (!active) return;
      drag.current = null;
      const ratio = zoom * 96 / 72;
      const dx = (event.clientX - active.x) / ratio, dy = (event.clientY - active.y) / ratio;
      if (Math.abs(dx) + Math.abs(dy) < 0.5) return;
      const draft = sceneRef.current;
      if (!draft) return;
      const commands: Command[] = active.ids.flatMap(elementId => {
        const frame = active.frames[elementId];
        const element = draft.elements.find(item => item.id === elementId);
        if (!element) return [];
        const next = draggedFrame(frame, dx, dy, active.mode, element.kind, draft.canvas);
        if (Object.keys(frame).every(key => frame[key as keyof Frame] === next[key as keyof Frame])) return [];
        return [{ op: 'set_frame', element_id: elementId, frame: next } as Command];
      });
      commit(commands);
    };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', end);
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', end); };
  }, [commit, zoom]);

  const addText = () => {
    if (!scene) return;
    const element: TextElement = { id: crypto.randomUUID(), kind: 'text', role: 'body',
      frame: { x: 80, y: 150, w: Math.min(500, scene.canvas.width_pt - 160), h: 130, rotation_deg: 0 }, text: '点击右侧编辑文字',
      style: defaultStyle, locked: false };
    commit([{ op: 'add_element', element }]); setSelected([element.id]);
  };
  const addImage = async (file?: File) => {
    if (!id || !file) return;
    try {
      const uploaded = await uploadSceneImage(id, file);
      assetUrlsRef.current = { ...assetUrlsRef.current, [uploaded.asset_id]: uploaded.url };
      setAssetUrls(assetUrlsRef.current);
      if (selectedElement?.kind === 'image' && !selectedElement.locked) {
        commit([{ op: 'replace_image', element_id: selectedElement.id, asset_id: uploaded.asset_id }]);
        return;
      }
      const element: SceneElement = { id: crypto.randomUUID(), kind: 'image', role: 'illustration',
        asset_id: uploaded.asset_id, frame: { x: Math.max(40, (sceneRef.current?.canvas.width_pt || 960) - 340), y: 135, w: 300, h: 270, rotation_deg: 0 },
        crop: { x: 0, y: 0, w: 1, h: 1 }, fit: 'contain', opacity: 1, locked: false, alt_text: file.name };
      commit([{ op: 'add_element', element }]); setSelected([element.id]);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };

  const replaceBackground = async (file?: File) => {
    if (!id || !file) return;
    try {
      const uploaded = await uploadSceneImage(id, file);
      assetUrlsRef.current = { ...assetUrlsRef.current, [uploaded.asset_id]: uploaded.url };
      setAssetUrls(assetUrlsRef.current);
      commit([{ op: 'set_background', background: { kind: 'image', asset_id: uploaded.asset_id,
        crop: { x: 0, y: 0, w: 1, h: 1 } } }]);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };

  const setStyle = (partial: Partial<TextStyle>) => {
    if (selectedElement?.kind !== 'text' || selectedElement.locked) return;
    commit([{ op: 'set_text_style', element_id: selectedElement.id,
      style: { ...selectedElement.style, ...partial } }]);
  };
  const setFrameField = (field: keyof Frame, value: number): boolean => {
    if (!selectedElement || selectedElement.locked || !Number.isFinite(value)) return false;
    if (field === 'rotation_deg' && (selectedElement.kind !== 'image' || value < -180 || value > 180)) return false;
    if ((field === 'x' || field === 'y') && (value < -2880 || value > 2880)) return false;
    if ((field === 'w' || field === 'h') && (value <= 0 || value > 2880)) return false;
    const frame = selectedElement.kind === 'image' && (field === 'w' || field === 'h')
      ? proportionalFrame(selectedElement.frame, field, value)
      : { ...selectedElement.frame, [field]: value };
    if (!frame) { setError('图片等比缩放后的尺寸须在 0–2880 pt 内'); return false; }
    if (frame[field] !== selectedElement.frame[field]) commit([{ op: 'set_frame', element_id: selectedElement.id, frame }]);
    return true;
  };
  const setCropField = (field: keyof Crop, value: number) => {
    if (selectedElement?.kind !== 'image' || selectedElement.locked || !Number.isFinite(value)) return false;
    const crop = { ...selectedElement.crop, [field]: Math.round(value * 1_000_000) / 1_000_000 };
    if (crop.x < 0 || crop.y < 0 || crop.x > 1 || crop.y > 1 ||
      crop.w <= 0 || crop.h <= 0 || crop.w > 1 || crop.h > 1 ||
      crop.x + crop.w > 1.000001 || crop.y + crop.h > 1.000001) {
      setError('裁剪范围须在原图内，宽和高必须大于 0'); return false;
    }
    if (crop[field] === selectedElement.crop[field]) return true;
    commit([{ op: 'set_crop', element_id: selectedElement.id, crop, fit: selectedElement.fit }]);
    return true;
  };
  const deleteSelection = () => {
    const editable = selectedElements.filter(element => !element.locked);
    if (!editable.length) return;
    commit(editable.map(element => ({ op: 'delete_element', element_id: element.id })));
    setSelected(selectedElements.filter(element => element.locked).map(element => element.id));
  };
  const moveSelectionLayer = (direction: 'up' | 'down') => {
    if (!scene || !selectedElements.some(element => !element.locked)) return;
    const ids = scene.elements.map(element => element.id);
    const moving = new Set(selectedElements.filter(element => !element.locked).map(element => element.id));
    const locked = new Set(scene.elements.filter(element => element.locked).map(element => element.id));
    if (direction === 'up') {
      for (let index = ids.length - 2; index >= 0; index--) {
        if (moving.has(ids[index]) && !moving.has(ids[index + 1]) && !locked.has(ids[index + 1])) {
          [ids[index], ids[index + 1]] = [ids[index + 1], ids[index]];
        }
      }
    } else {
      for (let index = 1; index < ids.length; index++) {
        if (moving.has(ids[index]) && !moving.has(ids[index - 1]) && !locked.has(ids[index - 1])) {
          [ids[index], ids[index - 1]] = [ids[index - 1], ids[index]];
        }
      }
    }
    if (ids.some((id, index) => id !== scene.elements[index].id)) commit([{ op: 'reorder_elements', element_ids: ids }]);
  };
  const historyMove = async (direction: 'undo' | 'redo') => {
    if (historyMoving.current || conflictBlocked.current || !id || !baseRef.current || drag.current ||
        composingText.current || queue.current.length || draining.current || pendingTextId.current || !pageId) return;
    const from = direction === 'undo' ? undo.current : redo.current;
    const to = direction === 'undo' ? redo.current : undo.current;
    const target = from[from.length - 1]; if (!target) return;
    historyMoving.current = true; setHistoryPending(true);
    setStatus(direction === 'undo' ? '正在撤销' : '正在重做');
    try {
      const current = baseRef.current;
      const inverse = sceneHistoryTarget(current, target.kind === 'locks' ? target.commands : []);
      const response = target.kind === 'locks'
        ? await saveSceneCommands(id, pageId, current, target.commands)
        : await restoreScene(id, pageId, current, target.revision_id);
      from.pop(); to.push(inverse); replaceScene(response);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
    finally { historyMoving.current = false; setHistoryPending(false); setStatus('已保存'); }
  };

  const resolveConflict = async (reapply: boolean) => {
    if (!id || !pageId || !conflictServer || draining.current) return;
    try {
      const latest = await getPageScene(id, pageId);
      if (latest.revision_id !== conflictServer.revision_id) {
        setConflictServer(latest);
        setError('服务器版本再次变化，请重新查看对照后再决定。');
        return;
      }
      if (pendingTextId.current) flushText(pendingTextId.current);
      if (reapply) {
        const replay = applyLocal(latest.scene, queue.current.flat());
        baseRef.current = latest; setBase(latest);
        sceneRef.current = replay; setScene(replay);
        assetUrlsRef.current = { ...assetUrlsRef.current, ...latest.asset_urls };
        setAssetUrls(assetUrlsRef.current);
        conflictBlocked.current = false; setConflictServer(null);
        persistDraft();
        await drain();
      } else {
        queue.current = [];
        if (textTimer.current) clearTimeout(textTimer.current);
        textTimer.current = null; pendingTextId.current = null;
        conflictBlocked.current = false; setConflictServer(null);
        if (draftKey) try { localStorage.removeItem(draftKey); } catch { /* no-op */ }
        replaceScene(latest); setStatus('已保存');
      }
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };

  const exportSelectionKey = project ? `banana-scene-export-pages-v1:${project.project_id}` : null;
  const setExportPageSelection = (ids: string[]) => {
    if (!project) return;
    const ordered = project.pages.filter(page => ids.includes(page.page_id)).map(page => page.page_id);
    setExportPageIds(ordered);
    if (exportSelectionKey) try { sessionStorage.setItem(exportSelectionKey, JSON.stringify(ordered)); }
      catch { /* Selection still works for this tab when storage is unavailable. */ }
  };
  const openExportDialog = () => {
    if (!project) return;
    const all = project.pages.map(page => page.page_id);
    let chosen = all;
    if (exportSelectionKey) try {
      const saved = sessionStorage.getItem(exportSelectionKey);
      if (saved) {
        const parsed: unknown = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.every(id => typeof id === 'string'))
          chosen = all.filter(id => parsed.includes(id));
      }
    } catch { /* Invalid storage falls back to all current pages. */ }
    setExportPageIds(chosen);
    setExportOpen(true);
  };
  const exportDeck = async (format: 'pptx' | 'pdf') => {
    if (exportSubmitting.current) return;
    if (!project) return;
    exportSubmitting.current = true;
    setExportBusy(format);
    try {
      const savedProject = await ensureSaved(project);
      if (!savedProject) return;
      const selectedPageIds = savedProject.pages.filter(page => exportPageIds.includes(page.page_id)).map(page => page.page_id);
      if (!selectedPageIds.length) { setError('请至少选择一页导出'); return; }
      const otherDraft = selectedPageIds.find(id => id !== pageId &&
        localStorage.getItem(`banana-scene-draft-v1:${savedProject.project_id}:${id}`));
      if (otherDraft) {
        const position = savedProject.pages.findIndex(page => page.page_id === otherDraft) + 1;
        setError(`第 ${position} 页有未确认的本地草稿；请先切换到该页并完成保存，再导出。`);
        return;
      }
      setLastExport(null);
      const pending = (await getCandidates(project.project_id, 'pending')).filter(candidate =>
        selectedPageIds.includes(candidate.page_id));
      const policy = pending.length ? (window.confirm(
        `有 ${pending.length} 个未接受的 AI 候选。确定只导出当前已接受的页面版本，不包含这些候选吗？`
      ) ? 'export_accepted' as const : null) : undefined;
      if (policy === null) return;
      const snapshot = await createSnapshot(savedProject, policy, selectedPageIds);
      const job = await createExport(project.project_id, snapshot.snapshot_id, format);
      listSceneExports(project.project_id).then(page => {
        setRecentExports(page.items); setExportCursor(page.next_cursor);
      }).catch(() => {});
      for (let attempt = 0; attempt < 120; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const result = await getExport(project.project_id, job.export_id);
        if (result.status === 'needs_review') {
          await finishExportRequest(savedProject, policy, snapshot.snapshot_id, format, selectedPageIds);
          setLastExport({ exportId: job.export_id, downloadUrl: null,
            reviewFileUrl: result.review_file_url, reportUrl: result.report_url,
            reportHash: result.report_sha256, visualComparison: result.visual_comparison,
            evidence: result.visual_evidence, format, status: result.status });
          return;
        }
        if (result.status === 'succeeded' && result.download_url) {
          await finishExportRequest(savedProject, policy, snapshot.snapshot_id, format, selectedPageIds);
          window.open(assetUrl(result.download_url) + '&download=1', '_blank', 'noopener');
          setLastExport({ exportId: job.export_id, downloadUrl: result.download_url,
            reviewFileUrl: null, reportUrl: result.report_url, reportHash: result.report_sha256,
            visualComparison: result.visual_comparison, evidence: result.visual_evidence,
            format, status: result.status });
          return;
        }
        if (result.status === 'failed') {
          await finishExportRequest(savedProject, policy, snapshot.snapshot_id, format, selectedPageIds);
          throw new Error(`导出失败：${result.error_code}`);
        }
      }
      throw new Error('导出仍在进行，请稍后在此项目查看');
    } catch (e: any) {
      const failure = e?.response?.data?.error;
      if (failure?.code === 'FONT_GLYPH_UNAVAILABLE') {
        const points = Array.isArray(failure.details?.codepoints) ? failure.details.codepoints.join('、') : '';
        setError(`固定字体不支持文字中的 ${points || '部分字符'}；请修改对应文字后重新导出。`);
      } else setError(failure?.message || e.message);
    }
    finally { exportSubmitting.current = false; setExportBusy(null); }
  };
  const openRecentExport = async (entry: SceneExportSummary) => {
    if (!project) return;
    try {
      const result = await getExport(project.project_id, entry.export_id);
      setLastExport({ exportId: entry.export_id, downloadUrl: result.download_url,
        reviewFileUrl: result.review_file_url, reportUrl: result.report_url,
        reportHash: result.report_sha256, visualComparison: result.visual_comparison,
        evidence: result.visual_evidence, format: entry.format, status: result.status });
      openExportDialog();
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const approveExport = async () => {
    if (!project || !lastExport?.reportHash || lastExport.visualComparison !== 'needs_review') return;
    if (!window.confirm('已检查导出文件、质量报告和逐页视觉对照，仅接受不影响内容的视觉差异？')) return;
    try {
      await acceptExportReview(project.project_id, lastExport.exportId, lastExport.reportHash);
      const result = await getExport(project.project_id, lastExport.exportId);
      setLastExport(current => current ? { ...current, status: result.status,
        downloadUrl: result.download_url, reviewFileUrl: null } : null);
      const page = await listSceneExports(project.project_id);
      setRecentExports(page.items); setExportCursor(page.next_cursor);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const openOutline = () => { setOutlineTaskId(undefined); setOutlineOpen(true); };

  const loadCandidates = async () => {
    if (!id) return;
    try {
      setCandidates(await getCandidates(id)); setCandidatePreview(null);
      setReviewedCandidateId(null); setReviewedCandidateIds([]); setCandidateOpen(true);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const createCredential = async () => {
    try {
      const added = await addModelCredential(credentialLabel.trim(), gatewayUrl.trim(), apiKey);
      setCredentials(items => [added, ...items]); setCredentialId(added.credential_id); setApiKey('');
      setCredentialLabel(''); setGatewayUrl('');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const revokeCredential = async () => {
    if (!credentialId || !window.confirm('撤销当前模型 API Key？新任务将无法继续使用，已创建任务与审计记录会保留。')) return;
    try {
      await revokeModelCredential(credentialId);
      const items = await listModelCredentials();
      setCredentials(items);
      setCredentialId(items.find(item => item.status !== 'revoked')?.credential_id || '');
      setAiTask('模型 API Key 已撤销');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const checkCredential = async (kind: 'model_listing' | 'text_generation' | 'image_generation') => {
    if (!project || !credentialId || credentialCheckBusy) return;
    const modelId = kind === 'text_generation' ? textModel.trim() : kind === 'image_generation' ? imageModel.trim() : undefined;
    if (kind !== 'model_listing' && !modelId) { setError('请先填写要试调用的模型 ID'); return; }
    if (kind !== 'model_listing' && !window.confirm(
      `将使用你自己的 API Key 试调用${kind === 'text_generation' ? '文本' : '图片'}模型 ${modelId}，可能产生费用。确定继续？`
    )) return;
    setCredentialCheckBusy(true);
    try {
      const task = await checkModelCredential(project.project_id, credentialId, kind, modelId);
      setAiTask(kind === 'model_listing' ? '正在检查 Key 与模型列表（不执行付费生成）' :
        `正在试调用${kind === 'text_generation' ? '文本' : '图片'}模型（可能计费）`);
      for (let attempt = 0; attempt < 60; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const result = await getSceneTask(task.task_id);
        if (result.state === 'succeeded') {
          setCredentials(await listModelCredentials());
          setAiTask(kind === 'model_listing' ? 'Key 连接与模型列表可用；文本/图片生成能力仍需实际调用验证' :
            `${kind === 'text_generation' ? '文本' : '图片'}模型 ${modelId} 试调用成功`);
          return;
        }
        if (['failed', 'cancelled', 'outcome_unknown'].includes(result.state))
          throw new Error(`模型连接检查失败：${result.error_code}`);
      }
      setAiTask('模型检查仍在后台运行，请稍后刷新');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
    finally { setCredentialCheckBusy(false); }
  };
  const configuredProject = async (requireImageModel = true, source: SceneProject | null = project) => {
    if (!source) throw new Error('项目未加载');
    if (!textModel.trim() || (requireImageModel && !imageModel.trim()))
      throw new Error(requireImageModel ? '请设置文本模型和图片模型' : '请设置文本模型');
    if (source.model_config?.text_model === textModel.trim() &&
        (source.model_config?.image_model || '') === imageModel.trim()) return source;
    const updated = await saveModelConfig(source, textModel.trim(), imageModel.trim());
    setProject(updated);
    return updated;
  };
  const runGeneration = async (selection: 'current' | 'selected' | 'all') => {
    if (jobSubmitting.current) return;
    if (!project || !credentialId) { setError('请先配置自己的模型 API Key'); return; }
    jobSubmitting.current = true; setAiBusy(true);
    try {
      const savedProject = await ensureSaved(project);
      if (!savedProject) return;
      const targetIds = selection === 'all' ? savedProject.pages.map(p => p.page_id)
        : selection === 'selected' ? savedProject.pages.filter(p => generationPageIds.includes(p.page_id)).map(p => p.page_id)
          : [pageId];
      if (!targetIds.length) { setError('请至少选择一页进行生成'); return; }
      if (!window.confirm(`将使用你自己的模型 Key 生成 ${targetIds.length} 页候选。文本模型：${textModel.trim() || '未配置'}；图片模型：${imageModel.trim() || '未配置'}。每页预计 0–6 个图片请求（具体数量由草稿决定），未分析的参考页需视觉模型；调用可能计费，费用以模型网关账单为准。继续吗？`)) return;
      const configured = await configuredProject(true, savedProject);
      const plan = await confirmGenerationPlan(configured);
      const task = await startSceneGeneration(configured, plan.plan_id, credentialId, targetIds);
      setTaskRows(task.tasks.map(t => ({ task_id: t.task_id, page_id: t.page_id,
        state: 'queued', error_code: null, possible_charge: false })));
      setAiTask(`生成中 · ${targetIds.length} 页`);
      for (let i = 0; i < 180; i++) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const group = await getSceneTaskGroup(task.task_group_id);
        setTaskRows(group.items);
        const pages = group.items.filter(item => item.operation === 'generate_scene');
        const finished = pages.filter(item => ['succeeded', 'failed', 'outcome_unknown', 'cancelled'].includes(item.state));
        const blocked = group.items.some(item => ['failed', 'outcome_unknown', 'cancelled'].includes(item.state) && item.operation === 'generate_asset');
        setAiTask(`生成中 · ${finished.length}/${pages.length} 页${blocked ? ' · 素材步骤需处理' : ''}`);
        if (group.state === 'needs_attention') {
          setAiTask('部分素材需要处理 · 成功页面保留；请重试失败步骤');
          await loadCandidates(); return;
        }
        if (finished.length === pages.length) {
          if (pages.every(item => item.state === 'succeeded'))
            await finishGenerationRequest(configured, plan.plan_id, credentialId, targetIds);
          setAiTask(pages.some(item => item.state !== 'succeeded') ? '部分页面失败 · 可查看任务' : '候选已生成 · 请审核');
          await loadCandidates(); return;
        }
      }
      setAiTask('任务仍在运行，请稍后刷新查看');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); setAiTask('生成未启动或失败'); }
    finally { jobSubmitting.current = false; setAiBusy(false); }
  };
  const runAiEdit = async () => {
    if (jobSubmitting.current) return;
    if (!project || !id || !baseRef.current || !credentialId || !aiInstruction.trim()) { setError('请选择模型凭据并输入修改要求'); return; }
    jobSubmitting.current = true; setAiBusy(true);
    try {
      const savedProject = await ensureSaved(project);
      if (!savedProject) return;
      if (!window.confirm(`将使用你自己的模型 Key 和文本模型 ${textModel.trim() || '未配置'} 为当前页生成修改候选；最多可能产生 6 个新图片请求，费用以模型网关账单为准。修改范围：${selected.length ? `${selected.length} 个选中对象` : '当前页'}。继续吗？`)) return;
      const configured = await configuredProject(false, savedProject);
      const task = await startSceneAiEdit(configured.project_id, pageId, baseRef.current, credentialId, aiInstruction.trim(), selected);
      setTaskRows([{ task_id: task.task_id, page_id: pageId,
        state: 'queued', error_code: null, possible_charge: false }]);
      setAiTask('AI 修改中');
      for (let i = 0; i < 180; i++) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const group = await getSceneTaskGroup(task.task_group_id);
        setTaskRows(group.items);
        const parent = group.items.find(item => item.task_id === task.task_id);
        if (parent?.state === 'succeeded') { setAiTask('修改候选已生成 · 请审核'); await loadCandidates(); return; }
        if (group.state === 'needs_attention') {
          setAiTask('图片素材步骤需要处理；草稿和成功素材已保留'); return;
        }
        if (parent && ['failed', 'outcome_unknown', 'cancelled'].includes(parent.state))
          throw new Error(`AI 修改未完成：${parent.error_code}`);
      }
      setAiTask('任务仍在运行，请稍后刷新查看');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); setAiTask('AI 修改失败'); }
    finally { jobSubmitting.current = false; setAiBusy(false); }
  };
  const acceptOne = async (candidateId: string) => {
    if (!id) return;
    if (drag.current || composingText.current || queue.current.length || draining.current ||
        pendingTextId.current || status !== '已保存') { setError('请先完成当前编辑并保存，再接受候选'); return; }
    try {
      const candidate = candidates.find(item => item.candidate_id === candidateId);
      if (!candidate) throw new Error('候选已变化，请重新打开候选列表');
      const previousRevision = candidate.page_id === pageId ? baseRef.current?.revision_id : null;
      await acceptCandidate(id, candidateId);
      setCandidates(await getCandidates(id));
      setReviewedCandidateIds(current => current.filter(item => item !== candidateId));
      const updated = await getSceneProject(id); setProject(updated);
      const accepted = await getPageScene(id, candidate.page_id);
      if (candidate.page_id === pageId) {
        if (previousRevision && previousRevision !== accepted.revision_id) {
          undo.current.push({ kind: 'revision', revision_id: previousRevision }); redo.current = [];
        }
        replaceScene(accepted);
      } else {
        assetUrlsRef.current = { ...assetUrlsRef.current, ...accepted.asset_urls };
        setAssetUrls(assetUrlsRef.current);
        setThumbs(items => ({ ...items, [candidate.page_id]: accepted }));
      }
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const acceptReviewed = async () => {
    if (!id || !project || !reviewedCandidateIds.length) return;
    if (drag.current || composingText.current || queue.current.length || draining.current ||
        pendingTextId.current || status !== '已保存') { setError('请先完成当前编辑并保存，再接受候选'); return; }
    const pending = candidates.filter(candidate => candidate.state === 'pending' &&
      reviewedCandidateIds.includes(candidate.candidate_id));
    if (pending.length !== reviewedCandidateIds.length ||
        new Set(pending.map(candidate => candidate.page_id)).size !== pending.length) {
      setError('每页只能选择一个待确认候选；请重新预览并选择。'); return;
    }
    const draftPage = pending.find(candidate => candidate.page_id !== pageId &&
      localStorage.getItem(`banana-scene-draft-v1:${project.project_id}:${candidate.page_id}`));
    if (draftPage) { setError('所选候选的某页有未确认的本地草稿；请先处理该页草稿。'); return; }
    if (!window.confirm(`已预览 ${pending.length} 页候选。将一次性接受并替换对应页面的当前版本；任一页冲突则整批不接受。继续吗？`)) return;
    try {
      const previousRevision = baseRef.current?.revision_id;
      const accepted = await acceptCandidates(id, pending.map(candidate => candidate.candidate_id));
      setCandidates(await getCandidates(id));
      setProject(await getSceneProject(id));
      for (const response of accepted) {
        if (response.page_id === pageId) {
          if (previousRevision && previousRevision !== response.revision_id) {
            undo.current.push({ kind: 'revision', revision_id: previousRevision }); redo.current = [];
          }
          replaceScene(response);
        } else {
          assetUrlsRef.current = { ...assetUrlsRef.current, ...response.asset_urls };
          setAssetUrls(assetUrlsRef.current);
          setThumbs(items => ({ ...items, [response.page_id]: response }));
        }
      }
      setReviewedCandidateIds([]); setReviewedCandidateId(null); setCandidatePreview(null);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const refreshTasks = async () => {
    try {
      const latest = await Promise.all(taskRows.map(async row => {
        const result = await getSceneTask(row.task_id);
        return { ...row, state: result.state, error_code: result.error_code,
          possible_charge: result.possible_charge };
      }));
      setTaskRows(latest);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const cancelTasks = async () => {
    try {
      await Promise.all(taskRows.filter(row => ['queued', 'running', 'waiting_assets'].includes(row.state))
        .map(row => cancelSceneTask(row.task_id)));
      setAiTask('已请求取消；在途模型请求可能仍会完成或计费');
      await refreshTasks();
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const retryTask = async (row: TaskRow) => {
    if (row.possible_charge && !window.confirm('此任务的上游请求可能已计费；重试可能再次计费。确定重试？')) return;
    try {
      await retrySceneTask(row.task_id, row.possible_charge);
      await refreshTasks();
      setAiTask('已重新排队，请刷新任务状态');
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };
  const insertCurrentPage = async (copy: boolean) => {
    if (!project || !pageId) return;
    if (historyMoving.current) { setError('请等待撤销或重做完成后再添加页面'); return; }
    if (drag.current || composingText.current) { setError('请先完成当前对象操作或中文输入再添加页面'); return; }
    if (pendingTextId.current) flushText(pendingTextId.current);
    if (queue.current.length || draining.current) { setError('请等待当前页保存完成后再添加页面'); return; }
    const source = project.pages.find(page => page.page_id === pageId);
    if (!source) { setError('当前页已变化，请刷新项目后重试'); return; }
    try {
      const added = await addScenePage(project, { insertAt: source.order_index + 1,
        ...(copy ? { copyFrom: source } : {}) });
      const updated = await getSceneProject(project.project_id);
      setProject(updated); setPageId(added.page_id);
    } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
  };

  if (!project || !scene || !base) return <div className="scene-loading">{error || '正在加载对象编辑器…'}</div>;
  const scale = zoom;
  const selectedFrame = selectedElement?.frame;
  return <div className="scene-workspace">
    <header className="scene-toolbar">
      <button className="scene-icon" onClick={() => navigate('/scene')} title="返回项目列表"><ArrowLeft size={20} /></button>
      <div className="scene-project-name"><strong>{project.title}</strong><small>{project.pages.length} 页 · 对象编辑</small></div>
      <span className={`scene-save-state ${status.includes('冲突') ? 'danger' : ''}`}>{status}</span>
      {status.startsWith('离线草稿') && <button onClick={() => void drain()}>重试保存</button>}
      {status.startsWith('版本冲突') && !conflictServer && <button onClick={async () => {
        if (!id || !pageId) return;
        try { setConflictServer(await getPageScene(id, pageId)); }
        catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
      }}>重试读取服务器版本</button>}
      <div className="scene-toolbar-spacer" />
      <button className="scene-icon" title="撤销" disabled={historyPending} onClick={() => void historyMove('undo')}><Undo2 size={19} /></button>
      <button className="scene-icon" title="重做" disabled={historyPending} onClick={() => void historyMove('redo')}><Redo2 size={19} /></button>
      <span className="scene-divider" />
      <button onClick={addText}><Plus size={18} /> 文字</button>
      <button onClick={() => fileInput.current?.click()}><ImagePlus size={18} /> 图片</button>
      <button onClick={openOutline}><FileText size={18} /> 大纲</button>
      <input hidden ref={fileInput} type="file" aria-label="上传对象图片" accept="image/png,image/jpeg,image/webp" onChange={e => { void addImage(e.target.files?.[0]); e.target.value = ''; }} />
      <input hidden ref={backgroundInput} type="file" aria-label="上传背景图片文件" accept="image/png,image/jpeg,image/webp" onChange={e => { void replaceBackground(e.target.files?.[0]); e.target.value = ''; }} />
      <button className="scene-export-button" onClick={openExportDialog}><Download size={18} /> 导出</button>
    </header>
    <div className="scene-editor-grid">
      <aside className="scene-pages">
        <div className="scene-panel-heading"><b>页面</b><div className="scene-page-actions">
          <button title="复制当前页" aria-label="复制当前页"
            onPointerDown={event => { if (composingText.current) event.preventDefault(); }}
            onClick={() => void insertCurrentPage(true)}><Copy size={16} /></button>
          <button title="在当前页后插入空白页" aria-label="在当前页后插入空白页"
            onPointerDown={event => { if (composingText.current) event.preventDefault(); }}
            onClick={() => void insertCurrentPage(false)}><Plus size={17} /></button>
        </div></div>
        {project.pages.map((page, index) => <button key={page.page_id}
          className={`scene-page-tile ${pageId === page.page_id ? 'active' : ''}`}
          onPointerDown={event => { if (composingText.current) event.preventDefault(); }} onClick={() => {
            if (page.page_id === pageId) return;
            if (historyMoving.current) { setError('请等待撤销或重做完成后再切换页面'); return; }
            if (drag.current || composingText.current) { setError('请先完成当前对象操作或中文输入再切换页面'); return; }
            if (pendingTextId.current) flushText(pendingTextId.current);
            if (queue.current.length || draining.current) { setError('请等待当前页保存完成后再切换页面'); return; }
            setPageId(page.page_id);
          }}>
          <span className="scene-page-number">{String(index + 1).padStart(2, '0')}</span>
          <div className="scene-thumb" style={{ aspectRatio: `${project.canvas.width_pt}/${project.canvas.height_pt}` }}><div className="scene-thumb-inner"
            style={{ '--scene-thumb-width-factor': 960 / project.canvas.width_pt } as CSSProperties}>{thumbs[page.page_id] &&
            <SceneRenderer scene={thumbs[page.page_id].scene} assets={thumbs[page.page_id].asset_urls || assetUrls} />}</div></div>
          <span className="scene-page-title">{page.outline_content?.title || `第 ${index + 1} 页`}</span>
        </button>)}
      </aside>
      <main className="scene-stage" onPointerDown={event => { if (event.target === event.currentTarget) setSelected([]); }}>
        <div className="scene-stage-top"><span>第 {project.pages.findIndex(p => p.page_id === pageId) + 1} 页</span><span>拖动对象调整位置，双击文字直接编辑</span></div>
        <div className="scene-canvas-scroll">
          <div className="scene-canvas-holder" style={{ width: `${scene.canvas.width_pt * 96 / 72 * scale}px`,
            height: `${scene.canvas.height_pt * 96 / 72 * scale}px` }}>
            <div className="scene-canvas-scaled" style={{ transform: `scale(${scale})` }}>
              <SceneRenderer scene={scene} assets={assetUrls} selected={selected} onSelect={select}
                onPointerStart={pointerStart} onEditText={elementId => setEditingText(elementId)}
                onClearSelection={() => setSelected([])} />
              {selected.length === 1 && selectedFrame && !selectedElement?.locked && <div className="scene-selection-box"
                style={{ left: `${selectedFrame.x}pt`, top: `${selectedFrame.y}pt`, width: `${selectedFrame.w}pt`, height: `${selectedFrame.h}pt` }}>
                <button className="scene-resize-handle" aria-label="缩放对象" onPointerDown={event => {
                  event.stopPropagation(); if (historyMoving.current || !selectedElement) return;
                  drag.current = { mode: 'resize', x: event.clientX, y: event.clientY,
                    frames: { [selectedElement.id]: { ...selectedElement.frame } }, ids: [selectedElement.id] };
                }} />
              </div>}
              {editingText && (() => {
                const element = scene.elements.find(e => e.id === editingText);
                if (element?.kind !== 'text') return null;
                return <textarea autoFocus className="scene-text-overlay" value={element.text} disabled={historyPending}
                  onChange={e => setTextDraft(element.id, e.target.value)}
                  onCompositionStart={beginComposition} onCompositionEnd={() => endComposition(element.id)}
                  onBlur={() => { if (composingText.current) return; flushText(element.id); setEditingText(null); }}
                  style={{ left: `${element.frame.x}pt`, top: `${element.frame.y}pt`, width: `${element.frame.w}pt`, height: `${element.frame.h}pt`,
                    fontFamily: 'NotoScene', fontSize: `${element.style.font_size_pt}pt`, lineHeight: element.style.line_height,
                    color: element.style.color, textAlign: element.style.align, padding: `${element.style.padding_pt}pt` }} />;
              })()}
            </div>
          </div>
        </div>
        <footer className="scene-zoom"><span>{scene.canvas.width_pt} × {scene.canvas.height_pt} pt</span>
          <select value={zoom} onChange={e => setZoom(Number(e.target.value))} aria-label="画布缩放">
            <option value={0.4}>40%</option><option value={0.5}>50%</option><option value={0.65}>65%</option>
            <option value={0.75}>75%</option><option value={1}>100%</option>
          </select></footer>
      </main>
      <aside className="scene-inspector">
        <div className="scene-panel-heading"><b>{selectedElements.length > 1 ? '批量操作' : selectedElement ? '对象属性' : '页面属性'}</b></div>
        {selectedElements.length > 1 ? <div className="scene-inspector-body">
          <p className="scene-help">已选 {selectedElements.length} 个对象；锁定对象不会被批量移动、删除或调层。</p>
          <div className="scene-inline-actions"><button onClick={() => moveSelectionLayer('up')}>上移一层</button>
            <button onClick={() => moveSelectionLayer('down')}>下移一层</button></div>
          <button disabled={!selectedElements.some(element => !element.locked)} onClick={deleteSelection}><Trash2 size={16} /> 删除未锁定对象</button>
        </div> : selectedElement ? <div className="scene-inspector-body">
          <div className="scene-object-kind">{selectedElement.kind === 'text' ? '文字对象' : '图片对象'} · {selectedElement.role}</div>
          <div className="scene-inline-actions">
            <button disabled={historyPending} onClick={() => void toggleLock(selectedElement.id)}>
              {selectedElement.locked ? <Unlock size={16} /> : <Lock size={16} />}{selectedElement.locked ? '解锁' : '锁定'}</button>
            <button disabled={!canEdit} onClick={deleteSelection}><Trash2 size={16} /> 删除</button>
          </div>
          {selectedElement.kind === 'text' && <>
            <label>内容<textarea rows={6} value={selectedElement.text} disabled={!canEdit}
              onChange={e => setTextDraft(selectedElement.id, e.target.value)}
              onCompositionStart={beginComposition} onCompositionEnd={() => endComposition(selectedElement.id)}
              onBlur={() => flushText(selectedElement.id)} /></label>
            <div className="scene-two-cols"><label>字号 (pt)<input key={`${selectedElement.id}:size:${selectedElement.style.font_size_pt}`}
              type="number" min="6" max="96" defaultValue={selectedElement.style.font_size_pt} disabled={!canEdit}
              onBlur={e => { const value = Number(e.currentTarget.value);
                if (e.currentTarget.value.trim() && Number.isFinite(value) && value >= 6 && value <= 96) setStyle({ font_size_pt: value });
                else e.currentTarget.value = String(selectedElement.style.font_size_pt); }}
              onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label>
              <label>颜色<input type="color" value={selectedElement.style.color} disabled={!canEdit}
                onChange={e => setStyle({ color: e.target.value })} /></label></div>
            <div className="scene-two-cols"><label>字重<select value={selectedElement.style.font_weight} disabled={!canEdit}
              onChange={e => setStyle({ font_weight: Number(e.target.value) as 400 | 700 })}><option value="400">常规</option><option value="700">加粗</option></select></label>
              <label>对齐<select value={selectedElement.style.align} disabled={!canEdit}
                onChange={e => setStyle({ align: e.target.value as TextStyle['align'] })}><option value="left">左对齐</option><option value="center">居中</option><option value="right">右对齐</option></select></label></div>
            <div className="scene-two-cols"><label>行距<input key={`${selectedElement.id}:line:${selectedElement.style.line_height}`}
              type="number" min="0.8" max="3" step="0.1" defaultValue={selectedElement.style.line_height} disabled={!canEdit}
              onBlur={e => { const value = Number(e.currentTarget.value);
                if (e.currentTarget.value.trim() && Number.isFinite(value) && value >= 0.8 && value <= 3) setStyle({ line_height: value });
                else e.currentTarget.value = String(selectedElement.style.line_height); }}
              onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label>
              <label>内边距 (pt)<input key={`${selectedElement.id}:padding:${selectedElement.style.padding_pt}`}
                type="number" min="0" max="48" step="1" defaultValue={selectedElement.style.padding_pt} disabled={!canEdit}
                onBlur={e => { const value = Number(e.currentTarget.value);
                  if (e.currentTarget.value.trim() && Number.isFinite(value) && value >= 0 && value <= 48) setStyle({ padding_pt: value });
                  else e.currentTarget.value = String(selectedElement.style.padding_pt); }}
                onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label></div>
          </>}
          {selectedElement.kind === 'image' && <><p className="scene-help">图片内部文字、图表标签不可编辑；可替换整个图片对象。</p>
            <label>适配方式<select value={selectedElement.fit} disabled={!canEdit}
              onChange={e => commit([{ op: 'set_crop', element_id: selectedElement.id, crop: selectedElement.crop,
                fit: e.target.value as 'contain' | 'cover' }])}><option value="contain">完整显示</option><option value="cover">填充裁剪</option></select></label>
            <button disabled={!canEdit} onClick={() => fileInput.current?.click()}>上传新图片</button>
            <div className="scene-inspector-section">图片裁剪与透明度</div>
            <div className="scene-two-cols">{(['x', 'y', 'w', 'h'] as const).map(field =>
              <label key={`${selectedElement.id}:${field}:${selectedElement.crop[field]}`}>裁剪 {field.toUpperCase()} (0–1)
                <input type="number" min="0" max="1" step="0.01" defaultValue={selectedElement.crop[field]} disabled={!canEdit}
                  onBlur={event => { if (!event.currentTarget.value.trim() ||
                    !setCropField(field, Number(event.currentTarget.value))) event.currentTarget.value = String(selectedElement.crop[field]); }}
                  onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /></label>)}</div>
            <div className="scene-two-cols"><label>透明度 (0–1)
              <input key={`${selectedElement.id}:opacity:${selectedElement.opacity}`} type="number" min="0" max="1" step="0.05"
                defaultValue={selectedElement.opacity} disabled={!canEdit}
                onBlur={event => { const value = Number(event.currentTarget.value);
                  if (!event.currentTarget.value.trim() || value < 0 || value > 1 || !Number.isFinite(value)) {
                    event.currentTarget.value = String(selectedElement.opacity); return;
                  }
                  if (value !== selectedElement.opacity) commit([{ op: 'set_opacity', element_id: selectedElement.id, opacity: value }]); }}
                onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /></label>
              <label>旋转 (度)
                <input key={`${selectedElement.id}:rotation:${selectedElement.frame.rotation_deg}`} type="number" min="-180" max="180" step="0.01"
                  defaultValue={selectedElement.frame.rotation_deg} disabled={!canEdit}
                  onBlur={event => { const value = Number(event.currentTarget.value);
                    if (!event.currentTarget.value.trim() || value < -180 || value > 180 || !Number.isFinite(value)) {
                      event.currentTarget.value = String(selectedElement.frame.rotation_deg); return;
                    }
                    if (value !== selectedElement.frame.rotation_deg) setFrameField('rotation_deg', value); }}
                  onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /></label></div>
            <button disabled={!canEdit} onClick={() => { commit([
              { op: 'set_background', background: { kind: 'image', asset_id: selectedElement.asset_id, crop: selectedElement.crop } },
              { op: 'delete_element', element_id: selectedElement.id },
            ]); setSelected([]); }}>设为页面背景（填充裁剪）</button></>}
          <div className="scene-inspector-section">位置与尺寸</div>
          <div className="scene-two-cols">{(['x', 'y', 'w', 'h'] as const).map(field => <label key={field}>{field.toUpperCase()} (pt)
            <input key={`${selectedElement.id}:${field}:${selectedElement.frame[field]}`} type="number"
              defaultValue={selectedElement.frame[field]} disabled={!canEdit}
              onBlur={e => { if (!e.currentTarget.value.trim() || !setFrameField(field, Number(e.currentTarget.value)))
                e.currentTarget.value = String(selectedElement.frame[field]); }}
              onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label>)}</div>
          <div className="scene-inspector-section">层级</div>
          <div className="scene-inline-actions"><button disabled={!canEdit} onClick={() => moveSelectionLayer('up')}>上移一层</button>
            <button disabled={!canEdit} onClick={() => moveSelectionLayer('down')}>下移一层</button></div>
        </div> : <div className="scene-inspector-body"><p className="scene-help">选择画布中的文字或图片可调整对象属性。</p>
          <button onClick={() => backgroundInput.current?.click()}><ImagePlus size={16} /> {scene.background.kind === 'image' ? '替换背景图片' : '上传背景图片'}</button>
          {scene.background.kind === 'solid' && <label>背景颜色<input type="color" value={scene.background.color}
            onChange={e => commit([{ op: 'set_background', background: { kind: 'solid', color: e.target.value } }])} /></label>}
          {scene.background.kind === 'image' && <button onClick={() => commit([
            { op: 'set_background', background: { kind: 'solid', color: '#FFFFFF' } },
          ])}>改为白色背景</button>}
          <button onClick={async () => { if (!id) return;
            try { setRevisions(await getRevisions(id, pageId)); }
            catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
          }}>查看版本历史</button>
          {revisions.map(r => <button key={r.revision_id} className="scene-version" onClick={async () => {
            if (!id || !baseRef.current) return;
            if (drag.current || composingText.current || queue.current.length || draining.current ||
                pendingTextId.current || status !== '已保存') { setError('请先完成当前编辑并保存，再恢复版本'); return; }
            try { replaceScene(await restoreScene(id, pageId, baseRef.current, r.revision_id)); }
            catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
          }}>版本 {r.seq} · {r.origin}</button>)}
          <button onClick={() => void loadCandidates()}>查看 AI 候选</button>
        </div>}
        <div className="scene-inspector-body scene-ai-panel">
          <div className="scene-inspector-section">AI 创作</div>
          <p className="scene-help">使用你自己的模型 Key；AI 结果先作为候选，不会直接覆盖页面。</p>
          <label>模型凭据<select value={credentialId} onChange={e => setCredentialId(e.target.value)}>
            <option value="">请选择</option>{credentials.filter(c => c.status !== 'revoked').map(c => <option key={c.credential_id} value={c.credential_id}>{c.label} · …{c.key_suffix}</option>)}
          </select></label>
          <button disabled={!credentialId || aiBusy} onClick={() => void revokeCredential()}>撤销当前 Key</button>
          <button disabled={!credentialId || credentialCheckBusy} onClick={() => void checkCredential('model_listing')}>
            {credentialCheckBusy ? '检查中…' : '检查 Key 与模型列表（不生成）'}</button>
          <details><summary>添加模型 API Key</summary>
            <label>名称<input value={credentialLabel} onChange={e => setCredentialLabel(e.target.value)} placeholder="我的 Sub2API Key" /></label>
            <label>网关 URL<input value={gatewayUrl} onChange={e => setGatewayUrl(e.target.value)} placeholder="https://example.com/v1" /></label>
            <label>API Key<input type="password" autoComplete="off" value={apiKey} onChange={e => setApiKey(e.target.value)} /></label>
            <button onClick={() => void createCredential()}>安全保存凭据</button>
          </details>
          <div className="scene-two-cols"><label>文本模型<input value={textModel} onChange={e => setTextModel(e.target.value)} placeholder="模型 ID" /></label>
            <label>图片模型<input value={imageModel} onChange={e => setImageModel(e.target.value)} placeholder="模型 ID" /></label></div>
          <button disabled={!textModel.trim() || aiBusy} onClick={() => void configuredProject(false)
            .then(() => setAiTask('模型配置已保存，可分析模板参考页'))
            .catch((e: any) => setError(e?.response?.data?.error?.message || e.message))}>保存模型配置</button>
          <div className="scene-inline-actions"><button disabled={!credentialId || credentialCheckBusy || !textModel.trim()}
            onClick={() => void checkCredential('text_generation')}>试调用文本模型 · 可能计费</button>
            <button disabled={!credentialId || credentialCheckBusy || !imageModel.trim()}
              onClick={() => void checkCredential('image_generation')}>试调用图片模型 · 可能计费</button></div>
          <QueueCapacity />
          <StorageCapacity />
          <button disabled={aiBusy} onClick={() => void runGeneration('current')}>确认大纲并生成当前页</button>
          <details className="scene-generation-selection"><summary>指定多页生成</summary>
            <div>{project.pages.map((item, index) => <label key={item.page_id}>
              <input type="checkbox" checked={generationPageIds.includes(item.page_id)} onChange={event =>
                setGenerationPageIds(current => event.target.checked
                  ? [...current, item.page_id] : current.filter(value => value !== item.page_id))} />
              第 {index + 1} 页 · {item.outline_content?.title || '未命名'}</label>)}</div>
            <button disabled={aiBusy || !project.pages.some(item => generationPageIds.includes(item.page_id))}
              onClick={() => void runGeneration('selected')}>确认大纲并生成选中页</button>
          </details>
          <button disabled={aiBusy} onClick={() => void runGeneration('all')}>确认大纲并生成全部页面</button>
          <label>自然语言修改<textarea rows={3} value={aiInstruction} onChange={e => setAiInstruction(e.target.value)} placeholder="例如：精简标题，保留其他对象" /></label>
          <button disabled={aiBusy} onClick={() => void runAiEdit()}>生成修改候选{selected.length ? `（选中 ${selected.length} 个对象）` : '（当前页）'}</button>
          {aiTask && <p className="scene-help" role="status">{aiTask}</p>}
          {taskRows.length > 0 && <div className="scene-task-list">
            <div className="scene-inline-actions"><button onClick={() => void refreshTasks()}>刷新任务状态</button>
              {taskRows.some(row => ['queued', 'running', 'waiting_assets'].includes(row.state)) &&
                <button onClick={() => void cancelTasks()}>取消进行中任务</button>}</div>
            {taskRows.map((row, index) => <div key={row.task_id} className="scene-task-row">
              <span>{row.page_id ? `第 ${project.pages.findIndex(p => p.page_id === row.page_id) + 1} 页` : `任务 ${index + 1}`}
                {row.operation === 'generate_asset' ? ' · 图片素材' : ''}
                {' · '}{row.state}{row.error_code ? ` · ${row.error_code}` : ''}</span>
              {row.operation === 'generate_outline' && row.state === 'succeeded' &&
                <button onClick={() => { setOutlineTaskId(row.task_id); setOutlineOpen(true); }}>查看大纲候选</button>}
              {['failed', 'outcome_unknown'].includes(row.state) &&
                <button onClick={() => void retryTask(row)}>重试{row.possible_charge ? '（可能再次计费）' : ''}</button>}
            </div>)}</div>}
          <button onClick={() => void loadCandidates()}>审核候选</button>
        </div>
        <div className="scene-inspector-body scene-ai-panel"><TemplateImport projectId={project.project_id} projectVersion={project.project_version}
          page={project.pages.find(p => p.page_id === pageId)!} pages={project.pages} credentialId={credentialId}
          onError={setError} canBind={() => !drag.current && !composingText.current && !historyMoving.current &&
            !queue.current.length && !draining.current &&
            !pendingTextId.current && status === '已保存'} onProjectUpdated={updated => {
            setProject(updated);
            if (drag.current || composingText.current || historyMoving.current ||
                queue.current.length || draining.current || pendingTextId.current) {
              setError('模板已更新，但当前页有未保存编辑；草稿已保留，请先处理保存/版本冲突');
              return;
            }
            if (id && pageId) void getPageScene(id, pageId).then(response => {
              if (drag.current || composingText.current || historyMoving.current || queue.current.length ||
                  draining.current || pendingTextId.current || response.page_id !== baseRef.current?.page_id ||
                  response.page_version < baseRef.current.page_version) return;
              replaceScene(response);
            })
              .catch(e => setError(e?.message || '当前页状态刷新失败'));
          }} onPageUpdated={updated => {
            setProject(current => current ? { ...current, project_version: current.project_version + 1,
              pages: current.pages.map(p => p.page_id === updated.page_id ? updated : p) } : null);
            if (id) void getSceneProject(id).then(setProject).catch(e => setError(e?.message || '项目状态刷新失败'));
            if (updated.page_id === pageId && baseRef.current &&
                baseRef.current.revision_id === updated.revision_id) {
              const response = { ...baseRef.current, page_version: updated.page_version };
              baseRef.current = response; setBase(response);
            }
          }} /></div>
      </aside>
    </div>
    {error && <div className="scene-error" role="alert"><span>{error}</span><button onClick={() => setError('')}><X size={16} /></button></div>}
    {conflictServer && scene && <div className="scene-modal-backdrop"><div className="scene-modal scene-candidate-modal" role="dialog" aria-modal="true" aria-label="解决版本冲突">
      <h2>{status.startsWith('未保存草稿') ? '发现未保存的浏览器草稿' : '版本冲突 · 草稿未覆盖'}</h2>
      <p>请先比较服务器版本与本地草稿，再决定是否重放操作；若对象已被删除或锁定，服务器会拒绝保存。</p>
      <div className="scene-candidate-preview-grid">
        <div><small>服务器当前版本</small><div className="scene-candidate-preview-inner"><SceneRenderer scene={conflictServer.scene} assets={conflictServer.asset_urls} /></div></div>
        <div><small>本地未保存草稿</small><div className="scene-candidate-preview-inner"><SceneRenderer scene={scene} assets={assetUrls} /></div></div>
      </div>
      <div className="scene-modal-actions"><button onClick={() => void resolveConflict(true)}>将草稿应用到最新版本</button>
        <button onClick={() => { if (window.confirm('放弃本标签页未保存的修改，使用服务器版本？')) void resolveConflict(false); }}>放弃草稿</button></div>
    </div></div>}
    {outlineOpen && <OutlineEditor project={project} credentialId={credentialId} textModel={textModel}
      taskId={outlineTaskId} configureProject={() => configuredProject(false)}
      canSave={() => !drag.current && !composingText.current && !queue.current.length &&
        !draining.current && !pendingTextId.current && !historyMoving.current && status === '已保存'}
      onClose={() => setOutlineOpen(false)} onTask={task => setTaskRows(items =>
        [...items.filter(item => item.task_id !== task.task_id), task])}
      onSaved={async updated => {
        setProject(updated);
        if (id && updated.pages.some(page => page.page_id === pageId)) replaceScene(await getPageScene(id, pageId));
        else setPageId(updated.pages[0].page_id);
      }} />}
    {exportOpen && <div className="scene-modal-backdrop"><div className="scene-modal" role="dialog" aria-modal="true" aria-label="确认导出">
      <button className="scene-modal-close" onClick={() => setExportOpen(false)}><X size={18} /></button>
      <h2>确认并导出</h2><p>仅将选中页按项目当前页序和已保存版本创建不可变快照；未选页不会进入文件。导出期间继续编辑不会改变这次文件。</p>
      <fieldset className="scene-export-pages" disabled={!!exportBusy}>
        <legend>导出页面（已选 {exportPageIds.length}/{project.pages.length}）</legend>
        <button type="button" onClick={() => setExportPageSelection(project.pages.map(page => page.page_id))}>全选</button>
        <button type="button" onClick={() => setExportPageSelection([])}>清空</button>
        <div className="scene-export-page-list">{project.pages.map((page, index) => <label key={page.page_id}>
          <input type="checkbox" checked={exportPageIds.includes(page.page_id)} onChange={event =>
            setExportPageSelection(event.target.checked ? [...exportPageIds, page.page_id] :
              exportPageIds.filter(id => id !== page.page_id))} />
          第 {index + 1} 页：{page.outline_content?.title || '未命名页面'}
        </label>)}</div>
      </fieldset>
      <div className="scene-export-preflight" role="status">
        <b>导出前检查</b>：{!exportPageIds.length ? '请先选择页面。' :
          exportPreflightStatus === 'checking' ? '正在检查所选页…' :
          exportPreflightStatus === 'error' ? '预检暂不可用；提交时仍会进行权威校验。' :
          exportPreflight?.ready ? `已检查 ${exportPreflight.page_count} 页，未发现结构阻断项；提交时将再次校验。` :
          exportPreflight ? <ul>{exportPreflight.blockers.map((blocker, index) => {
            const number = project.pages.findIndex(page => page.page_id === blocker.page_id) + 1;
            return <li key={`${blocker.code}:${index}`}>{number > 0 ? `第 ${number} 页 · ` : ''}{blocker.code}：
              {blocker.code === 'TEXT_OVERFLOW' ? '文字溢出，请扩大文字框、精简内容或拆页。' :
                blocker.code === 'FONT_GLYPH_UNAVAILABLE' ? '固定字体缺少文字中的字符，请替换后重试。' :
                blocker.code === 'ASSET_UNAVAILABLE' || blocker.code === 'ASSET_NOT_READY' ? '图片素材缺失或损坏，请替换。' :
                blocker.message}</li>;
          })}</ul> : '等待检查。'}
      </div>
      <div className="scene-export-note"><b>PPTX</b>：标题、正文与独立标注是可编辑文字；图片和图表可整体移动或替换。<br />
        <b>PDF</b>：文字可搜索和复制；图片内部内容仍是图片。</div>
      <div className="scene-export-note" role="note">
        <b>PPTX 客户端字体</b>：PowerPoint/WPS 不会从此文件自动安装字体；若缺少字体，客户端可能替换字形或改变换行。<br />
        {fontManifest ? <>
          请在打开 PPTX 的电脑安装 <b>{fontManifest.family} {fontManifest.style}</b>（{fontManifest.format}）。
          <a href={assetUrl(fontManifest.download_url)} download="NotoSansCJKsc-Regular.otf">下载当前固定字体</a>。<br />
          字体包：{fontManifest.font_manifest_id}；SHA-256：<code>{fontManifest.sha256}</code>。
          请将此值与导出质量报告的 <code>font_sha256</code> 核对；不一致时不要按此字体复核。
          {project && fontManifest.font_manifest_id !== project.font_manifest_id && <span>项目字体版本与当前字体包不一致，不能导出 PPTX。</span>}
        </> : fontManifestError || '正在核验固定字体包…'}
      </div>
      <div className="scene-modal-actions"><button disabled={!!exportBusy || !exportPageIds.length || exportPreflight?.ready === false || !fontManifest || fontManifest.font_manifest_id !== project?.font_manifest_id} onClick={() => void exportDeck('pptx')}>{exportBusy === 'pptx' ? '正在导出…' : '导出 PPTX'}</button>
        <button disabled={!!exportBusy || !exportPageIds.length || exportPreflight?.ready === false} onClick={() => void exportDeck('pdf')}>{exportBusy === 'pdf' ? '正在导出…' : '导出 PDF'}</button></div>
      {recentExports.length > 0 && <div className="scene-export-result"><strong>历史导出</strong>
        {recentExports.map(entry => <button key={entry.export_id} onClick={() => void openRecentExport(entry)}>
          {entry.format.toUpperCase()} · {entry.status} · {new Date(entry.created_at).toLocaleString()}</button>)}
        {exportCursor && <button onClick={async () => {
          if (!project) return;
          try {
            const page = await listSceneExports(project.project_id, exportCursor);
            setRecentExports(rows => [...rows, ...page.items]);
            setExportCursor(page.next_cursor);
          } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
        }}>加载更多</button>}</div>}
      {lastExport && <div className="scene-export-result" role="status">
        {lastExport.format.toUpperCase()} · {lastExport.status === 'needs_review' ? '结构与文字检查通过，等待视觉复核' :
          lastExport.status === 'succeeded' ? '复核已确认' :
          lastExport.status === 'failed' ? '导出失败' : '仍在后台处理'}。
        <button onClick={async () => {
          if (!project) return;
          try {
            const result = await getExport(project.project_id, lastExport.exportId);
            setLastExport(current => current ? { ...current, status: result.status,
              downloadUrl: result.download_url, reviewFileUrl: result.review_file_url,
              reportUrl: result.report_url, reportHash: result.report_sha256,
              visualComparison: result.visual_comparison, evidence: result.visual_evidence } : null);
          } catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
        }}>刷新导出状态</button>
        {lastExport.reviewFileUrl && <a href={assetUrl(lastExport.reviewFileUrl) + '&download=1'} target="_blank" rel="noreferrer">下载待审核文件</a>}
        {lastExport.downloadUrl && <a href={assetUrl(lastExport.downloadUrl) + '&download=1'} target="_blank" rel="noreferrer">下载文件</a>}
        {lastExport.reportUrl && <a href={assetUrl(lastExport.reportUrl)} target="_blank" rel="noreferrer">查看质量报告</a>}
        {lastExport.evidence.map((entry, index) => <a key={entry.page_id} href={assetUrl(entry.url)} target="_blank" rel="noreferrer">第 {index + 1} 页视觉对照</a>)}
        {lastExport.visualComparison === 'not_run' && <p>隔离渲染器未完成视觉对照，不能在此确认通过。</p>}
        {lastExport.status === 'needs_review' && lastExport.visualComparison === 'needs_review' &&
          <button onClick={() => void approveExport()}>确认已复核视觉差异</button>}
      </div>}
    </div></div>}
    {candidateOpen && <div className="scene-modal-backdrop"><div className="scene-modal scene-candidate-modal" role="dialog" aria-modal="true" aria-label="AI 候选">
      <button className="scene-modal-close" onClick={() => setCandidateOpen(false)}><X size={18} /></button><h2>AI 候选</h2>
      {reviewedCandidateIds.length > 1 && <button className="scene-batch-accept" onClick={() => void acceptReviewed()}>
        批量接受已预览候选（{reviewedCandidateIds.length} 页）</button>}
      {candidates.length === 0 ? <p>当前没有候选页面。</p> : candidates.map(c => <div className="scene-candidate" key={c.candidate_id}>
        <span>第 {project.pages.findIndex(p => p.page_id === c.page_id) + 1} 页 · {c.state}</span>
        <CandidateChangeSummary summary={c.change_summary} />
        {c.change_summary.warnings?.includes('IMAGE_TEXT_UNVERIFIED') &&
          <small>请检查生成图片中是否烘焙正文或与原生文字重影</small>}
        <button onClick={async () => { if (!id) return;
          try {
            const preview = await getPageScene(id, c.page_id, c.revision_id);
            setCandidatePreview(preview);
            setReviewedCandidateId(c.candidate_id);
            setReviewedCandidateIds(current => {
              const otherPages = current.filter(candidateId =>
                candidates.find(candidate => candidate.candidate_id === candidateId)?.page_id !== c.page_id);
              return c.state === 'pending' ? [...otherPages, c.candidate_id] : otherPages;
            });
          } catch (e: any) {
            setReviewedCandidateIds(current => current.filter(candidateId => candidateId !== c.candidate_id));
            setError(e?.response?.data?.error?.message || e.message);
          }
        }}>预览</button>
        {c.state === 'pending' && <button disabled={reviewedCandidateId !== c.candidate_id}
          onClick={() => void acceptOne(c.candidate_id)}>接受候选</button>}
        {c.state === 'pending' && <button onClick={async () => { if (!id) return;
          try { await rejectCandidate(id, c.candidate_id); setCandidates(await getCandidates(id));
            setReviewedCandidateIds(current => current.filter(item => item !== c.candidate_id));
            if (reviewedCandidateId === c.candidate_id) { setReviewedCandidateId(null); setCandidatePreview(null); } }
          catch (e: any) { setError(e?.response?.data?.error?.message || e.message); }
        }}>拒绝候选</button>}</div>)}
      {candidatePreview && <div className="scene-candidate-preview"><strong>当前版本与候选对照</strong>
        <div className="scene-candidate-preview-grid">
          <div><small>当前已接受版本</small><div className="scene-candidate-preview-inner">
            {thumbs[candidatePreview.page_id] && <SceneRenderer scene={thumbs[candidatePreview.page_id].scene}
              assets={thumbs[candidatePreview.page_id].asset_urls || assetUrls} />}</div></div>
          <div><small>AI 候选版本</small><div className="scene-candidate-preview-inner">
            <SceneRenderer scene={candidatePreview.scene} assets={candidatePreview.asset_urls} /></div></div>
        </div></div>}
    </div></div>}
  </div>;
}
