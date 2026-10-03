import { useCallback, useEffect, useRef, useState } from 'react';
import { getSceneStorageCapacity, type SceneStorageCapacity } from './sceneApi';

function bytes(value: number) {
  const power = value > 0 ? Math.min(3, Math.floor(Math.log(value) / Math.log(1024))) : 0;
  return `${(value / 1024 ** power).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${['B', 'KiB', 'MiB', 'GiB'][power]}`;
}

export function StorageCapacity() {
  const [capacity, setCapacity] = useState<SceneStorageCapacity | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const sequence = ++request.current;
    setLoading(true); setError('');
    try {
      const result = await getSceneStorageCapacity();
      if (request.current === sequence) setCapacity(result);
    } catch {
      if (request.current === sequence) {
        setCapacity(null); setError('存储查询失败，请稍后刷新或联系管理员检查素材卷。');
      }
    } finally { if (request.current === sequence) setLoading(false); }
  }, []);
  useEffect(() => {
    void refresh();
    return () => { request.current++; };
  }, [refresh]);
  return <details className="scene-storage-capacity">
    <summary>素材存储</summary>
    {loading && <p className="scene-help" role="status">正在核对素材占用…</p>}
    {error && <p className="scene-help" role="alert">{error}</p>}
    {capacity && <>
      <p className="scene-help">我的素材：{bytes(capacity.owner.used_bytes)} / {bytes(capacity.owner.limit_bytes)}
        （{capacity.owner.file_count} 个文件）</p>
      <p className="scene-help">系统配额剩余：{bytes(capacity.global.available_bytes)}</p>
      <p className="scene-help">历史版本、导出和失败遗留文件均占空间；归档项目或移除参考页不会释放历史素材。
        请联系管理员扩容或安全清理。此查询不预留空间；模型返回后仍可能因并发写入而不足，重试可能再次计费。</p>
      {(capacity.owner.available_bytes === 0 || capacity.global.available_bytes === 0 ||
        capacity.disk_free_bytes < capacity.min_free_bytes) &&
        <p className="scene-help" role="status">存储空间不足，新增素材将被阻止。已保存内容仍可读取。</p>}
    </>}
    <button disabled={loading} onClick={() => void refresh()}>刷新存储占用</button>
  </details>;
}
