import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SceneRenderer } from './SceneRenderer';
import type { SlideScene } from './sceneTypes';

describe('SceneRenderer image geometry', () => {
  it('uses scene points rather than CSS pixels for the background and cropped images', () => {
    const scene: SlideScene = {
      schema_version: 1, font_manifest_id: 'fonts-v1',
      canvas: { width_pt: 960, height_pt: 540 },
      background: { kind: 'image', asset_id: 'background', crop: { x: 0, y: 0, w: 1, h: 1 } },
      elements: [{ id: 'image', kind: 'image', role: 'illustration', asset_id: 'image',
        frame: { x: 100, y: 80, w: 300, h: 150, rotation_deg: 0 },
        crop: { x: 0.1, y: 0, w: 0.9, h: 1 }, fit: 'contain', opacity: 0.5,
        locked: false, alt_text: '' }],
    };
    const { container } = render(<SceneRenderer scene={scene} assets={{ background: '/background.png', image: '/image.png' }} />);
    const [background, image] = [...container.querySelectorAll('img')];
    for (const node of [background, image]) {
      Object.defineProperties(node, { naturalWidth: { value: 200 }, naturalHeight: { value: 100 } });
      fireEvent.load(node);
    }
    expect(background.parentElement?.style.width).toBe('960pt');
    expect(background.parentElement?.style.height).toBe('540pt');
    expect(image.parentElement?.style.width).toMatch(/pt$/);
    expect(image.style.left).toMatch(/pt$/);
    expect(image.style.width).toMatch(/pt$/);
  });

  it('clears selection on blank slide but not on an object', () => {
    const clear = vi.fn();
    const scene: SlideScene = { schema_version: 1, font_manifest_id: 'fonts-v1',
      canvas: { width_pt: 960, height_pt: 540 }, background: { kind: 'solid', color: '#FFFFFF' },
      elements: [{ id: 'text', kind: 'text', role: 'body', locked: false, text: 'Hello',
        frame: { x: 10, y: 10, w: 100, h: 20, rotation_deg: 0 },
        style: { font_family_id: 'noto-sans-sc', font_size_pt: 12, font_weight: 400,
          color: '#000000', align: 'left', vertical_align: 'top', line_height: 1.2, padding_pt: 0 } }] };
    const { container } = render(<SceneRenderer scene={scene} assets={{}} onClearSelection={clear} />);
    fireEvent.pointerDown(container.querySelector('[data-element-id]')!);
    expect(clear).not.toHaveBeenCalled();
    fireEvent.pointerDown(container.querySelector('.scene-slide')!);
    expect(clear).toHaveBeenCalledOnce();
  });

  it('retains empty hard paragraphs without splitting Unicode text', () => {
    const scene: SlideScene = { schema_version: 1, font_manifest_id: 'fonts-v1',
      canvas: { width_pt: 960, height_pt: 540 }, background: { kind: 'solid', color: '#FFFFFF' },
      elements: [{ id: 'text', kind: 'text', role: 'body', locked: false, text: 'Á🄀\n\n尾行\n',
        frame: { x: 10, y: 10, w: 200, h: 200, rotation_deg: 0 },
        style: { font_family_id: 'noto-sans-sc', font_size_pt: 12, font_weight: 400,
          color: '#000000', align: 'left', vertical_align: 'middle', line_height: 1.2, padding_pt: 4 } }] };
    const { container } = render(<SceneRenderer scene={scene} assets={{}} />);
    const paragraphs = container.querySelector('[data-element-id] > div')!.children;
    expect([...paragraphs].map(node => node.textContent)).toEqual(['Á🄀', '', '尾行', '']);
    expect(paragraphs[1].querySelector('br')).not.toBeNull();
    expect(paragraphs[3].querySelector('br')).not.toBeNull();
    expect(scene.elements[0].kind === 'text' && scene.elements[0].text).toBe('Á🄀\n\n尾行\n');
  });
});
