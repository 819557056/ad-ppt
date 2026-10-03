import { apiClient, getBaseURL } from '@/api/client';
import type { Command, SceneCandidate, SceneProject, SceneResponse } from './sceneTypes';
import { clearRequestKey, pendingRequestKey, requestSucceeded } from './sceneIdempotency';

const root = '/api/v2';
const data = <T>(response: { data: { data: T } }): T => response.data.data;
export const assetUrl = (url: string) => `${getBaseURL()}${url}`;

export type StorageUsage = { used_bytes: number; limit_bytes: number; available_bytes: number; file_count: number };
export type SceneStorageCapacity = { owner: StorageUsage; global: StorageUsage; disk_free_bytes: number;
  min_free_bytes: number; includes_uncommitted_files: boolean };
export async function getSceneStorageCapacity() {
  return data<SceneStorageCapacity>(await apiClient.get(`${root}/storage-capacity`));
}

export type QueueUsage = { active_items: number; reserved_units: number; limit_units: number; available_units: number };
export type SceneQueueCapacity = { owner: QueueUsage; global: QueueUsage; max_generated_assets_per_page: number };
export async function getSceneQueueCapacity() {
  return data<SceneQueueCapacity>(await apiClient.get(`${root}/queue-capacity`));
}

export type SceneFontManifest = { font_manifest_id: string; family: string; style: string;
  format: string; sha256: string; download_url: string };
export async function getSceneFontManifest() {
  return data<SceneFontManifest>(await apiClient.get(`${root}/font-manifest`));
}

async function idempotent<T>(method: 'post' | 'patch' | 'delete', path: string, body: unknown,
  retainUntilTerminal = false): Promise<T> {
  const { key, fingerprint } = await pendingRequestKey(path, body);
  const response = method === 'delete'
    ? await apiClient.delete(path, { data: body, headers: { 'Idempotency-Key': key } })
    : await apiClient[method](path, body, { headers: { 'Idempotency-Key': key } });
  const result = data<T>(response);
  if (result === undefined || result === null) throw new Error('服务器响应缺少 data；请求键已保留，请查询状态后重试');
  if (!retainUntilTerminal) requestSucceeded(fingerprint);
  return result;
}

async function listAll<T>(path: string, params: Record<string, string> = {}): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    const page: { items: T[]; next_cursor: string | null } = data(await apiClient.get(path,
      { params: { ...params, limit: 100, ...(cursor ? { cursor } : {}) } }));
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && seen.has(cursor)) throw new Error('列表游标重复，已停止加载');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

