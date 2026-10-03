import type { Frame, SceneElement, SlideScene } from './sceneTypes';

type Canvas = SlideScene['canvas'];
type DragMode = 'move' | 'resize';

const round = (value: number) => Math.round(value * 100) / 100;

export function draggedFrame(start: Frame, dx: number, dy: number, mode: DragMode,
  kind: SceneElement['kind'], canvas: Canvas): Frame {
  if (mode === 'move') {
    const minX = kind === 'text' ? 0 : Math.max(-2880, -start.w + 1);
    const minY = kind === 'text' ? 0 : Math.max(-2880, -start.h + 1);
    const maxX = kind === 'text' ? Math.max(0, canvas.width_pt - start.w) : Math.min(2880, canvas.width_pt - 1);
    const maxY = kind === 'text' ? Math.max(0, canvas.height_pt - start.h) : Math.min(2880, canvas.height_pt - 1);
    return { ...start, x: round(Math.max(minX, Math.min(maxX, start.x + dx))),
      y: round(Math.max(minY, Math.min(maxY, start.y + dy))) };
  }
  if (kind === 'image') {
    const widthChange = dx / start.w, heightChange = dy / start.h;
    const dominant = Math.abs(widthChange) >= Math.abs(heightChange) ? widthChange : heightChange;
    const maximum = Math.min(2880 / start.w, 2880 / start.h);
    const practicalMinimum = Math.max(10 / start.w, 10 / start.h);
    const roundableMinimum = Math.max(0.01 / start.w, 0.01 / start.h);
    if (roundableMinimum > maximum) return start;
    const minimum = practicalMinimum <= maximum ? practicalMinimum : roundableMinimum;
    const ratio = Math.max(minimum, Math.min(maximum, 1 + dominant));
    return { ...start, w: round(start.w * ratio), h: round(start.h * ratio) };
  }
  return { ...start, w: round(Math.max(10, Math.min(canvas.width_pt - start.x, start.w + dx))),
    h: round(Math.max(10, Math.min(canvas.height_pt - start.y, start.h + dy))) };
}

export function proportionalFrame(start: Frame, dimension: 'w' | 'h', value: number): Frame | null {
  if (!Number.isFinite(value) || value <= 0) return null;
  const ratio = value / start[dimension];
  const w = round(start.w * ratio), h = round(start.h * ratio);
  return w > 0 && h > 0 && w <= 2880 && h <= 2880 ? { ...start, w, h } : null;
}
