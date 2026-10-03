import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const [source, target] = process.argv.slice(2);
if (!source || (source !== '--identity' && !target)) {
  throw new Error('Usage: render_text_layout.mjs scene.html output.json | --identity');
}
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const browser = await chromium.launch({ headless: true,
  ...(process.env.SCENE_CHROMIUM_PATH ? { executablePath: process.env.SCENE_CHROMIUM_PATH } : {}) });
try {
  const identity = {
    chromium: browser.version(), node: process.versions.node, icu: process.versions.icu,
    playwright: require('playwright/package.json').version,
    script_sha256: hash((await readFile(new URL(import.meta.url), 'utf8')).replace(/\r\n/g, '\n')),
    font_sha256: hash(await readFile(new URL('../backend/fonts/NotoSansSC-Regular.ttf', import.meta.url))),
  };
  if (source === '--identity') {
    process.stdout.write(JSON.stringify(identity));
  } else {
    const page = await browser.newPage({ serviceWorkers: 'block', deviceScaleFactor: 1 });
    await page.route(/^https?:/, route => route.abort());
    await page.goto(pathToFileURL(source).href, { waitUntil: 'load', timeout: 30000 });
    const pages = await page.evaluate(async () => {
      await document.fonts.load('16px NotoScene', 'Hg汉');
      await document.fonts.ready;
      if (!document.fonts.check('16px NotoScene')) throw new Error('Scene font unavailable');
      await Promise.all([...document.images].map(async image => {
        if (!image.complete || image.naturalWidth === 0) await image.decode();
        if (!image.naturalWidth) throw new Error('Scene image unavailable');
      }));
      const segmenter = new Intl.Segmenter('und', { granularity: 'grapheme' });
      const pt = px => Math.round(px * .75 * 10000) / 10000;
      const pages = [];
      let nodeIndex = 0;
      for (const section of document.querySelectorAll('section.slide')) {
        const measured = {};
        const origin = section.getBoundingClientRect();
        for (const element of section.querySelectorAll('[data-scene-id]')) {
          const id = element.getAttribute('data-scene-id');
          element.setAttribute('data-layout-node', String(nodeIndex++));
          const style = getComputedStyle(element);
          let lineHeight = parseFloat(style.lineHeight);
          const paragraphs = [...element.querySelectorAll('[data-scene-paragraph]')];
          if (!id || !Number.isFinite(lineHeight) || lineHeight <= 0 || !paragraphs.length) {
            throw new Error('Scene text element has unexpected DOM');
          }
          const source = paragraphs.map(p => p.textContent).join('\n');
          const bounds = element.getBoundingClientRect();
          const padding = parseFloat(style.paddingTop);
          const content = element.querySelector('[data-scene-content]');
          const contentBounds = content.getBoundingClientRect();
          if (element.scrollHeight > element.clientHeight + 1 ||
              element.scrollWidth > element.clientWidth + 1 ||
              contentBounds.height > bounds.height - 2 * padding + .5 ||
              content.scrollWidth > content.clientWidth + 1) throw new Error('Text overflow');

          // A separate single-line probe measures the baseline. Range rectangles
          // are glyph rectangles, NOT line boxes. Never insert markers in source.
          const probe = document.createElement('div');
          Object.assign(probe.style, { position: 'absolute', left: '0', top: '0',
            visibility: 'hidden', width: '10000px', fontFamily: style.fontFamily,
            fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight,
            whiteSpace: 'pre', padding: '0', margin: '0', border: '0' });
          const sample = document.createTextNode('Hg汉');
          const marker = document.createElement('span');
          marker.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
          probe.append(sample, marker);
          document.body.append(probe);
          const probeRange = document.createRange();
          probeRange.selectNodeContents(sample);
          const glyphTop = probeRange.getBoundingClientRect().top - probe.getBoundingClientRect().top;
          const baseline = marker.getBoundingClientRect().top - probe.getBoundingClientRect().top;
          lineHeight = probe.getBoundingClientRect().height;
          probe.remove();

          const lines = [];
          let cp = 0;
          for (const [paragraphIndex, paragraph] of paragraphs.entries()) {
            const node = paragraph.firstChild?.nodeName === 'BR' && paragraph.childNodes.length === 1
              ? null : paragraph.firstChild;
            if ((node && (node.nodeType !== Node.TEXT_NODE || paragraph.childNodes.length !== 1)) ||
                (!node && paragraph.textContent !== '')) throw new Error('Unexpected paragraph DOM');
            const rect = paragraph.getBoundingClientRect();
            const start = cp;
            let row = 0, lineStart = cp;
            const addLine = (end, kind) => lines.push({ start: lineStart, end,
              break_kind: kind, font_size_pt: pt(parseFloat(style.fontSize)),
              line_box: { x: pt(rect.left - origin.left), y: pt(rect.top - origin.top + row * lineHeight),
                w: pt(rect.width), h: pt(lineHeight) },
              baseline_pt: pt(rect.top - origin.top + row * lineHeight + baseline) });
            const range = document.createRange();
            for (const part of segmenter.segment(paragraph.textContent)) {
              range.setStart(node, part.index); // DOM indices are UTF-16 units.
              range.setEnd(node, part.index + part.segment.length);
              const glyph = range.getClientRects()[0];
              if (!glyph) throw new Error('Unmeasurable grapheme');
              const nextRow = Math.round((glyph.top - rect.top - glyphTop) / lineHeight);
              if (nextRow !== row) {
                if (nextRow !== row + 1 || cp === lineStart) throw new Error('Noncontiguous text layout');
                addLine(cp, 'soft');
                row = nextRow;
                lineStart = cp;
              }
              cp += [...part.segment].length;
            }
            const kind = paragraphIndex < paragraphs.length - 1 ? 'hard' : 'end';
            addLine(cp, kind);
            if (Math.abs(rect.height - (row + 1) * lineHeight) > .5 || cp < start) {
              throw new Error('Text line box mismatch');
            }
            if (kind === 'hard') cp++;
          }
          measured[id] = { source_length: [...source].length, font_size_pt: pt(parseFloat(style.fontSize)),
            line_height_pt: pt(lineHeight), padding_pt: pt(padding), lines, resolved_fonts: [] };
        }
        pages.push(measured);
      }
      return pages;
    });
    // Ask the browser which physical fonts shaped the original nodes, rather
    // than reporting the requested CSS alias as if it were a resolved family.
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    const { root } = await cdp.send('DOM.getDocument', { depth: -1 });
    let nodeIndex = 0;
    for (const measured of pages) {
      for (const element of Object.values(measured)) {
        const { nodeIds } = await cdp.send('DOM.querySelectorAll', {
          nodeId: root.nodeId, selector: `[data-layout-node="${nodeIndex++}"] [data-scene-paragraph]` });
        const resolved = new Map();
        for (const nodeId of nodeIds) {
          const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId });
          for (const font of fonts) {
            const key = JSON.stringify([font.familyName, font.postScriptName, font.isCustomFont]);
            const previous = resolved.get(key);
            resolved.set(key, { family_name: font.familyName, postscript_name: font.postScriptName,
              is_custom_font: font.isCustomFont, glyph_count: font.glyphCount + (previous?.glyph_count || 0) });
          }
        }
        element.resolved_fonts = [...resolved.values()];
      }
    }
    await writeFile(target, JSON.stringify({ schema_version: 1,
      engine: `chromium-${browser.version()}`, engine_identity: identity, pages }));
  }
} finally {
  await browser.close();
}
