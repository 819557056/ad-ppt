const prefix = 'banana-scene-idempotency-v1:';
// Server ApiIdempotencyRecord expires after seven days. Never silently replay a
// paid request with a key whose server-side record may have expired.
const maxPendingAgeMs = 6 * 24 * 60 * 60 * 1000;
type PendingKey = { key: string; createdAt: number };
const pending = new Map<string, PendingKey>();

export async function requestFingerprint(path: string, body: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify({ path, body }));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export async function pendingRequestKey(path: string, body: unknown): Promise<{ key: string; fingerprint: string }> {
  const fingerprint = await requestFingerprint(path, body);
  let record = pending.get(fingerprint);
  if (!record) {
    let raw: string | null;
    try {
      raw = sessionStorage.getItem(prefix + fingerprint);
    }
    catch { throw new Error('无法访问会话存储；为避免重复提交，请启用浏览器存储后重试'); }
    if (raw) {
      try { record = JSON.parse(raw) as PendingKey; }
      catch { throw new Error('幂等请求记录损坏；请先查询任务/导出状态，不要直接重试'); }
      if (!record || typeof record !== 'object' || typeof record.key !== 'string' ||
          typeof record.createdAt !== 'number')
        throw new Error('幂等请求记录损坏；请先查询任务/导出状态，不要直接重试');
    }
  }
  if (record && (!Number.isFinite(record.createdAt) || !record.key ||
      Date.now() - record.createdAt > maxPendingAgeMs || record.createdAt > Date.now() + 60_000))
    throw new Error('未确认请求已超过幂等记录保留期；请先查询任务/导出状态，不要直接重试可能计费的操作');
  if (!record) record = { key: crypto.randomUUID(), createdAt: Date.now() };
  try { sessionStorage.setItem(prefix + fingerprint, JSON.stringify(record)); }
  catch { throw new Error('无法保存幂等请求记录；为避免重复提交，请检查浏览器存储'); }
  pending.set(fingerprint, record);
  return { key: record.key, fingerprint };
}

export async function clearRequestKey(path: string, body: unknown): Promise<void> {
  requestSucceeded(await requestFingerprint(path, body));
}

export function requestSucceeded(fingerprint: string): void {
  pending.delete(fingerprint);
  try { sessionStorage.removeItem(prefix + fingerprint); }
  catch { /* Storage may be disabled. */ }
}
