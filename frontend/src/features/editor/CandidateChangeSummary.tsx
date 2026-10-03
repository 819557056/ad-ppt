import type { CandidateObjectChange, CandidateSummary } from './sceneTypes';

const fields: Record<string, string> = { text: '文字', frame: '位置/尺寸', style: '文字样式',
  asset_id: '图片素材', crop: '裁剪', fit: '填充方式', opacity: '透明度', alt_text: '图片说明',
  locked: '锁定', role: '用途', kind: '类型' };

function ObjectChanges({ title, items }: { title: string; items: CandidateObjectChange[] }) {
  if (!items.length) return null;
  return <details>
    <summary>{title} {items.length} 个对象</summary>
    <ul>{items.map(item => <li key={item.element_id}>
      <span>{item.kind === 'text' ? '文字' : '图片'} · {item.label || item.role}</span>
      <code>{item.element_id}</code>
      {!!item.changed_fields?.length && <small>修改：{item.changed_fields.map(field => fields[field] || field).join('、')}</small>}
    </li>)}</ul>
  </details>;
}

export function CandidateChangeSummary({ summary }: { summary: CandidateSummary }) {
  const hasDetails = summary.schema_version === 1 && Array.isArray(summary.added) &&
    Array.isArray(summary.removed) && Array.isArray(summary.modified);
  return <div className="scene-candidate-changes">
    {summary.text_count !== undefined && summary.image_count !== undefined &&
      <small>候选包含 {summary.text_count} 个文字、{summary.image_count} 个图片对象</small>}
    {hasDetails ? <>
      <p>相对生成时的基版本：新增 {summary.added!.length} · 删除 {summary.removed!.length} · 修改 {summary.modified!.length}</p>
      <ObjectChanges title="新增" items={summary.added!} />
      <ObjectChanges title="删除" items={summary.removed!} />
      <ObjectChanges title="修改" items={summary.modified!} />
      {summary.background_changed && <p>背景已更换</p>}
      {summary.layer_order_changed && <p>已有对象的层级顺序已调整</p>}
      {!summary.added!.length && !summary.removed!.length && !summary.modified!.length &&
        !summary.background_changed && !summary.layer_order_changed && <p>页面内容无变化</p>}
    </> : <p>此历史候选未记录对象变更清单，请预览核对。</p>}
  </div>;
}
