import { useState, type CSSProperties, type PointerEvent } from 'react';
import type { ImageElement, SceneElement, SlideScene } from './sceneTypes';
import { assetUrl } from './sceneApi';

type Props = { scene: SlideScene; assets: Record<string, string>; selected?: string[];
  onSelect?: (id: string, additive: boolean) => void; onEditText?: (id: string) => void;
  onClearSelection?: () => void;
  onPointerStart?: (event: PointerEvent<HTMLDivElement>, id: string) => void };

function SceneImage({ element, url }: { element: ImageElement; url?: string }) {
  const [size, setSize] = useState({ w: 1, h: 1 });
  if (!url) return <div className="scene-missing-image">图片不可用</div>;
  const { frame, crop } = element;
  const cw = crop.w * size.w, ch = crop.h * size.h;
  const scale = (element.fit === 'contain' ? Math.min : Math.max)(frame.w / cw, frame.h / ch);
  const vw = Math.min(frame.w, cw * scale), vh = Math.min(frame.h, ch * scale);
  return <div style={{ position: 'absolute', left: `${(frame.w - vw) / 2}pt`, top: `${(frame.h - vh) / 2}pt`,
    width: `${vw}pt`, height: `${vh}pt`, overflow: 'hidden', opacity: element.opacity }}>
    <img draggable={false} alt={element.alt_text} src={assetUrl(url)} onLoad={event => setSize({ w: event.currentTarget.naturalWidth, h: event.currentTarget.naturalHeight })}
      style={{ position: 'absolute', width: `${size.w * scale}pt`, height: `${size.h * scale}pt`,
        left: `${-crop.x * size.w * scale - (cw * scale - vw) / 2}pt`,
        top: `${-crop.y * size.h * scale - (ch * scale - vh) / 2}pt`,
        maxWidth: 'none', pointerEvents: 'none' }} />
  </div>;
}

export function SceneRenderer({ scene, assets, selected = [], onSelect, onEditText, onPointerStart, onClearSelection }: Props) {
  const background = scene.background;
  const bgElement: ImageElement | null = background.kind === 'image' ? {
    id: 'background', kind: 'image', role: 'decoration', asset_id: background.asset_id,
    frame: { x: 0, y: 0, w: scene.canvas.width_pt, h: scene.canvas.height_pt, rotation_deg: 0 },
    crop: background.crop, fit: 'cover', opacity: 1, locked: true, alt_text: '' } : null;
  return <div className="scene-slide" style={{ width: `${scene.canvas.width_pt}pt`, height: `${scene.canvas.height_pt}pt`,
    background: background.kind === 'solid' ? background.color : '#fff' }}
    onPointerDown={event => { if (!(event.target as HTMLElement).closest('[data-element-id]')) onClearSelection?.(); }}>
    {bgElement && <SceneImage element={bgElement} url={assets[bgElement.asset_id]} />}
    {scene.elements.map((element: SceneElement) => {
      const { frame } = element;
      const common: CSSProperties = { position: 'absolute', left: `${frame.x}pt`, top: `${frame.y}pt`,
        width: `${frame.w}pt`, height: `${frame.h}pt`, transform: `rotate(${frame.rotation_deg}deg)`,
        boxSizing: 'border-box', outline: selected.includes(element.id) ? '2px solid #2B76D2' : undefined,
        cursor: onSelect ? (element.locked ? 'not-allowed' : 'move') : 'default' };
      if (element.kind === 'image') return <div key={element.id} data-element-id={element.id} style={common}
        onPointerDown={event => { onSelect?.(element.id, event.shiftKey); onPointerStart?.(event, element.id); }}><SceneImage element={element} url={assets[element.asset_id]} /></div>;
      const style = element.style;
      return <div key={element.id} data-element-id={element.id} style={{ ...common, display: 'flex', flexDirection: 'column',
        justifyContent: { top: 'flex-start', middle: 'center', bottom: 'flex-end' }[style.vertical_align],
        padding: `${style.padding_pt}pt`, fontFamily: 'NotoScene, sans-serif', fontSize: `${style.font_size_pt}pt`,
        fontWeight: style.font_weight, color: style.color, textAlign: style.align,
        lineHeight: style.line_height, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'hidden' }}
        onPointerDown={event => { onSelect?.(element.id, event.shiftKey); onPointerStart?.(event, element.id); }}
        onDoubleClick={() => onEditText?.(element.id)}>
        <div style={{ flex: 'none', width: '100%' }}>{element.text.split('\n').map((line, index) =>
          <div key={index}>{line || <br />}</div>)}</div>
      </div>;
    })}
  </div>;
}
