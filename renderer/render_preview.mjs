import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const [source, target] = process.argv.slice(2);
if (!source || !target) throw new Error('Usage: render_preview.mjs scene.html output-dir');

const browser = await chromium.launch({ headless: true,
  ...(process.env.SCENE_CHROMIUM_PATH ? { executablePath: process.env.SCENE_CHROMIUM_PATH } : {}) });
try {
  console.log(browser.version());
  // CSS pt is 4/3 px. A 1.125 device scale produces 1.5 output pixels per pt,
  // matching the PDF and LibreOffice raster checks.
  const page = await browser.newPage({ viewport: { width: 1920, height: 1200 },
    deviceScaleFactor: 1.125, serviceWorkers: 'block' });
  await page.route(/^https?:/, route => route.abort());
  await page.goto(pathToFileURL(source).href, { waitUntil: 'load', timeout: 30000 });
  await page.evaluate(async () => {
    await document.fonts.load('16px NotoScene', 'Hg汉');
    await document.fonts.ready;
    if (!document.fonts.check('16px NotoScene')) throw new Error('Scene font unavailable');
    await Promise.all([...document.images].map(async image => {
      if (!image.complete || !image.naturalWidth) await image.decode();
      if (!image.naturalWidth) throw new Error('Scene image unavailable');
    }));
    for (const element of document.querySelectorAll('[data-scene-id]')) {
      if (element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1)
        throw new Error(`Text overflow: ${element.getAttribute('data-scene-id')}`);
    }
  });
  const slides = page.locator('.slide');
  const count = await slides.count();
  for (let index = 0; index < count; index++) {
    await slides.nth(index).screenshot({ path: join(target, `page-${String(index + 1).padStart(3, '0')}.png`) });
  }
} finally {
  await browser.close();
}
