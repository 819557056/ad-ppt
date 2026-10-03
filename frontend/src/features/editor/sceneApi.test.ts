import { webcrypto } from 'node:crypto';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SceneProject } from './sceneTypes';

const { post, patch, del } = vi.hoisted(() => ({ post: vi.fn(), patch: vi.fn(), del: vi.fn() }));
vi.mock('@/api/client', () => ({ apiClient: { post, patch, delete: del }, getBaseURL: () => '' }));

const project: SceneProject = {
  project_id: 'project-1', title: 'Deck', prompt: '', editor_mode: 'scene_v1',
  project_version: 4, canvas: { width_pt: 960, height_pt: 540 }, font_manifest_id: 'fonts-v1',
  active_plan_id: null, model_config: { text_model: 'text', image_model: 'image' },
  pages: [{ page_id: 'page-1', order_index: 0, outline_content: null,
    page_version: 2, revision_id: 'revision-1' }]
};

function keyAt(index: number): string {
  return post.mock.calls[index][2].headers['Idempotency-Key'];
}

describe('Scene multi-step idempotency', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto);
    sessionStorage.clear();
    post.mockReset();
    patch.mockReset();
    del.mockReset();
  });

  it('replays reference removal with the same key and exact bound page versions', async () => {
    const { deleteTemplateReference } = await import('./sceneApi');
    const pages = [{ ...project.pages[0], template_asset_id: 'reference-1' },
      { ...project.pages[0], page_id: 'unbound', template_asset_id: null }];
    const reference = { template_asset_id: 'reference-1', template_document_id: 'document-1',
      source_page_index: 1, analysis_revision: 2, analysis_status: 'completed', analysis: null,
      preview_url: null, thumbnail_url: null };
    del.mockRejectedValueOnce(new Error('response lost'));
    await expect(deleteTemplateReference(project.project_id, project.project_version, pages, reference))
      .rejects.toThrow('response lost');
    del.mockResolvedValueOnce({ data: { data: project } });
    await deleteTemplateReference(project.project_id, project.project_version, pages, reference);
    expect(del.mock.calls[0][0]).toBe('/api/v2/projects/project-1/template-assets/reference-1');
    expect(del.mock.calls[0][1].data).toEqual({ base_project_version: 4, base_analysis_revision: 2,
      expected_pages: [{ page_id: 'page-1', page_version: 2, revision_id: 'revision-1' }] });
    expect(del.mock.calls[1][1].headers['Idempotency-Key']).toBe(del.mock.calls[0][1].headers['Idempotency-Key']);
  });

  it('reuses snapshot and export keys after a response is lost between the two steps', async () => {
    const { createSnapshot, createExport, finishExportRequest } = await import('./sceneApi');
    post.mockResolvedValueOnce({ data: { data: { snapshot_id: 'snapshot-1' } } });
    await createSnapshot(project);
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(createExport(project.project_id, 'snapshot-1', 'pdf')).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: { snapshot_id: 'snapshot-1' } } });
    expect((await createSnapshot(project)).snapshot_id).toBe('snapshot-1');
    expect(keyAt(2)).toBe(keyAt(0));
    post.mockResolvedValueOnce({ data: { data: { export_id: 'export-1', task_id: 'task-1' } } });
    await createExport(project.project_id, 'snapshot-1', 'pdf');
    expect(keyAt(3)).toBe(keyAt(1));
    await finishExportRequest(project, undefined, 'snapshot-1', 'pdf');
    post.mockResolvedValueOnce({ data: { data: { snapshot_id: 'snapshot-2' } } });
    await createSnapshot(project);
    expect(keyAt(4)).not.toBe(keyAt(0));
  });

  it('freezes only selected pages in project order and scopes replay keys to that selection', async () => {
    const { createSnapshot, finishExportRequest } = await import('./sceneApi');
    const multi: SceneProject = { ...project, pages: [project.pages[0],
      { ...project.pages[0], page_id: 'page-2', order_index: 1, revision_id: 'revision-2' },
      { ...project.pages[0], page_id: 'page-3', order_index: 2, revision_id: 'revision-3' }] };
    post.mockResolvedValue({ data: { data: { snapshot_id: 'snapshot-subset' } } });
    await createSnapshot(multi, undefined, ['page-3', 'page-1']);
    expect(post.mock.calls[0][1].pages.map((page: { page_id: string }) => page.page_id))
      .toEqual(['page-1', 'page-3']);
    await createSnapshot(multi, undefined, ['page-1', 'page-3']);
    expect(keyAt(1)).toBe(keyAt(0));
    await createSnapshot(multi, undefined, ['page-2']);
    expect(keyAt(2)).not.toBe(keyAt(0));
    await finishExportRequest(multi, undefined, 'snapshot-subset', 'pdf', ['page-1', 'page-3']);
    await createSnapshot(multi, undefined, ['page-3', 'page-1']);
    expect(keyAt(3)).not.toBe(keyAt(0));
    await expect(createSnapshot(multi, undefined, [])).rejects.toThrow('至少一页');
    await expect(createSnapshot(multi, undefined, ['page-missing'])).rejects.toThrow('项目中');
  });

  it('reuses a confirmed plan for task submission and changes keys after page edits', async () => {
    const { confirmGenerationPlan, startSceneGeneration, finishGenerationRequest } = await import('./sceneApi');
    post.mockResolvedValueOnce({ data: { data: { plan_id: 'plan-1' } } });
    await confirmGenerationPlan(project);
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(startSceneGeneration(project, 'plan-1', 'credential-1', ['page-1']))
      .rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: { plan_id: 'plan-1' } } });
    await confirmGenerationPlan(project);
    expect(keyAt(2)).toBe(keyAt(0));
    post.mockResolvedValueOnce({ data: { data: { task_group_id: 'group-1', tasks: [] } } });
    await startSceneGeneration(project, 'plan-1', 'credential-1', ['page-1']);
    expect(keyAt(3)).toBe(keyAt(1));
    await finishGenerationRequest(project, 'plan-1', 'credential-1', ['page-1']);
    post.mockResolvedValueOnce({ data: { data: { plan_id: 'plan-2' } } });
    await confirmGenerationPlan(project);
    expect(keyAt(4)).not.toBe(keyAt(0));
    const edited = { ...project, pages: [{ ...project.pages[0], page_version: 3,
      revision_id: 'revision-2' }] };
    post.mockResolvedValueOnce({ data: { data: { plan_id: 'plan-3' } } });
    await confirmGenerationPlan(edited);
    expect(keyAt(5)).not.toBe(keyAt(4));
  });

  it('keeps a paid request key if a success response has no data envelope', async () => {
    const { startSceneGeneration } = await import('./sceneApi');
    post.mockResolvedValueOnce({ data: {} });
    await expect(startSceneGeneration(project, 'plan-bad', 'credential-1', ['page-1']))
      .rejects.toThrow('服务器响应缺少 data');
    post.mockResolvedValueOnce({ data: { data: { task_group_id: 'group-1', tasks: [] } } });
    await startSceneGeneration(project, 'plan-bad', 'credential-1', ['page-1']);
    expect(keyAt(1)).toBe(keyAt(0));
  });

  it('keeps a paid template-analysis key until its task completes', async () => {
    const { analyzeTemplate, finishTemplateAnalysisRequest } = await import('./sceneApi');
    post.mockResolvedValueOnce({ data: { data: { task_group_id: 'group-1' } } });
    await analyzeTemplate('project-1', 'document-1', 'credential-1', 1);
    post.mockResolvedValueOnce({ data: { data: { task_group_id: 'group-1' } } });
    await analyzeTemplate('project-1', 'document-1', 'credential-1', 1);
    expect(keyAt(1)).toBe(keyAt(0));
    await finishTemplateAnalysisRequest('project-1', 'document-1', 'credential-1', 1);
    post.mockResolvedValueOnce({ data: { data: { task_group_id: 'group-2' } } });
    await analyzeTemplate('project-1', 'document-1', 'credential-1', 1);
    expect(keyAt(2)).not.toBe(keyAt(0));
  });

  it('reuses the same upload key when a template file is reselected after response loss', async () => {
    const { uploadTemplate, finishTemplateUploadRequest } = await import('./sceneApi');
    const file = new File([new Uint8Array([1, 2, 3])], 'reference.png', { type: 'image/png' });
    Object.defineProperty(file, 'arrayBuffer', { value: async () => new Uint8Array([1, 2, 3]).buffer });
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(uploadTemplate('project-1', file)).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: { document_id: 'document-1', task_id: 'task-1' } } });
    await uploadTemplate('project-1', file);
    expect(keyAt(1)).toBe(keyAt(0));
    await finishTemplateUploadRequest('project-1', file);
    post.mockResolvedValueOnce({ data: { data: { document_id: 'document-2', task_id: 'task-2' } } });
    await uploadTemplate('project-1', file);
    expect(keyAt(2)).not.toBe(keyAt(0));
  });

  it('reuses an image upload key after response loss, then clears it on success', async () => {
    const { uploadSceneImage } = await import('./sceneApi');
    const file = new File([new Uint8Array([1, 2, 3])], 'figure.png', { type: 'image/png' });
    Object.defineProperty(file, 'arrayBuffer', { value: async () => new Uint8Array([1, 2, 3]).buffer });
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(uploadSceneImage('project-1', file)).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: { asset_id: 'asset-1', url: '/signed-1' } } });
    await uploadSceneImage('project-1', file);
    expect(keyAt(1)).toBe(keyAt(0));
    post.mockResolvedValueOnce({ data: { data: { asset_id: 'asset-2', url: '/signed-2' } } });
    await uploadSceneImage('project-1', file);
    expect(keyAt(2)).not.toBe(keyAt(0));
  });

  it('replays page deletion and project archiving with their original keys', async () => {
    const { deleteScenePage, archiveSceneProject } = await import('./sceneApi');
    del.mockRejectedValueOnce(new Error('response lost'));
    await expect(deleteScenePage(project, project.pages[0])).rejects.toThrow('response lost');
    del.mockResolvedValueOnce({ data: { data: project } });
    await deleteScenePage(project, project.pages[0]);
    expect(del.mock.calls[1][1].headers['Idempotency-Key'])
      .toBe(del.mock.calls[0][1].headers['Idempotency-Key']);
    del.mockRejectedValueOnce(new Error('response lost'));
    await expect(archiveSceneProject('project-1')).rejects.toThrow('response lost');
    del.mockResolvedValueOnce({ data: { data: { archived: true } } });
    await archiveSceneProject('project-1');
    expect(del.mock.calls[3][1].headers['Idempotency-Key'])
      .toBe(del.mock.calls[2][1].headers['Idempotency-Key']);
  });

  it('retries candidate acceptance with the same key after response loss', async () => {
    const { acceptCandidate } = await import('./sceneApi');
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(acceptCandidate('project-1', 'candidate-1')).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: [{ revision_id: 'accepted-revision' }] } });
    await acceptCandidate('project-1', 'candidate-1');
    expect(keyAt(1)).toBe(keyAt(0));
    post.mockResolvedValueOnce({ data: { data: [{ revision_id: 'next-revision' }] } });
    await acceptCandidate('project-1', 'candidate-1');
    expect(keyAt(2)).not.toBe(keyAt(0));
  });

  it('keeps keys for rejection, cancellation, review and credential revocation until acknowledged', async () => {
    const { rejectCandidate, cancelSceneTask, acceptExportReview, revokeModelCredential } =
      await import('./sceneApi');
    const postCases = [
      { call: () => rejectCandidate('project-1', 'candidate-1'), result: { candidate_id: 'candidate-1', state: 'rejected' } },
      { call: () => cancelSceneTask('task-1'), result: { state: 'cancelled' } },
      { call: () => acceptExportReview('project-1', 'export-1', 'report-hash'), result: { status: 'succeeded' } }
    ];
    for (const item of postCases) {
      const firstIndex = post.mock.calls.length;
      post.mockRejectedValueOnce(new Error('response lost'));
      await expect(item.call()).rejects.toThrow('response lost');
      post.mockResolvedValueOnce({ data: { data: item.result } });
      await item.call();
      expect(keyAt(firstIndex + 1)).toBe(keyAt(firstIndex));
    }
    del.mockRejectedValueOnce(new Error('response lost'));
    await expect(revokeModelCredential('credential-1')).rejects.toThrow('response lost');
    del.mockResolvedValueOnce({ data: { data: { credential_id: 'credential-1', status: 'revoked' } } });
    await revokeModelCredential('credential-1');
    expect(del.mock.calls[1][1].headers['Idempotency-Key'])
      .toBe(del.mock.calls[0][1].headers['Idempotency-Key']);
  });

  it('retries page creation and revision restoration with their original keys', async () => {
    const { addScenePage, restoreScene } = await import('./sceneApi');
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(addScenePage(project)).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: project.pages[0] } });
    await addScenePage(project);
    expect(keyAt(1)).toBe(keyAt(0));
    const base = { page_id: 'page-1', page_version: 2, revision_id: 'revision-1', scene_hash: 'hash',
      scene: { schema_version: 1 as const, font_manifest_id: 'fonts-v1' as const,
        canvas: project.canvas, background: { kind: 'solid' as const, color: '#FFFFFF' }, elements: [] },
      asset_urls: {} };
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(restoreScene(project.project_id, 'page-1', base, 'revision-0')).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: base } });
    await restoreScene(project.project_id, 'page-1', base, 'revision-0');
    expect(keyAt(3)).toBe(keyAt(2));
  });

  it('sends an insertion position and source head when copying a page', async () => {
    const { addScenePage } = await import('./sceneApi');
    post.mockResolvedValueOnce({ data: { data: project.pages[0] } });
    await addScenePage(project, { insertAt: 1, copyFrom: project.pages[0] });
    expect(post.mock.calls[0][1]).toEqual({
      base_project_version: 4, insert_at: 1,
      copy_from_page_id: 'page-1', copy_from_page_version: 2,
      copy_from_revision_id: 'revision-1'
    });
  });

  it('retries outline and model configuration saves without changing request keys', async () => {
    const { saveSceneOutline, saveModelConfig } = await import('./sceneApi');
    const outline = [{ page_id: 'page-1', outline: { title: '新标题', points: [] } }];
    patch.mockRejectedValueOnce(new Error('response lost'));
    await expect(saveSceneOutline(project, outline)).rejects.toThrow('response lost');
    patch.mockResolvedValueOnce({ data: { data: project } });
    await saveSceneOutline(project, outline);
    expect(patch.mock.calls[1][2].headers['Idempotency-Key'])
      .toBe(patch.mock.calls[0][2].headers['Idempotency-Key']);
    patch.mockRejectedValueOnce(new Error('response lost'));
    await expect(saveModelConfig(project, 'text', 'image')).rejects.toThrow('response lost');
    patch.mockResolvedValueOnce({ data: { data: project } });
    await saveModelConfig(project, 'text', 'image');
    expect(patch.mock.calls[3][2].headers['Idempotency-Key'])
      .toBe(patch.mock.calls[2][2].headers['Idempotency-Key']);
  });

  it('does not allocate a new key when a charged task retry loses its response', async () => {
    const { retrySceneTask } = await import('./sceneApi');
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(retrySceneTask('task-retry', true)).rejects.toThrow('response lost');
    post.mockResolvedValueOnce({ data: { data: { task_id: 'task-retry', state: 'queued' } } });
    await retrySceneTask('task-retry', true);
    expect(keyAt(1)).toBe(keyAt(0));
  });

  it('reuses a credential-create key without persisting the plaintext secret', async () => {
    const { addModelCredential } = await import('./sceneApi');
    post.mockRejectedValueOnce(new Error('response lost'));
    await expect(addModelCredential('Mine', 'https://model.example/v1', 'short-secret'))
      .rejects.toThrow('response lost');
    expect(Object.values(sessionStorage).join(' ')).not.toContain('short-secret');
    post.mockResolvedValueOnce({ data: { data: { credential_id: 'credential-1' } } });
    await addModelCredential('Mine', 'https://model.example/v1', 'short-secret');
    expect(keyAt(1)).toBe(keyAt(0));
  });

  it('replays template binding and profile edits after a lost response', async () => {
    const { bindPageTemplate, saveTemplateProfile } = await import('./sceneApi');
    const reference = { template_asset_id: 'reference-1', template_document_id: 'document-1',
      source_page_index: 1, preview_url: null, thumbnail_url: null, analysis_status: 'completed',
      analysis_revision: 1, analysis: { role: 'content' } };
    patch.mockRejectedValueOnce(new Error('response lost'));
    await expect(bindPageTemplate('project-1', project.pages[0], 'reference-1', '简洁'))
      .rejects.toThrow('response lost');
    patch.mockResolvedValueOnce({ data: { data: project.pages[0] } });
    await bindPageTemplate('project-1', project.pages[0], 'reference-1', '简洁');
    expect(patch.mock.calls[1][2].headers['Idempotency-Key'])
      .toBe(patch.mock.calls[0][2].headers['Idempotency-Key']);
    patch.mockRejectedValueOnce(new Error('response lost'));
    await expect(saveTemplateProfile('project-1', reference, { role: 'content' }))
      .rejects.toThrow('response lost');
    patch.mockResolvedValueOnce({ data: { data: { analysis_revision: 2 } } });
    await saveTemplateProfile('project-1', reference, { role: 'content' });
    expect(patch.mock.calls[3][2].headers['Idempotency-Key'])
      .toBe(patch.mock.calls[2][2].headers['Idempotency-Key']);
  });
});
