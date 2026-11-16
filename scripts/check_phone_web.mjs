/** 真实打包页面的 Phone 离线验收；后端 QR 来自实际 ASGI 路由，其余宿主事实在内存模拟。 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';
import { host } from '../frontend/tests/fixtures.ts';

const root = fileURLToPath(new URL('../', import.meta.url));
const qr = JSON.parse(execFileSync(`${root}.venv/Scripts/python.exe`, ['-c', `
import asyncio, json, time, httpx
from tests.test_phone_web import PhoneHost
from purrcept_engine.app.web import create_app
class Example(PhoneHost):
    async def phone_pairing(self, target, *, operation="read", expected_device_id=None):
        return {'status':'ready','v':1,'url':'https://192.0.2.10:8766','certificate_sha256':'a'*64,'invitation':'synthetic-offline-invitation-only','expires_at':time.time()+120}
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(Example(), port=8765, token='test'),client=('127.0.0.1',123)),base_url='http://127.0.0.1:8765') as client:
        print(json.dumps((await client.get('/api/phone/phone/pairing')).json()))
asyncio.run(run())
`], { cwd: root, encoding: 'utf8', env: { ...process.env, LITELLM_LOCAL_MODEL_COST_MAP: 'True' } }));
const snapshot = structuredClone(host());
snapshot.devices.push({ instance_id: 'phone', kind: 'phone', revision: '1', state: { online: false, control_connected: false }, controls: [], has_preview: false });
const writes = [];
let pairing = { status: 'closed' };
let disconnected = false;
const assets = new Map(await Promise.all([
  ['/', 'index.html', 'text/html'], ['/assets/panel.js', 'panel.js', 'text/javascript'],
  ['/assets/panel.css', 'panel.css', 'text/css'], ['/assets/purrcept.css', 'purrcept.css', 'text/css'],
].map(async ([route, file, type]) => [route, { body: await readFile(`${root}src/purrcept_engine/app/static/${file}`), type }])));
const server = createServer(async (request, response) => {
  function json(value, code = 200) { response.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }).end(JSON.stringify(value)); }
  if (disconnected && request.url.startsWith('/api/')) { json({}, 503); return; }
  if (request.method === 'POST') {
    assert.equal(request.headers['x-purrcept-token'], 'synthetic-only');
    let body = ''; for await (const chunk of request) body += chunk;
    const value = JSON.parse(body);
    writes.push({ url: request.url, value });
    if (request.url === '/api/phone/phone/pairing/replace') { assert.deepEqual(value, { device_id: 'old-device' }); pairing = { ...qr, expires_at: Date.now() / 1000 + 120 }; json(pairing); return; }
    if (request.url === '/api/phone/phone/pairing') { pairing = pairing.status === 'paired' ? pairing : { ...qr, expires_at: Date.now() / 1000 + 120 }; json(pairing); return; }
    assert.equal(request.url, '/api/phone/setup');
    if (value.action === 'preview') { assert.equal(value.address, '192.0.2.20'); json({ preview_id: 'review-one', url: 'https://192.0.2.20:8766', create_identity: false }); return; }
    assert.deepEqual(value, { action: 'save', preview_id: 'review-one' }); json({ saved: true, restart_required: true }); return;
  }
  if (request.url === '/api/session') { json({ instance_id: snapshot.instance_id, token: 'synthetic-only' }); return; }
  if (request.url === '/api/snapshot') { json(snapshot); return; }
  if (request.url === '/api/health') { json(snapshot.health); return; }
  if (request.url === '/api/phone/phone/pairing') { json(pairing); return; }
  if (request.url === '/api/phone/setup') { json({ available: true, config_name: 'purrcept.toml', address: '192.0.2.10', port: 8766, addresses: ['192.0.2.10'] }); return; }
  const asset = assets.get(request.url);
  if (!asset) { response.writeHead(404).end(); return; }
  response.writeHead(200, { 'Content-Type': asset.type }).end(asset.body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const output = `${root}.validation/phone-web`;
await mkdir(output, { recursive: true });
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/*', (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await page.goto(`${base}/#/devices`);
  await page.getByRole('button', { name: '显示配对二维码', exact: true }).waitFor();
  assert.equal(writes.length, 0, '页面读取不得自动开启配对');
  await page.getByRole('button', { name: '显示配对二维码', exact: true }).click();
  const image = page.getByRole('img', { name: '手机配对二维码' });
  await image.waitFor();
  assert.equal(await image.locator('path').getAttribute('d'), qr.qr_path);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('main .page')).opacity) === 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: `${output}/qr-${width}.png`, fullPage: true });
  }
  await Promise.all([page.waitForResponse((response) => response.url().endsWith('/pairing') && response.request().method() === 'POST'), page.getByRole('button', { name: '刷新配对二维码', exact: true }).click()]);
  assert.equal(writes.filter((entry) => entry.url.endsWith('/pairing')).length, 2);
  pairing = { status: 'expired' };
  await image.waitFor({ state: 'detached' });
  await page.getByRole('button', { name: '显示配对二维码', exact: true }).waitFor();
  pairing = { status: 'paired', name: '测试手机', device_id: 'old-device' };
  await page.getByText('电脑已保存配对：测试手机。').waitFor();
  const writesBeforeCancel = writes.length;
  await page.getByRole('button', { name: '重新配对', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '重新配对手机？' });
  await confirmation.waitFor();
  await page.keyboard.press('Escape');
  await confirmation.waitFor({ state: 'detached' });
  assert.equal(writes.length, writesBeforeCancel, '取消重新配对不得写入');
  await page.getByRole('button', { name: '重新配对', exact: true }).click();
  await confirmation.waitFor();
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('[role="dialog"]')).opacity) === 1);
  await page.screenshot({ path: `${output}/repair-confirm-320.png`, fullPage: true });
  await Promise.all([page.waitForResponse((response) => response.url().endsWith('/pairing/replace')), confirmation.getByRole('button', { name: '撤销旧配对并显示二维码' }).click()]);
  await confirmation.waitFor({ state: 'detached' });
  await image.waitFor();
  await page.getByText('旧配对已撤销，请用新安装的 App 扫描二维码。Engine 中的历史记录保留。').waitFor();
  await page.getByRole('button', { name: '设置手机连接', exact: true }).click();
  await page.getByLabel('手机可访问的电脑地址', { exact: true }).fill('192.0.2.20');
  await page.getByRole('button', { name: '查看将要保存的改动' }).click();
  await page.getByRole('button', { name: '保存手机设置', exact: true }).waitFor();
  await page.screenshot({ path: `${output}/settings-320.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.getByRole('button', { name: '保存手机设置', exact: true }).click();
  await page.getByText('手机设置已保存，原配置已备份。请正常关闭并重新打开 Engine，再回到这里显示配对二维码。').waitFor();
  pairing = { ...qr, expires_at: Date.now() / 1000 + 120 };
  await image.waitFor();
  disconnected = true;
  await image.waitFor({ state: 'detached' });
  await page.getByText('连接中断，配对二维码已收起。连接恢复后会重新读取。').waitFor();
  assert.deepEqual(errors, []);
  console.log('通过：实际后端 QR 路径、显式开启/刷新、过期撤下、已配对提示、重新配对确认/取消/新二维码、设置预览/保存、断线撤下，以及 1440/390/320px 页面。');
  console.log(`截图：${output}`);
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
