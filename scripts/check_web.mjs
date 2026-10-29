/**
 * 使用真实打包产物验收 WebUI，HTTP 只返回内存中的合成宿主投影。
 * 不启动 Engine、不读取配置或凭据；开关和人工评价专测接受内存模拟控制，其他写入和外网请求失败。
 * 先运行 frontend 的 build，再执行本文件；截图写入 ignored .validation/webui。
 * 浏览器与服务由本脚本拥有，失败也会关闭；使用本仓库锁定的 Playwright 与系统 Edge。
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';
import { host, paused } from '../frontend/tests/fixtures.ts';

const output = fileURLToPath(new URL('../.validation/webui/', import.meta.url));
await mkdir(output, { recursive: true });
const now = Math.floor(Date.now() / 1000);
const call = (id, path, state) => ({
  call_id: id, path, state, receipt_revision: 1, started_at: now - 120,
  finished_at: state === 'started' ? null : now - 100, error_type: null,
  error: null, result: null, media_count: 0,
});
const example = {
  ...host(),
  devices: [
    { instance_id: 'qq', kind: 'qq', revision: '1', state: { state: 'online', mode: 'forward' }, controls: [{ key: 'reconnect', title: '重新连接 QQ', fields: [] }], has_preview: false },
    { instance_id: 'voice', kind: 'voice_input', revision: '1', state: { state: 'stopped', mode: 'asr', device: 0, devices_loaded: true, devices: [{ index: 0, name: '测试麦克风' }], available_modes: ['asr'], asr_ready: true, audio_ready: false, vad_interrupt_enabled: true, latest_input: { processing_stage: 'failed' } }, controls: [{ key: 'start', title: '开始收音', fields: ['device', 'mode'] }, { key: 'set_vad_interrupt', title: 'VAD 自动停播', fields: ['enabled'] }], has_preview: false },
    { instance_id: 'computer', kind: 'computer', revision: '1', state: { connected: true, observing: false, foreground: '示例工作区' }, controls: [], has_preview: false },
  ],
  activities: [{ ...paused, goal: '整理今天的阅读笔记', remaining_seconds: 1500, progress_preview: { text: '已整理三篇文章的要点，等待继续。', truncated: false, original_chars: 21 } }],
  recent_actions: [
    { action_id: 'recent', state: 'completed', created_at: now - 30, finished_at: now - 20, error: null, calls: [call('read', 'me.computer.status', 'confirmed')] },
    { action_id: 'uncertain', state: 'failed', created_at: now - 120, finished_at: now - 100, error: null, calls: [call('saved', 'me.notebook.write', 'confirmed'), call('sent', 'me.qq.send', 'unknown'), call('next', 'me.audio.say', 'not_started')] },
  ],
};
let snapshot = structuredClone(example);
let disconnected = false;
// 首次快照由测试显式放行，可靠覆盖慢连接下不应闪出故障横幅的阶段。
const firstSnapshot = Promise.withResolvers();
const writes = [];
let proactiveControls = false;
let reviewControls = false;
const reviewRequests = [];
const reviewMaterial = {
  target: { kind: 'action', id: 'uncertain', state: 'failed' }, actions: [],
  revision: 'review-v1', turn_id: 'example-turn',
  stimuli: [
    { input_id: 'stimulus-one', adapter_id: 'test', display_name: '审查样本', text: '我正在忙，稍后再聊。', occurred_at: '2026-10-28T10:00:00+08:00', received_at: '2026-10-28T10:00:01+08:00', media: [] },
    { input_id: 'stimulus-two', adapter_id: 'test', display_name: '审查样本', text: '请先不要发消息。', occurred_at: '2026-10-28T10:00:02+08:00', received_at: '2026-10-28T10:00:03+08:00', media: [] },
  ],
  action: { action_id: 'uncertain', state: 'failed', source: 'await me.qq.send(text="再聊聊吧")', output: '', result_json: null, error: null,
    calls: [{ call_id: 'sent', path: 'me.qq.send', state: 'unknown', arguments_json: '{"text":"再聊聊吧"}', result_json: null, error: '平台尚未确认' }] },
  feedback: [],
};
const proactiveRequests = [];
const vadRequests = [];
const proactiveResults = new Map();
const files = new Map([
  ['/', ['index.html', 'text/html']], ['/assets/panel.js', ['panel.js', 'text/javascript']],
  ['/assets/panel.css', ['panel.css', 'text/css']], ['/assets/purrcept.css', ['purrcept.css', 'text/css']],
]);
// 白名单静态文件在监听前读入，避免请求路径参与文件路径计算。
const assets = new Map(await Promise.all([...files].map(async ([route, [name, type]]) => [route, {
  body: await readFile(new URL(`../src/purrcept_engine/app/static/${name}`, import.meta.url)), type,
}])));
const server = createServer(async (req, res) => {
  if (req.method === 'GET' && (req.url.startsWith('/api/reviews?') || req.url === '/api/reviews')) {
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ items: example.recent_actions.map((action) => ({ ...action, target_kind: "action", target_id: action.action_id, label: `${action.calls.length} 项操作` })), next_cursor: null }));
    return;
  }
  if (req.method === 'GET' && req.url === '/api/reviews/uncertain') {
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(reviewMaterial));
    return;
  }
  if (req.method === 'GET' && proactiveResults.has(req.url)) {
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(proactiveResults.get(req.url)));
    return;
  }
  if (reviewControls && req.method === 'POST' && req.url === '/api/controls') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const command = JSON.parse(body);
    assert.equal(command.target, 'action:uncertain');
    assert.equal(command.operation, 'review');
    assert.equal(command.expected_revision, reviewMaterial.revision);
    assert.ok(['good', 'bad'].includes(command.arguments.rating));
    reviewRequests.push(command);
    reviewMaterial.feedback.unshift({ ...command.arguments, request_id: command.request_id, evidence_id: `human-feedback:${command.request_id}`, revision: command.expected_revision, created_at: now });
    const result = { request_id: command.request_id, instance_id: snapshot.instance_id,
      request: { target: command.target, operation: command.operation, expected_revision: command.expected_revision, arguments: command.arguments },
      state: 'finished', admitted_at: now, finished_at: now, action_id: null, command_id: null, error_code: null, error: null };
    proactiveResults.set(`/api/controls/${command.request_id}`, result);
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result));
    return;
  }
  if (proactiveControls && req.method === 'POST' && req.url === '/api/controls') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const command = JSON.parse(body);
    assert.equal(typeof command.arguments.enabled, 'boolean');
    if (command.target === 'subject') {
      assert.equal(command.operation, 'set_proactive_thinking');
      assert.equal(command.expected_revision, snapshot.proactive_thinking.revision);
      proactiveRequests.push(command);
      snapshot.proactive_thinking = { ...snapshot.proactive_thinking, enabled: command.arguments.enabled,
        revision: String(Number(snapshot.proactive_thinking.revision) + 1) };
    } else {
      assert.equal(command.target, 'voice');
      assert.equal(command.operation, 'set_vad_interrupt');
      const voice = snapshot.devices.find((device) => device.instance_id === 'voice');
      assert.equal(command.expected_revision, voice.revision);
      vadRequests.push(command);
      voice.state.vad_interrupt_enabled = command.arguments.enabled;
      voice.revision = String(Number(voice.revision) + 1);
    }
    const result = {
      request_id: command.request_id, instance_id: snapshot.instance_id,
      request: { target: command.target, operation: command.operation, expected_revision: command.expected_revision, arguments: command.arguments },
      state: 'finished', admitted_at: now, action_id: null, command_id: null, finished_at: now,
      error_code: null, error: null, action: null, activity_command: null,
    };
    proactiveResults.set(`/api/controls/${command.request_id}`, result);
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result));
    return;
  }
  if (req.method !== 'GET') { writes.push(req.url); res.writeHead(405).end(); return; }
  if (req.url === '/api/session' || req.url === '/api/snapshot') {
    if (req.url === '/api/snapshot') await firstSnapshot.promise;
    if (disconnected) { res.writeHead(503, { 'Content-Type': 'application/json' }).end('{}'); return; }
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(req.url === '/api/session' ? { token: 'synthetic-only', instance_id: snapshot.instance_id } : snapshot));
    return;
  }
  const asset = assets.get(req.url);
  if (!asset) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'Content-Type': asset.type }); res.end(asset.body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
let page;
/** hash 生效即应只剩一份可读内容；不再靠等待离场动画掩盖中间帧的问题。 */
async function openPage(name) {
  await page.goto(`${base}/#/${name}`);
  await page.locator('.topbar-age').filter({ hasText: '更新于' }).waitFor();
  await page.waitForFunction((route) => document.querySelector('.nav-item[aria-current="page"]')?.getAttribute('href') === `#/${route}`, name);
  await page.waitForFunction(() => document.querySelectorAll('main .page').length === 1 && Number(getComputedStyle(document.querySelector('main .page')).opacity) === 1);
}
try {
  browser = await chromium.launch({ channel: 'msedge' });
  page = await browser.newPage({ viewport: { width: 1440, height: 960 }, reducedMotion: 'no-preference' });
  const failures = [];
  page.on('pageerror', (error) => failures.push(error.message));
  await page.route('**/*', (route) => {
    if (!route.request().url().startsWith(base + '/')) { failures.push(`意外外部请求：${route.request().url()}`); return route.abort(); }
    return route.continue();
  });
  await page.goto(base);
  await page.getByRole('heading', { name: '正在连接本机服务' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '立即重试' }).count(), 0);
  assert.equal(await page.locator('.pc-feedback').count(), 0);
  firstSnapshot.resolve();
  await page.getByRole('heading', { name: '有 1 件事需要你看一下' }).waitFor();

  // 在真实动效设置下逐帧观察快速来回切页：旧页面不能并存，新页面面板以 pc-enter 到达，
  // 任何一帧都不能出现空白（面板透明度不低于 0.4），页面容器本身不位移。
  const transitionFrames = await page.evaluate(async () => {
    const routes = ['devices', 'home', 'timeline', 'system', 'activities', 'attention', 'home'];
    // 选中项的终值取自 Cel 组件变量，逐帧比较可发现颜色过渡造成的中间帧。
    const probe = document.createElement('span');
    probe.style.background = 'var(--pc-nav-item-current-background)';
    probe.style.color = 'var(--pc-nav-item-current-color)';
    document.body.append(probe);
    const expected = { background: getComputedStyle(probe).backgroundColor, color: getComputedStyle(probe).color };
    probe.remove();
    const samples = [];
    for (const route of routes) {
      document.querySelector(`.nav-item[href="#/${route}"]`).click();
      for (let frame = 0; frame < 4; frame++) {
        await new Promise(requestAnimationFrame);
        const pages = document.querySelectorAll('main .page');
        const style = pages[0] ? getComputedStyle(pages[0]) : null;
        const selectedItem = document.querySelector('.nav-item[aria-current="page"]');
        const selected = getComputedStyle(selectedItem);
        const indicator = selectedItem.querySelector('.nav-indicator');
        const panels = pages[0] ? [...pages[0].children].map((child) => Number(getComputedStyle(child).opacity)) : [];
        // 选中项文字色与滑动指示块的底色必须同帧到达终值，不能分两次切换。
        samples.push({ count: pages.length, opacity: style?.opacity, transform: style?.transform,
          visible: panels.length > 0 && Math.min(...panels) >= 0.4, entering: panels.some((value) => value < 1),
          navigationReady: selected.color === expected.color && indicator !== null && getComputedStyle(indicator).backgroundColor === expected.background });
      }
    }
    return samples;
  });
  for (const { entering, ...frame } of transitionFrames) assert.deepEqual(frame, { count: 1, opacity: '1', transform: 'none', visible: true, navigationReady: true });
  assert.ok(transitionFrames.some((frame) => frame.entering), '同级切换应当播放到达过渡');
  // 到达结束后面板不能残留位移或半透明。
  await page.waitForTimeout(1000);
  assert.deepEqual(await page.evaluate(() => [...document.querySelector('main .page').children]
    .filter((child) => getComputedStyle(child).opacity !== '1' || getComputedStyle(child).transform !== 'none').length), 0);

  // 七页在桌面、中等宽度与手机上均不能产生横向溢出。
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 960 });
    for (const route of ['home', 'attention', 'timeline', 'reviews', 'devices', 'activities', 'system']) {
      await openPage(route);
      await page.evaluate(() => document.fonts.ready);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false, `${route} 在 ${width}px 横向溢出`);
      if (width === 1440 || width === 390) await page.screenshot({ path: `${output}/${route}-${width}.png`, fullPage: true });
    }
  }

  await page.setViewportSize({ width: 1440, height: 960 });
  await openPage('timeline');
  await page.getByRole('button', { name: /使用电脑/ }).waitFor();
  await page.getByRole('searchbox', { name: '搜索记录' }).fill('电脑');
  assert.equal(await page.locator('.data-row').count(), 1);
  await page.getByRole('searchbox', { name: '搜索记录' }).fill('无匹配内容');
  await page.getByText('没有符合条件的记录。', { exact: false }).waitFor();
  await page.getByRole('button', { name: '清除筛选' }).click();
  assert.equal(await page.locator('.data-row').count(), 2);
  await page.getByRole('radio', { name: '全部', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('radio', { name: '她的行动' }).isChecked(), true);
  await page.getByRole('radio', { name: '需要核对' }).check();
  await page.locator('.data-row').click();
  await page.getByRole('heading', { name: '已经发生 · 1' }).waitFor();
  await page.getByRole('heading', { name: '结果未知 · 1' }).waitFor();
  await page.getByRole('heading', { name: '没有发生 · 1' }).waitFor();
  await page.screenshot({ path: `${output}/inspector-1440.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('dialog').waitFor();
  await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
  await page.waitForFunction(() => Math.abs(new DOMMatrix(getComputedStyle(document.querySelector('[role="dialog"]')).transform).m41) < 0.1);
  await page.screenshot({ path: `${output}/sheet-390.png`, fullPage: true });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => location.hash === '#/timeline');

  // 同一页面断线后保留旧事实，任何原本可用的控制必须关闭。
  await openPage('devices');
  await page.getByText('最近一句处理失败，不能视为已经回应', { exact: true }).waitFor();
  await page.getByText('收音设备：测试麦克风', { exact: true }).waitFor();
  assert.equal(await page.getByRole('option', { name: '直接听原声', exact: true }).count(), 0);
  await page.getByRole('button', { name: '重新连接 QQ', exact: true }).waitFor();
  disconnected = true;
  await page.getByRole('banner').getByText('与本机服务的连接中断', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '重新连接 QQ', exact: true }).isDisabled(), true);
  await page.screenshot({ path: `${output}/offline-390.png`, fullPage: true });
  disconnected = false;

  // 当前行动比近期完成记录更早时，首页仍必须展示当前行动路径。
  snapshot = structuredClone(example);
  snapshot.recent_actions[1].state = 'running';
  snapshot.recent_actions[1].calls = [call('running', 'me.qq.send', 'started')];
  await page.setViewportSize({ width: 1440, height: 960 });
  await openPage('home');
  await page.getByRole('heading', { name: '正在使用QQ' }).waitFor();
  await page.getByRole('button', { name: '查看这次行动' }).click();
  await page.waitForFunction(() => location.hash === '#/timeline/uncertain');

  snapshot = { ...host(), activities: [] };
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openPage('home');
  await page.getByRole('heading', { name: '安静待命中' }).waitFor();
  await page.screenshot({ path: `${output}/idle-1440.png`, fullPage: true });
  // 移除吉祥物和放大正文不能丢失主体状态或导致水平溢出。
  await page.addStyleTag({ content: '.home-mascot { visibility: hidden; } body { font-size: 200%; }' });
  assert.equal(await page.getByRole('heading', { name: '安静待命中' }).isVisible(), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(writes, [], '查看页面不能提交业务控制');
  // 真实 React/Cel、HTTP 序列化和快照刷新共用生产路径，控制端仅在内存改变合成事实。
  proactiveControls = true;
  await openPage('system');
  const proactive = page.getByRole('switch', { name: '主动思考' });
  await proactive.waitFor();
  assert.equal(await proactive.isChecked(), false);
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-disabled') !== 'true');
  await proactive.click();
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-checked') === 'true');
  assert.equal(proactiveRequests.length, 1);
  await page.screenshot({ path: `${output}/proactive-on-1440.png`, fullPage: true });
  await proactive.focus();
  await page.keyboard.press('Space');
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-checked') === 'false');
  assert.equal(proactiveRequests.length, 2);
  assert.deepEqual(proactiveRequests.map((item) => item.arguments.enabled), [true, false]);
  assert.notEqual(proactiveRequests[0].request_id, proactiveRequests[1].request_id);
  disconnected = true;
  await page.getByRole('button', { name: '立即重试', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-disabled') === 'true');
  await proactive.click({ force: true });
  assert.equal(proactiveRequests.length, 2, '断线不能修改主动思考');
  disconnected = false;
  snapshot = structuredClone(example);
  await openPage('devices');
  const vad = page.getByRole('switch', { name: 'VAD 自动停播' });
  await vad.waitFor();
  assert.equal(await vad.isChecked(), true);
  await vad.click();
  await page.waitForFunction(() => document.querySelector('[aria-label="VAD 自动停播"]')?.getAttribute('aria-checked') === 'false');
  await vad.focus();
  await page.keyboard.press('Space');
  await page.waitForFunction(() => document.querySelector('[aria-label="VAD 自动停播"]')?.getAttribute('aria-checked') === 'true');
  assert.deepEqual(vadRequests.map((item) => item.arguments.enabled), [false, true]);
  await page.screenshot({ path: `${output}/vad-switch-1440.png`, fullPage: true });
  disconnected = true;
  await page.getByRole('button', { name: '立即重试', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="VAD 自动停播"]')?.getAttribute('aria-disabled') === 'true');
  await vad.click({ force: true });
  assert.equal(vadRequests.length, 2, '断线不能修改 VAD 自动停播');

  // 人工审查复用真实打包表单与控制客户端，写入仅进入本脚本的内存替身。
  disconnected = false;
  reviewControls = true;
  await page.setViewportSize({ width: 1440, height: 960 });
  await openPage('reviews');
  await page.locator('.item-row').filter({ hasText: '3 项操作' }).click();
  await page.getByText('我正在忙，稍后再聊。', { exact: true }).waitFor();
  await page.getByText('请先不要发消息。', { exact: true }).waitFor();
  await page.getByRole('radio', { name: '坏', exact: true }).check();
  await page.getByRole('textbox', { name: '留言（可选）' }).fill('对方已经说明正在忙，等待她主动回应后再继续。');
  await page.getByRole('button', { name: '提交评价', exact: true }).click();
  await page.getByText('评价已保存，已加入主脑与反思的可靠待办。', { exact: true }).waitFor();
  assert.equal(reviewRequests.length, 1);
  assert.equal(reviewRequests[0].arguments.rating, 'bad');
  await page.screenshot({ path: `${output}/reviews-1440.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 960 });
  await page.getByRole('dialog').waitFor();
  await page.getByRole('radio', { name: '好', exact: true }).check();
  await page.getByRole('textbox', { name: '留言（可选）' }).fill('补充评价：有克制地等待时值得保持。');
  await page.getByRole('button', { name: '提交评价', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.review-detail section:last-of-type article').length === 2);
  await page.getByRole('button', { name: '提交评价', exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('.review-form button[type="submit"]')?.disabled);
  assert.equal(reviewRequests.length, 2);
  assert.notEqual(reviewRequests[0].request_id, reviewRequests[1].request_id);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: `${output}/reviews-390.png`, fullPage: true });
  disconnected = true;
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '立即重试', exact: true }).click();
  await page.locator('.item-row').filter({ hasText: '3 项操作' }).click();
  await page.getByRole('radio', { name: '坏', exact: true }).check();
  assert.equal(await page.getByRole('button', { name: '提交评价', exact: true }).isDisabled(), true);
  assert.equal(reviewRequests.length, 2);
  assert.deepEqual(failures, [], '浏览器不应报错或访问外网');
  console.log(`通过：28 个快速切换帧、35 个页面/视口组合、搜索、键盘过滤、部分失败详情、窄屏关闭、断线控制、主动思考与 VAD 开关、人工好坏评价与留言、当前行动、减少动效与空状态。截图：${output}`);
} catch (error) {
  if (page) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error(await page.locator('body').innerText());
  }
  throw error;
} finally {
  firstSnapshot.resolve();
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