export async function listSceneProjects(cursor?: string) {
  return data<{ items: SceneProject[]; next_cursor: string | null }>(await apiClient.get(`${root}/projects`,
    { params: { limit: 20, ...(cursor ? { cursor } : {}) } }));
}
export async function createSceneProject(title: string, prompt: string, pages: { title: string; points: string[] }[],
  canvas: { width_pt: number; height_pt: number }) {
  return idempotent<SceneProject>('post', `${root}/projects`, { title, prompt, pages, canvas });
}
export async function getSceneProject(id: string) { return data<SceneProject>(await apiClient.get(`${root}/projects/${id}`)); }
export async function archiveSceneProject(id: string) {
  return idempotent<{ archived: boolean }>('delete', `${root}/projects/${id}`, {});
}
export async function saveSceneOutline(project: SceneProject, pages: { page_id?: string; outline: import('./sceneTypes').OutlineContent }[]) {
  return idempotent<SceneProject>('patch', `${root}/projects/${project.project_id}/outline`,
    { base_project_version: project.project_version, pages });
}
export async function deleteScenePage(project: SceneProject, page: SceneProject['pages'][number]) {
  return idempotent<SceneProject>('delete', `${root}/projects/${project.project_id}/pages/${page.page_id}`,
    { base_project_version: project.project_version, base_page_version: page.page_version });
}
export async function addScenePage(project: SceneProject, options: {
  insertAt?: number; copyFrom?: SceneProject['pages'][number] } = {}) {
  return idempotent<SceneProject['pages'][number]>('post', `${root}/projects/${project.project_id}/pages`,
    { base_project_version: project.project_version,
      ...(options.insertAt === undefined ? {} : { insert_at: options.insertAt }),
      ...(options.copyFrom ? {
        copy_from_page_id: options.copyFrom.page_id,
        copy_from_page_version: options.copyFrom.page_version,
        copy_from_revision_id: options.copyFrom.revision_id
      } : { outline: { title: '', points: [] } }) });
}
export async function getPageScene(projectId: string, pageId: string, revisionId?: string) {
  return data<SceneResponse>(await apiClient.get(`${root}/projects/${projectId}/pages/${pageId}/scene`,
    { params: revisionId ? { revision_id: revisionId } : undefined }));
}
export async function saveSceneCommands(projectId: string, pageId: string, base: SceneResponse, commands: Command[]) {
  return idempotent<SceneResponse>('patch', `${root}/projects/${projectId}/pages/${pageId}/scene`,
    { base_revision_id: base.revision_id, base_page_version: base.page_version, commands });
}
export async function restoreScene(projectId: string, pageId: string, base: SceneResponse, revisionId: string) {
  return idempotent<SceneResponse>('post', `${root}/projects/${projectId}/pages/${pageId}/restore`,
    { base_revision_id: base.revision_id, base_page_version: base.page_version, target_revision_id: revisionId });
}
export async function uploadSceneImage(projectId: string, file: File) {
  const path = `${root}/projects/${projectId}/assets`;
  const { key, fingerprint } = await pendingRequestKey(path, await fileUploadFingerprint(file));
  const form = new FormData(); form.append('file', file);
  const result = data<{ asset_id: string; url: string }>(await apiClient.post(path, form,
    { headers: { 'Idempotency-Key': key } }));
  if (!result?.asset_id) throw new Error('服务器响应缺少 asset_id；请求键已保留，请查询素材后重试');
  requestSucceeded(fingerprint);
  return result;
}
export async function getRevisions(projectId: string, pageId: string) {
  return listAll<{ revision_id: string; seq: number; origin: string; created_at: string }>(
    `${root}/projects/${projectId}/pages/${pageId}/revisions`);
}
export async function createSnapshot(project: SceneProject, pendingCandidatePolicy?: 'export_accepted',
  selectedPageIds?: string[]) {
  return idempotent<{ snapshot_id: string }>('post', `${root}/projects/${project.project_id}/snapshots`,
    snapshotPayload(project, pendingCandidatePolicy, selectedPageIds), true);
}
export type SnapshotPreflight = { ready: boolean; page_count: number; blockers: {
  code: string; message: string; page_id: string | null; details: Record<string, unknown> }[] };
