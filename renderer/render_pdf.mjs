import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const [source, target] = process.argv.slice(2);
if (!source || !target) throw new Error('Usage: render_pdf.mjs scene.html output.pdf');

const browser = await chromium.launch({ headless: true,
  ...(process.env.SCENE_CHROMIUM_PATH ? { executablePath: process.env.SCENE_CHROMIUM_PATH } : {}) });
try {
  console.log(browser.version());
  const page = await browser.newPage({ serviceWorkers: 'block' });
  await page.route(/^https?:/, route => route.abort());
  await page.goto(pathToFileURL(source).href, { waitUntil: 'load', timeout: 30000 });
  await page.evaluate(async () => {
    await document.fonts.load('16px NotoScene', 'Hg汉');
    await document.fonts.ready;
    if (document.fonts.check('16px NotoScene') === false) throw new Error('Scene font unavailable');
    await Promise.all([...document.images].map(async image => {
      if (!image.complete || image.naturalWidth === 0) await image.decode();
      if (!image.naturalWidth) throw new Error('Scene image unavailable');
    }));
    for (const element of document.querySelectorAll('[data-scene-id]')) {
      if (element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1) {
        throw new Error(`Text overflow: ${element.getAttribute('data-scene-id')}`);
      }
    }
  });
  await page.pdf({ path: target, preferCSSPageSize: true, printBackground: true,
                   displayHeaderFooter: false, margin: { top: 0, right: 0, bottom: 0, left: 0 } });
} finally {
  await browser.close();
}
