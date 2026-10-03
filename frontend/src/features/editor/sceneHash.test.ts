import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import fixture from '../../../../docs/fixtures/slide_scene_hash_v1.json';
import { canonicalScene } from './sceneHash';

describe('SlideScene canonical hash', () => {
  it('matches the Python golden fixture with Chinese, newline and six-decimal crop', () => {
    const actual = createHash('sha256').update(canonicalScene(fixture.scene), 'utf8').digest('hex');
    expect(actual).toBe(fixture.sha256);
  });
});