export async function getSnapshotPreflight(projectId: string, selectedPageIds: string[]) {
  return data<SnapshotPreflight>(await apiClient.get(
    `${root}/projects/${projectId}/snapshots/preflight`,
    { params: { page_ids: selectedPageIds.join(',') } }));
}
function snapshotPayload(project: SceneProject, pendingCandidatePolicy?: 'export_accepted', selectedPageIds?: string[]) {
  const chosen = selectedPageIds ? new Set(selectedPageIds) : new Set(project.pages.map(p => p.page_id));
  if (!chosen.size || (selectedPageIds && chosen.size !== selectedPageIds.length) ||
      project.pages.filter(p => chosen.has(p.page_id)).length !== chosen.size)
    throw new Error('请选择项目中至少一页且不要重复选择');
  return { project_version: project.project_version, pages: project.pages.filter(p => chosen.has(p.page_id)).map(p => ({ page_id: p.page_id,
      page_version: p.page_version, revision_id: p.revision_id })),
      ...(pendingCandidatePolicy ? { pending_candidate_policy: pendingCandidatePolicy } : {}) };
}
export async function createExport(projectId: string, snapshotId: string, format: 'pptx' | 'pdf') {
  return idempotent<{ export_id: string; task_id: string }>('post', `${root}/projects/${projectId}/exports`,
    exportPayload(snapshotId, format), true);
}
function exportPayload(snapshotId: string, format: 'pptx' | 'pdf') {
  return { snapshot_id: snapshotId, format, options: { quality_profile: 'standard' } };
}
export async function finishExportRequest(project: SceneProject, pendingCandidatePolicy: 'export_accepted' | undefined,
  snapshotId: string, format: 'pptx' | 'pdf', selectedPageIds?: string[]) {
  await clearRequestKey(`${root}/projects/${project.project_id}/exports`, exportPayload(snapshotId, format));
  await clearRequestKey(`${root}/projects/${project.project_id}/snapshots`, snapshotPayload(project, pendingCandidatePolicy, selectedPageIds));
}
export type SceneExportSummary = { export_id: string; format: 'pptx' | 'pdf'; status: string; created_at: string };
export async function listSceneExports(projectId: string, cursor?: string) {
  return data<{ items: SceneExportSummary[]; next_cursor: string | null }>(await apiClient.get(
    `${root}/projects/${projectId}/exports`, { params: { limit: 20, ...(cursor ? { cursor } : {}) } }));
}
export async function getExport(projectId: string, exportId: string) {
  return data<{ status: string; error_code: string | null; download_url: string | null;
    review_file_url: string | null; report_url: string | null; report_sha256: string | null;
    visual_comparison: string | null; waivable_warning_codes: string[];
    visual_evidence: { page_id: string; url: string }[] }>(
    await apiClient.get(`${root}/projects/${projectId}/exports/${exportId}`));
}
export async function acceptExportReview(projectId: string, exportId: string, reportSha256: string) {
  return idempotent<{ status: string }>('post', `${root}/projects/${projectId}/exports/${exportId}/review`,
    { report_sha256: reportSha256, acknowledged_warning_codes: ['VISUAL_DIFF_UNCALIBRATED'] });
}
export async function getCandidates(projectId: string, state?: 'pending' | 'accepted' | 'rejected' | 'stale') {
  return listAll<SceneCandidate>(
    `${root}/projects/${projectId}/candidates`, state ? { state } : {});
}
export async function acceptCandidate(projectId: string, candidateId: string) {
  return acceptCandidates(projectId, [candidateId]);
}
export async function acceptCandidates(projectId: string, candidateIds: string[]) {
  if (!candidateIds.length || new Set(candidateIds).size !== candidateIds.length)
    throw new Error('请选择不重复的候选页面');
  return idempotent<SceneResponse[]>('post', `${root}/projects/${projectId}/candidates/accept`,
    { candidate_ids: candidateIds });
}
export async function rejectCandidate(projectId: string, candidateId: string) {
  return idempotent<{ candidate_id: string; state: string }>('post',
    `${root}/projects/${projectId}/candidates/${candidateId}/reject`, {});
}
export type ModelCredentialSummary = { credential_id: string; label: string; key_suffix: string; status: string;
  base_url: string; capabilities?: { models?: string[]; text_generation_verified?: boolean;
    image_generation_verified?: boolean } };
