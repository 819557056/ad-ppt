// Build-time verification of the actually installed headless runtime; no downloads.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');
const registry = JSON.parse(readFileSync(join(dirname(require.resolve('playwright-core/package.json')), 'browsers.json'), 'utf8'));
const browser = await chromium.launch({ headless: true });
try {
  console.log(JSON.stringify({
    playwright: require('playwright/package.json').version,
    chromium_revision: registry.browsers.find(item => item.name === 'chromium').revision,
    chromium_version: browser.version(),
  }));
} finally {
  await browser.close();
}
