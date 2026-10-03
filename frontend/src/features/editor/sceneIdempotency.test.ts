import { webcrypto } from 'node:crypto';
import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('Scene request idempotency', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto);
    sessionStorage.clear();
    vi.resetModules();
  });

  it('reuses a pending key across retries and reloads, but not after success', async () => {
    const firstModule = await import('./sceneIdempotency');
    const path = '/api/v2/projects/p/exports';
    const payload = { snapshot_id: 's', format: 'pdf' };
    const first = await firstModule.pendingRequestKey(path, payload);
    expect((await firstModule.pendingRequestKey(path, payload)).key).toBe(first.key);
    expect((await firstModule.pendingRequestKey(path, { ...payload, format: 'pptx' })).key).not.toBe(first.key);
    vi.resetModules();
    const reloaded = await import('./sceneIdempotency');
    expect((await reloaded.pendingRequestKey(path, payload)).key).toBe(first.key);
    reloaded.requestSucceeded(first.fingerprint);
    expect((await reloaded.pendingRequestKey(path, payload)).key).not.toBe(first.key);
  });

  it('does not replay a request after server-side dedupe retention may have expired', async () => {
    const module = await import('./sceneIdempotency');
    const path = '/api/v2/projects/p/generation-tasks';
    const body = { plan_id: 'plan-1', targets: ['page-1'] };
    const first = await module.pendingRequestKey(path, body);
    sessionStorage.setItem(`banana-scene-idempotency-v1:${first.fingerprint}`,
      JSON.stringify({ key: first.key, createdAt: Date.now() - 8 * 24 * 60 * 60 * 1000 }));
    vi.resetModules();
    const reloaded = await import('./sceneIdempotency');
    await expect(reloaded.pendingRequestKey(path, body)).rejects.toThrow('幂等记录保留期');
  });

  it('blocks a paid retry when the persisted request key is corrupted', async () => {
    const module = await import('./sceneIdempotency');
    const path = '/api/v2/projects/p/generation-tasks';
    const body = { plan_id: 'plan-2' };
    const first = await module.pendingRequestKey(path, body);
    sessionStorage.setItem(`banana-scene-idempotency-v1:${first.fingerprint}`, '{invalid');
    vi.resetModules();
    const reloaded = await import('./sceneIdempotency');
    await expect(reloaded.pendingRequestKey(path, body)).rejects.toThrow('幂等请求记录损坏');
    sessionStorage.setItem(`banana-scene-idempotency-v1:${first.fingerprint}`, 'null');
    await expect(reloaded.pendingRequestKey(path, body)).rejects.toThrow('幂等请求记录损坏');
  });
});
