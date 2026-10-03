import { describe, expect, it } from 'vitest';
import { draggedFrame, proportionalFrame } from './sceneGeometry';

const canvas = { width_pt: 960, height_pt: 540 };
const image = { x: 100, y: 80, w: 300, h: 150, rotation_deg: 30 };

describe('scene geometry', () => {
  it('keeps image aspect ratio when resizing by drag or numeric size', () => {
    expect(draggedFrame(image, 60, 0, 'resize', 'image', canvas)).toEqual({ ...image, w: 360, h: 180 });
    expect(proportionalFrame(image, 'h', 75)).toEqual({ ...image, w: 150, h: 75 });
    expect(proportionalFrame(image, 'w', 0)).toBeNull();
    expect(proportionalFrame(image, 'w', 6000)).toBeNull();
    const extreme = { ...image, w: 1, h: 2880 };
    expect(draggedFrame(extreme, 300, 0, 'resize', 'image', canvas)).toEqual(extreme);
  });

  it('keeps text inside canvas while allowing partially off-canvas images', () => {
    const text = { ...image, rotation_deg: 0 };
    expect(draggedFrame(text, -200, -200, 'move', 'text', canvas)).toMatchObject({ x: 0, y: 0 });
    expect(draggedFrame(text, 2000, 2000, 'move', 'text', canvas)).toMatchObject({ x: 660, y: 390 });
    expect(draggedFrame(image, -200, -200, 'move', 'image', canvas)).toMatchObject({ x: -100, y: -120 });
  });
});
