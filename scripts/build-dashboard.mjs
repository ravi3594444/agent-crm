import { cp, mkdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const source = join(root, 'plus-agent/app/dashboard_ui');
await mkdir(join(root, 'dist'), { recursive: true });
await cp(source, join(root, 'dist'), { recursive: true });
const html = await readFile(join(root, 'dist/index.html'), 'utf8');
for (const asset of ['app.js', 'styles.css', 'favicon.svg']) {
  if (!html.includes(asset)) throw new Error(`Missing entrypoint reference: ${asset}`);
  await readFile(join(root, 'dist', asset));
}
console.log('Dashboard built: dist/index.html');
