/** Canonical encoding shared with backend/services/scene/validation.py. */
export function canonicalScene(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || Math.abs(value) > 1e12) throw new Error('number out of range');
    const fixed = value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
    if (Math.abs(value - Number(fixed || '0')) > 1e-9) throw new Error('number exceeds 6 decimals');
    return fixed && fixed !== '-0' ? fixed : '0';
  }
  if (Array.isArray(value)) return `[${value.map(canonicalScene).join(',')}]`;
  if (typeof value === 'object' && value !== undefined) {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map(key => `${JSON.stringify(key)}:${canonicalScene(object[key])}`).join(',')}}`;
  }
  throw new Error('unsupported canonical value');
}

export async function sceneHash(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalScene(value));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
