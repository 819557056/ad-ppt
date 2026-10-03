import { useCallback, useEffect, useRef, useState } from 'react';
import { getSceneQueueCapacity, type SceneQueueCapacity } from './sceneApi';

/** Informational only: task submission always rechecks capacity transactionally. */
export function QueueCapacity() {
  const [capacity, setCapacity] = useState<SceneQueueCapacity | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const sequence = ++request.current;
    setLoading(true); setError('');
    try {
      const result = await getSceneQueueCapacity();
      if (request.current === sequence) setCapacity(result);
    } catch {
      if (request.current === sequence) {
        setCapacity(null); setError('容量查询失败，请刷新后重试。');
      }
    } finally { if (request.current === sequence) setLoading(false); }
  }, []);
  useEffect(() => {
    void refresh();
    return () => { request.current++; };
  }, [refresh]);
  return <details className="scene-queue-capacity">
    <summary>队列容量</summary>
    {loading && <p className="scene-help" role="status">正在查询队列容量…</p>}
    {error && <p className="scene-help" role="alert">{error}</p>}
    {capacity && <>
      <p className="scene-help">我的任务预留：{capacity.owner.reserved_units} / {capacity.owner.limit_units} 单位
        （{capacity.owner.active_items} 个进行中任务）</p>
      <p className="scene-help">系统剩余：{capacity.global.available_units} / {capacity.global.limit_units} 单位</p>
      <p className="scene-help">每页生成或 AI 修改先预留 {capacity.max_generated_assets_per_page + 1} 单位，
        包含最多 {capacity.max_generated_assets_per_page} 个后续图片任务；普通任务占 1 单位。
        这是查询时的状态，不保证下一次提交成功。</p>
      {(capacity.owner.available_units === 0 || capacity.global.available_units === 0) &&
        <p className="scene-help" role="status">队列已满，请等待任务结束或取消不需要的任务。取消在途模型请求不保证停止计费。</p>}
    </>}
    <button disabled={loading} onClick={() => void refresh()}>刷新队列容量</button>
  </details>;
}