export async function listModelCredentials() {
  return listAll<ModelCredentialSummary>(`${root}/model-credentials`);
}
export async function revokeModelCredential(credentialId: string) {
  return idempotent<ModelCredentialSummary>('delete', `${root}/model-credentials/${credentialId}`, {});
}
export async function addModelCredential(label: string, baseUrl: string, apiKey: string) {
  return idempotent<ModelCredentialSummary>('post', `${root}/model-credentials`,
    { label, base_url: baseUrl, api_key: apiKey });
}
export async function checkModelCredential(projectId: string, credentialId: string,
  kind: 'model_listing' | 'text_generation' | 'image_generation' = 'model_listing', modelId?: string) {
  return idempotent<{ task_id: string; possible_charge: boolean }>('post',
    `${root}/model-credentials/${credentialId}/check`, { project_id: projectId, kind,
      ...(kind !== 'model_listing' ? { model_id: modelId, acknowledge_possible_charge: true } : {}) });
}
export async function saveModelConfig(project: SceneProject, textModel: string, imageModel?: string) {
  return idempotent<SceneProject>('patch', `${root}/projects/${project.project_id}/model-config`,
    { base_project_version: project.project_version, model_config: {
      text_model: textModel, ...(imageModel ? { image_model: imageModel } : {}) } });
}
export async function confirmGenerationPlan(project: SceneProject) {
  return idempotent<{ plan_id: string }>('post', `${root}/projects/${project.project_id}/generation-plans`,
    planPayload(project), true);
}
function planPayload(project: SceneProject) {
  return { base_project_version: project.project_version,
    expected_pages: project.pages.map(p => ({ page_id: p.page_id, page_version: p.page_version,
      revision_id: p.revision_id })) };
}
export async function startSceneGeneration(project: SceneProject, planId: string, credentialId: string,
  pageIds: string[]) {
  return idempotent<{ task_group_id: string; tasks: { page_id: string; task_id: string }[] }>(
    'post', `${root}/projects/${project.project_id}/generation-tasks`, generationPayload(project, planId, credentialId, pageIds), true);
}
function generationPayload(project: SceneProject, planId: string, credentialId: string, pageIds: string[]) {
  return {
      plan_id: planId, credential_id: credentialId,
      targets: project.pages.filter(p => pageIds.includes(p.page_id)).map(p => ({ page_id: p.page_id,
        base_revision_id: p.revision_id, base_page_version: p.page_version }))
    };
}
export async function finishGenerationRequest(project: SceneProject, planId: string, credentialId: string,
  pageIds: string[]) {
  await clearRequestKey(`${root}/projects/${project.project_id}/generation-tasks`,
    generationPayload(project, planId, credentialId, pageIds));
  await clearRequestKey(`${root}/projects/${project.project_id}/generation-plans`, planPayload(project));
}
export async function getSceneTaskGroup(groupId: string) {
  return data<{ state: string; completed_pages: number; total_pages: number;
    items: { task_id: string; page_id: string; operation: string; logical_key: string;
    state: string; error_code: string | null;
    possible_charge: boolean }[] }>(
    await apiClient.get(`${root}/task-groups/${groupId}`));
}
export async function startSceneAiEdit(projectId: string, pageId: string, base: SceneResponse,
  credentialId: string, instruction: string, elementIds: string[]) {
  return idempotent<{ task_id: string; task_group_id: string }>('post', `${root}/projects/${projectId}/pages/${pageId}/ai-edits`, {
    base_revision_id: base.revision_id, base_page_version: base.page_version,
    credential_id: credentialId, instruction, element_ids: elementIds
  });
}
export async function getSceneTask(taskId: string) {
  return data<{ state: string; error_code: string | null; result: Record<string, string>;
    possible_charge: boolean }>(
    await apiClient.get(`${root}/tasks/${taskId}`));
}
export async function listSceneProjectTasks(projectId: string) {
  return listAll<{ task_id: string; page_id: string | null; operation: string;
    state: string; error_code: string | null; possible_charge: boolean }>(
    `${root}/projects/${projectId}/tasks`);
}
export async function cancelSceneTask(taskId: string) {
  return idempotent<{ state: string }>('post', `${root}/tasks/${taskId}/cancel`, {});
}
export async function retrySceneTask(taskId: string, acknowledgePossibleCharge: boolean) {
  return idempotent<{ state: string }>('post', `${root}/tasks/${taskId}/retry`,
    { acknowledge_possible_charge: acknowledgePossibleCharge });
}
export type TemplateReference = { template_asset_id: string; template_document_id: string; source_page_index: number;
  preview_url: string | null; thumbnail_url: string | null; analysis_status: string;
  analysis_revision: number; analysis: Record<string, unknown> | null };
async function fileUploadFingerprint(file: File) {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return { filename: file.name.toLowerCase(), byte_size: file.size,
    sha256: Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('') };
}
export async function uploadTemplate(projectId: string, file: File) {
  const path = `${root}/projects/${projectId}/template-documents`;
  const { key } = await pendingRequestKey(path, await fileUploadFingerprint(file));
  const form = new FormData(); form.append('file', file);
  return data<{ document_id: string; task_id: string; status: string }>(await apiClient.post(path, form,
    { headers: { 'Idempotency-Key': key } }));
}
export async function finishTemplateUploadRequest(projectId: string, file: File) {
  await clearRequestKey(`${root}/projects/${projectId}/template-documents`,
    await fileUploadFingerprint(file));
}
export type TemplateDocumentStatus = { document_id: string; status: string; source_page_count: number;
  error_code: string | null; warnings: string[]; task_id: string | null };
export async function getTemplateDocument(projectId: string, documentId: string) {
  return data<TemplateDocumentStatus>(
    await apiClient.get(`${root}/projects/${projectId}/template-documents/${documentId}`));
}
export async function listTemplateReferences(projectId: string, documentId?: string) {
  return listAll<TemplateReference>(`${root}/projects/${projectId}/template-assets`,
    documentId ? { document_id: documentId } : {});
}
export async function bindPageTemplate(projectId: string, page: SceneProject['pages'][number], templateAssetId: string | null, styleText: string) {
  return idempotent<SceneProject['pages'][number]>('patch', `${root}/projects/${projectId}/pages/${page.page_id}/template`,
    { base_page_version: page.page_version, template_asset_id: templateAssetId, style_text: styleText });
}
export async function analyzeTemplate(projectId: string, documentId: string, credentialId: string, pageIndex: number) {
  return idempotent<{ task_group_id: string }>('post', `${root}/projects/${projectId}/template-documents/${documentId}/analyze`,
    { credential_id: credentialId, selected_page_indexes: [pageIndex] }, true);
}
export async function finishTemplateAnalysisRequest(projectId: string, documentId: string,
  credentialId: string, pageIndex: number) {
  await clearRequestKey(`${root}/projects/${projectId}/template-documents/${documentId}/analyze`,
    { credential_id: credentialId, selected_page_indexes: [pageIndex] });
}
export async function deleteTemplateReference(projectId: string, projectVersion: number,
  pages: SceneProject['pages'], item: TemplateReference) {
  return idempotent<SceneProject>('delete', `${root}/projects/${projectId}/template-assets/${item.template_asset_id}`,
    { base_project_version: projectVersion, base_analysis_revision: item.analysis_revision,
      expected_pages: pages.filter(page => page.template_asset_id === item.template_asset_id)
        .map(page => ({ page_id: page.page_id, page_version: page.page_version, revision_id: page.revision_id }))
        .sort((a, b) => a.page_id.localeCompare(b.page_id)) });
}
export async function saveTemplateProfile(projectId: string, item: TemplateReference, analysis: Record<string, unknown>) {
  return idempotent<{ analysis_revision: number }>('patch', `${root}/projects/${projectId}/template-assets/${item.template_asset_id}`,
    { base_analysis_revision: item.analysis_revision, analysis });
}
export type OutlineDraft = { base_project_version: number | null;
  pages: { role: import('./sceneTypes').OutlineRole; title: string; points: string[]; facts_needed: string[]; sources?: string[] }[] };
export async function startOutlineTask(project: SceneProject, credentialId: string, brief: string, slideCount: number) {
  return idempotent<{ task_id: string }>('post', `${root}/projects/${project.project_id}/outline-tasks`,
    { base_project_version: project.project_version, credential_id: credentialId,
      brief, slide_count: slideCount });
}
export async function getOutlineDraft(projectId: string, assetId: string) {
  return data<OutlineDraft>(await apiClient.get(`${root}/projects/${projectId}/outline-drafts/${assetId}`));
}
