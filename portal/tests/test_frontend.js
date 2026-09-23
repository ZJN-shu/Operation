/*
 * 前端行为回归测试（零依赖，只用 node 内置模块）。
 * 把 static/app.js 装进假 DOM 里跑，断言那些「点一下才知道」的行为。
 *
 * 运行：node portal/tests/test_frontend.js
 *       node portal/tests/test_frontend.js --old   # 还原修复前的写法，看它怎么打爆
 *
 * 覆盖：
 *   ① 会话过期（服务重启后本地 token 失效）不再刷出无限 logout 请求
 *   ② 刷新后停在原来那一屏（后台留在后台、tab 留在原 tab）
 *   ③ 非管理角色不会被恢复进后台
 *   ④ 退出登录会清掉界面位置，不留给下一个人
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const OLD = process.argv.includes('--old');
const APP = path.join(__dirname, '..', 'static', 'app.js');
const CAP = 5000;          // 请求数上限：死循环会远远超过它

let pass = 0, fail = 0;
function check(cond, label, extra) {
  if (cond) { pass++; console.log(`  [PASS] ${label}${extra ? ' -> ' + extra : ''}`); }
  else { fail++; console.log(`  [FAIL] ${label}${extra ? ' -> ' + extra : ''}`); }
}

// ---------- 还原修复前的写法（复现死循环）----------
function patchedSource() {
  let src = fs.readFileSync(APP, 'utf8');
  if (!OLD) return src;
  // 循环要「api() 无条件回调 logout」+「logout() 用旧顺序」两半合起来才成立，所以两处一起还原
  const aStart = src.indexOf('    if (res.status === 401) {');
  const aEnd = src.indexOf('\n    const data = await res.json()');
  if (aStart < 0 || aEnd < 0) { console.error('定位 api 401 分支失败'); process.exit(2); }
  src = src.slice(0, aStart) +
    "    if (res.status === 401) { logout(false); throw new Error('登录已过期'); }" +
    src.slice(aEnd);

  const lStart = src.indexOf('async function logout(redirect) {');
  const lEnd = src.indexOf('\n  function enterApp(');
  if (lStart < 0 || lEnd < 0) { console.error('定位 logout 失败'); process.exit(2); }
  src = src.slice(0, lStart) + `async function logout(redirect) {
    await flushTrack();
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {}
    trackQueue = [];
    saveTrackQueue();
    token = ''; user = null; sessionId = '';
    localStorage.removeItem('ops_token');
    localStorage.removeItem('ops_session');
    if (redirect !== false) showLogin();
  }
` + src.slice(lEnd);
  if (!/await api\('\/api\/auth\/logout'/.test(src)) {
    console.error('替换失败：没能还原旧版 logout'); process.exit(2);
  }
  return src;
}

// ---------- 假 DOM ----------
const ADMIN_USER = {
  emp_id: '1001', username: 'admin', name: '管理员',
  role: 'super_admin', department: 'IT', points: 1205,
};
const PLAIN_USER = {
  emp_id: '1002', username: 'user01', name: '普通员工',
  role: 'user', department: 'IT', points: 300,
};

const EMPTY_DASHBOARD = {
  overview: { points_issued: 0, points_spent: 0, active_gifts: 0, pending_orders: 0 },
  daily_points: [], funnel: {}, rates: {}, shipping: null,
  search_keywords: [], course_accuracy: [], top_course_views: [],
  top_gift_views: [], top_gifts: [], recent_orders: [],
};

// 积分明细现在是「数据看板 → 积分 tab」，不再是一个独立菜单页。
// 想直接停在这一屏，就得同时给出分区和 tab 两个键。
const ON_POINTS_TAB = { ops_token: 'tok', ops_view: 'admin', ops_section: 'dashboard', ops_dash_tab: 'points' };

function makeEnv({ storage = {}, me = null, authMode = 'ok', stream = null, request = null, storageFail = false } = {}) {
  // 元素按选择器记忆：classList 的增删才能被断言观察到；
  // 监听器也记下来，测试里可以真的「点」一下（click()）。
  const els = new Map();
  function el(sel) {
    if (!els.has(sel)) {
      const cls = new Set();
      const handlers = {};
      els.set(sel, {
        classList: {
          add: (c) => cls.add(c), remove: (c) => cls.delete(c),
          toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
          contains: (c) => cls.has(c),
        },
        _cls: cls,
        _handlers: handlers,
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        removeEventListener() {},
        async click() { for (const fn of handlers['click'] || []) await fn({}); },
        dataset: {}, style: {}, value: '', textContent: '', innerHTML: '',
        scrollIntoView() {}, focus() {}, remove() {}, appendChild() {},
        querySelector: () => el(sel + ' *'),
      });
    }
    return els.get(sel);
  }

  const calls = { total: 0, byPath: {}, requests: [] };

  // 实时流的桩。chunks 是「服务端逐块发出的字节」，可以故意把一帧切成两半、
  // 也可以把汉字切在多字节中间 —— 真实 TCP 分片不看帧边界，这正是要测的地方。
  //   status     给 401/403 就能测「这条路径不该重连」
  //   keepAlive  true = 发完这些块后连接不关闭，用来断言「收到帧不重拉列表」
  //   calls      每次发起的记录（含 signal），用来断言主动断开时 aborted 了
  const streamCfg = Object.assign({ status: 200, chunks: [], keepAlive: true }, stream || {});
  const streamCalls = [];
  function makeReader(chunks, keepAlive) {
    let i = 0;
    return {
      read() {
        if (i < chunks.length) return Promise.resolve({ value: chunks[i++], done: false });
        if (keepAlive) return new Promise(() => {});      // 永不 resolve：连接一直开着
        return Promise.resolve({ value: undefined, done: true });
      },
    };
  }
  const store = Object.assign({}, storage);
  const localStorage = {
    get length() { return Object.keys(store).length; },
    key: (i) => Object.keys(store)[i] ?? null,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      if (storageFail && k.startsWith('ops_intent:')) throw new Error('存储不可用');
      store[k] = String(v);
    },
    removeItem: (k) => { delete store[k]; },
  };

  const sandbox = {
    console, JSON, Math, Date, String, Number, Array, Object, Error, Promise,
    RegExp, Set, Map, isNaN, parseInt, parseFloat, encodeURIComponent,
    decodeURIComponent, setTimeout, clearTimeout, setInterval, clearInterval,
    // vm 上下文不继承 node 的全局：少一个就是 ReferenceError。
    // 读实时流要用 TextDecoder（解字节）和 AbortController（主动断开）。
    TextDecoder, AbortController, Uint8Array,
    localStorage,
    crypto: { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2) },
    fetch: async (url, opts) => {
      calls.total++;
      const p = String(url).split('?')[0];
      calls.byPath[p] = (calls.byPath[p] || 0) + 1;
      calls.requests.push({ path: p, method: opts?.method, body: opts?.body ? JSON.parse(opts.body) : null });
      // 超过上限直接收尾并退出。不能指望下面的 setTimeout 来判定：
      // 死循环会把事件循环饿死，定时器永远轮不上（浏览器里就是页面卡住点不动）。
      if (calls.total > CAP) {
        console.log(`     ！请求数超过上限 ${CAP}，判定为死循环，提前收尾`);
        console.log(`     按路径 = ${JSON.stringify(calls.byPath)}`);
        console.log('  [FAIL] 请求数有界（不是无限套娃）');
        process.exit(1);
      }

      const ok200 = (body) => ({ ok: true, status: 200, json: async () => body });
      const unauthorized = {
        ok: false, status: 401, json: async () => ({ detail: '未登录或会话已过期' }),
      };

      // 服务重启后的样子：会话存在服务进程内存里，重启即清空，
      // 于是任何「带 Authorization 头的请求」都回 401 —— 连登出请求也一样。
      // 注意判的是「头存在」而不是「头非空」：token 被清空后发的是空头，
      // 真实服务端对空 token 照样 401（判非空会把复现放行掉，循环就复现不出来了）。
      if (authMode === 'expired' && opts && opts.headers && 'Authorization' in opts.headers) {
        return unauthorized;
      }
      if (request) {
        const custom = await request(p, opts);
        if (custom !== undefined) return custom;
      }
      if (p === '/api/auth/me') {
        return me ? ok200({ user: me }) : unauthorized;
      }
      // 积分实时流：唯一一个不是 JSON 的响应，必须单独桩。
      // 兜底那个 ok200 没有 .body，app 一读 getReader 就 TypeError。
      if (p === '/api/admin/points/stream') {
        streamCalls.push({ opts: opts || {}, signal: (opts || {}).signal });
        if (streamCfg.status !== 200) {
          return {
            ok: false, status: streamCfg.status, body: null,
            json: async () => ({ detail: '无权限' }),
          };
        }
        return {
          ok: true, status: 200,
          body: { getReader: () => makeReader(streamCfg.chunks, streamCfg.keepAlive) },
          json: async () => ({}),
        };
      }
      if (p === '/api/admin/points/ref-types') {
        return ok200({ ref_types: [
          { value: 'welcome', label: '注册奖励' }, { value: 'course', label: '完成课程' },
          { value: 'grant', label: '人工发放' }, { value: 'revert', label: '回滚' },
        ] });
      }
      if (p === '/api/admin/points') return ok200({ items: [], total: 0, page: 1, size: 10 });
      // 趋势单独一条接口：实时刷新只动这一块，不重算整个看板。
      // 给一个和 EMPTY_DASHBOARD 不同的数字，断言「图确实被换过了」。
      if (p === '/api/admin/points/trend') {
        return ok200({ daily_points: [{ d: '2026-09-22', issued: 175, spent: 0 }] });
      }
      if (p === '/api/admin/dashboard') return ok200(EMPTY_DASHBOARD);
      if (p === '/api/user/notifications/unread') return ok200({ count: 0 });
      if (p === '/api/user/home') return ok200({ carousel: [], announcements: [], hot_courses: [] });
      if (p === '/api/user/courses') return ok200({ courses: [] });
      if (p === '/api/user/gifts') return ok200({ gifts: [] });
      if (p === '/api/user/orders') return ok200({ orders: [] });
      if (p === '/api/user/points') return ok200({ points: 0, records: [] });
      if (p === '/api/user/activities') return ok200({ activities: [], announcements: [] });
      if (p === '/api/analytics/events') {
        return ok200({ accepted: 0, duplicated: 0, rejected: 0 });
      }
      // 列表接口一律给空分页。桩不能对没列的接口随手回 401 ——
      // 那会让 api() 判定「会话失效」把整个会话清掉，测试测的就不是它想测的东西了。
      return ok200({ items: [], total: 0, page: 1, size: 10 });
    },
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.document = {
    querySelector: el, querySelectorAll: () => [],
    getElementById: el, addEventListener() {},
    visibilityState: 'visible', createElement: () => el('*tmp'),
  };

  vm.createContext(sandbox);
  vm.runInContext(patchedSource(), sandbox);
  // win 暴露沙箱本身：window.App 上的纯函数（parseSSE）要直接调
  return { els: els, store, calls, get: el, win: sandbox, streams: streamCalls };
}

const tick = (ms = 60) => new Promise((r) => setTimeout(r, ms));
// 注意：classList.contains 对「从没被 toggle 过」的元素恒为 false，
// 所以光断言 !hidden(x) 是空的。这里一律以 #admin-toggle 的文案作为正向标志：
// setView('admin') 写「返回用户端」，setView('user') 写「进入后台」。
const hidden = (env, sel) => env.get(sel).classList.contains('hidden');
const viewOf = (env) => {
  const t = env.get('#admin-toggle').textContent;
  return t.indexOf('返回用户端') >= 0 ? 'admin' : (t.indexOf('进入后台') >= 0 ? 'user' : '?');
};

// ---------- 用例 ----------
(async () => {
  console.log(`\n① 会话过期：本地 token 失效后请求数是否有界（${OLD ? '修复前' : '修复后'}）`);
  {
    // authMode='expired'：任何带 token 的请求都 401，正是服务重启后的样子
    const env = makeEnv({
      storage: { ops_token: 'stale-token-from-previous-server-run' },
      authMode: 'expired',
    });
    await tick(2000);
    const logoutCalls = env.calls.byPath['/api/auth/logout'] || 0;
    console.log(`     请求总数 = ${env.calls.total}，其中 logout = ${logoutCalls}`);
    console.log(`     按路径 = ${JSON.stringify(env.calls.byPath)}`);
    check(env.calls.total <= 20, '请求数有界（不是无限套娃）', `总共 ${env.calls.total}`);
    check(env.store.ops_token === undefined, '本地 token 已清除');
    check(hidden(env, '#app'), '退回登录页（#app 被隐藏）');
  }

  if (OLD) {   // 修复前的写法只剩复现价值，后面的用例都被它带崩
    console.log(`\n修复前：${pass} 通过 / ${fail} 失败`);
    process.exit(fail ? 1 : 0);
  }

  console.log('\n② 刷新后停在原来那一屏（管理员·后台 · 课程管理）');
  {
    const env = makeEnv({
      storage: { ops_token: 'tok', ops_view: 'admin', ops_section: 'courses' },
      me: ADMIN_USER,
    });
    await tick(150);
    check(viewOf(env) === 'admin', '停在后台视图', `view=${viewOf(env)}`);
    check(hidden(env, '#user-nav'), '用户端导航已隐藏');
    check(env.store.ops_section === 'courses', '后台分区还在 courses', env.store.ops_section);
    check(env.calls.byPath['/api/admin/courses'] === 1, '确实去加载了课程管理那一页');
  }

  console.log('\n③ 同一份 localStorage，换成普通用户登录');
  {
    const env = makeEnv({
      storage: { ops_token: 'tok', ops_view: 'admin', ops_section: 'audit' },
      me: PLAIN_USER,
    });
    await tick(150);
    check(viewOf(env) === 'user', '不会被恢复进后台', `view=${viewOf(env)}`);
    check(!hidden(env, '#user-nav'), '落在用户端');
    check(env.calls.byPath['/api/admin/audit-logs'] === undefined, '没去请求审计日志');
  }

  console.log('\n④ 用户端 tab 也记住');
  {
    const env = makeEnv({
      storage: { ops_token: 'tok', ops_view: 'user', ops_tab: 'gifts' },
      me: PLAIN_USER,
    });
    await tick(150);
    check(env.store.ops_tab === 'gifts', 'tab 还在 gifts', env.store.ops_tab);
    check(env.calls.byPath['/api/user/gifts'] === 1, '确实去加载了礼品那一页');
    check(env.get('#tab-gifts').innerHTML !== '', '礼品面板被渲染过');
  }

  console.log('\n⑤ 退出登录清掉界面位置，不留给下一个人');
  {
    const env = makeEnv({
      storage: { ops_token: 'tok', ops_view: 'admin', ops_section: 'courses' },
      me: ADMIN_USER,
    });
    await tick(150);
    check(env.store.ops_view === 'admin', '点之前：ops_view=admin', env.store.ops_view);
    await env.get('#logout-btn').click();
    await tick(150);
    check(env.store.ops_view === undefined, '退出后 ops_view 已清');
    check(env.store.ops_section === undefined, '退出后 ops_section 已清');
    check(env.store.ops_tab === undefined, '退出后 ops_tab 已清');
    check(env.store.ops_token === undefined, '退出后 token 已清');
    check(!hidden(env, '#login-screen'), '回到登录页');
    // 下一个人登录（token 换新的，store 里已经没有任何界面位置）→ 落在用户端首页。
    // 注意不能直接拿 env.store 再建一个 env 就断言：那会儿 token 也是空的，
    // bootstrap 会停在登录页，setView 根本没跑过，断言等于没断言。
    const next = makeEnv({
      storage: Object.assign({}, env.store, { ops_token: 'tok2' }),
      me: PLAIN_USER,
    });
    await tick(150);
    check(viewOf(next) === 'user', '下一个人落在用户端', `view=${viewOf(next)}`);
    check(next.store.ops_tab === 'home', '并回到首页 tab', next.store.ops_tab);
  }

  console.log('\n⑥ 实时帧解析器（纯函数，直接喂缓冲）');
  {
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: ADMIN_USER });
    const parse = env.win.App.parseSSE;
    const one = parse('data: {"id":1,"type":"point"}\n\ndata: {"id":2,"type":"point"}\n\n');
    check(one.frames.length === 2 && one.frames[1].data.id === 2, '一块里两帧都切出来了');
    check(one.rest === '', '没有残留');
    const half = parse('data: {"id":3,"ty');
    check(half.frames.length === 0 && half.rest === 'data: {"id":3,"ty', '半帧不吐，留成 rest');
    const joined = parse(half.rest + 'pe":"point"}\n\n');
    check(joined.frames.length === 1 && joined.frames[0].data.id === 3, '拼上后半块才成帧');
    const hb = parse(': ping\n\n: connected\n\ndata: {"id":4,"type":"point"}\n\n');
    check(hb.frames.length === 1 && hb.frames[0].data.id === 4, '心跳/注释被忽略，不算空帧');
    const bye = parse('event: bye\ndata: {}\n\n');
    check(bye.frames.length === 1 && bye.frames[0].event === 'bye', 'event: 行被识别');
    const bad = parse('data: {不是json\n\ndata: {"id":5,"type":"point"}\n\n');
    check(bad.frames.length === 2 && bad.frames[0].data === null && bad.frames[1].data.id === 5,
      '坏帧给 null，后面的帧照常解析');
    const crlf = parse('data: {"id":6,"type":"point"}\r\n\r\n');
    check(crlf.frames.length === 1 && crlf.frames[0].data.id === 6, 'CRLF 换行也能切');
  }

  console.log('\n⑦ 进积分页：自动开流；收到帧就地插入，不重拉列表');
  {
    const mk = (o) => 'data: ' + JSON.stringify(o) + '\n\n';
    const sse = ': connected\n\n' +
      mk({ type: 'point', id: 901, emp_id: '1002', user_name: '普通员工', points: 50,
           note: '人工发放：跨块汉字测试', ref_type: 'grant', ref_id: 7,
           created_at: '2026-09-22 10:00:00', operator_emp_id: '1001',
           operator_name: '管理员', revertible: false }) +
      mk({ type: 'point', id: 902, emp_id: '1002', user_name: '普通员工', points: -30,
           note: '课程积分纠偏测试', ref_type: 'course', ref_id: 3,
           created_at: '2026-09-22 10:01:00', operator_emp_id: '', operator_name: '',
           revertible: true });
    // 故意切在一个汉字的中间：TCP 分片不看字符边界，TextDecoder 少了 {stream:true}
    // 就会把这两个字各解成一个替换字符（页面上出现 �）。
    const bytes = Buffer.from(sse, 'utf8');
    const cjk = bytes.findIndex((b) => b >= 0xe0);
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER,
      stream: { chunks: [bytes.subarray(0, cjk + 1), bytes.subarray(cjk + 1)], keepAlive: true },
    });
    await tick(250);
    check(env.calls.byPath['/api/admin/points/stream'] === 1, '进了积分页就建了流');
    check(env.calls.byPath['/api/admin/points'] === 1, '首屏数据只拉一次');
    const body = env.get('#pt-body').innerHTML;
    check(body.indexOf('人工发放：跨块汉字测试') >= 0, '跨块汉字没有解成乱码');
    check(body.indexOf('+50') >= 0, '增加显示 + 号');
    check(body.indexOf('-30') >= 0, '扣减带负号');
    check(body.indexOf('1001 管理员') >= 0, '操作人显示工号 + 姓名');
    check(body.indexOf('↩ 回滚') >= 0 && body.indexOf('不可回滚') >= 0,
      '回滚按钮按服务端给的 revertible 渲染（人工流水不给按钮）');
    check(env.calls.byPath['/api/admin/points'] === 1, '收到帧没有重拉列表');
    check(env.get('#pt-live').textContent.indexOf('本次推送 2 条') >= 0, '实时条数提示',
      env.get('#pt-live').textContent);

    // 趋势图也要跟着动，但不能「来一帧刷一次」—— 它是按天聚合的，连帧只有当天那一格变。
    check(!env.calls.byPath['/api/admin/points/trend'],
      '刚收到帧时还没刷趋势（走防抖，不是每帧一问）');
    await tick(1400);
    check(env.calls.byPath['/api/admin/points/trend'] === 1,
      '防抖到点后趋势刷了一次', String(env.calls.byPath['/api/admin/points/trend']));
    check(env.calls.byPath['/api/admin/points'] === 1, '刷趋势没有顺带重拉整个明细');
    check(env.get('#pt-trend').innerHTML.indexOf('发放 175') >= 0,
      '趋势图真的换成了新数据');
    check(/📈 近 7 天积分趋势/.test(env.get('#sec-dashboard').innerHTML),
      '趋势卡片和明细在同一屏上');
  }

  console.log('\n⑫ 连帧风暴：趋势是按天聚合的，不许一帧一问');
  {
    const mk = (o) => 'data: ' + JSON.stringify(o) + '\n\n';
    // 一个 chunk 里塞 6 帧（一次活动结算就会这样）
    let sse = '';
    for (let i = 0; i < 6; i++) {
      sse += mk({ type: 'point', id: 950 + i, emp_id: '1002', user_name: '普通员工',
        points: 10, note: '完成课程', ref_type: 'course', ref_id: i + 1,
        created_at: '2026-09-22 11:00:0' + i, operator_emp_id: '', operator_name: '',
        revertible: true });
    }
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER,
      stream: { chunks: [Buffer.from(sse, 'utf8')], keepAlive: true },
    });
    await tick(2000);
    const n = env.calls.byPath['/api/admin/points/trend'] || 0;
    console.log(`     6 帧 -> 趋势请求 ${n} 次`);
    check(n === 1, '6 帧只触发 1 次趋势刷新（防抖生效）', `${n} 次`);
    check(env.calls.byPath['/api/admin/points'] === 1, '明细仍然没被重拉');
    check(env.get('#pt-body').innerHTML.split('<tr').length - 1 === 6, '6 行都插进表里了');
  }

  console.log('\n⑧ 切走 / 登出：流要被主动断开（不能只靠服务端心跳发现）');
  {
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER, stream: { chunks: [], keepAlive: true },
    });
    await tick(150);
    check(env.streams.length === 1, '先确认流开着');
    await env.get('#admin-toggle').click();       // 返回用户端
    await tick(60);
    check(env.streams[0].signal && env.streams[0].signal.aborted === true,
      '切回用户端后请求被 abort');
  }
  {
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER, stream: { chunks: [], keepAlive: true },
    });
    await tick(150);
    await env.get('#logout-btn').click();
    await tick(60);
    check(env.streams[0].signal.aborted === true, '登出后请求被 abort');
  }

  console.log('\n⑨ 流返回 401：只发一次，不重连、不刷 logout');
  {
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER, stream: { status: 401 },
    });
    await tick(2500);                              // 远超首次退避的 1s
    console.log(`     按路径 = ${JSON.stringify(env.calls.byPath)}`);
    check(env.calls.byPath['/api/admin/points/stream'] === 1, '流只请求了一次');
    check((env.calls.byPath['/api/auth/logout'] || 0) <= 1, '登出请求不超过 1 次');
    check(env.calls.total <= 20, '请求总数有界', `总共 ${env.calls.total}`);
  }

  console.log('\n⑩ 流反复结束：指数退避让请求数有界（不是每 0.4s 一次的轮询）');
  {
    const env = makeEnv({
      storage: ON_POINTS_TAB,
      me: ADMIN_USER, stream: { chunks: [], keepAlive: false },
    });
    await tick(4000);
    const n = env.calls.byPath['/api/admin/points/stream'] || 0;
    console.log(`     4 秒内重连 ${n} 次`);
    check(n >= 2, '确实在重连（通道没死）', `${n} 次`);
    check(n <= 5, '退避生效，没有打满上限', `${n} 次`);
    check(env.calls.total < CAP, '没有撞到死循环上限');
    const snapshots = env.calls.byPath['/api/admin/points'] || 0;
    console.log(`     验收数据：流请求 ${n} 次，明细快照请求 ${snapshots} 次（含首屏）`);
    check(snapshots >= 2, '断线重连后补拉积分快照，补偿断线期间漏帧', `实际 ${snapshots} 次，至少应为 2 次`);
  }

  console.log('\n补充验收：收到 dropped 后补拉快照');
  {
    let snapshots = 0;
    const env = makeEnv({ storage: ON_POINTS_TAB, me: ADMIN_USER,
      stream: { chunks: [Buffer.from('data: {"type":"dropped"}\n\n')], keepAlive: true },
      request: (p) => {
        if (p !== '/api/admin/points') return undefined;
        snapshots++;
        const items = snapshots === 1 ? [] : [{ id: 1801, emp_id: '1002', user_name: '补偿用户',
          points: 30, note: '丢帧补偿记录', ref_type: 'course', ref_label: '完成课程',
          operator_label: '本人', created_at: '2026-09-22 12:00:00', revertible: true }];
        return { ok: true, status: 200, json: async () => ({ items, total: items.length, page: 1, size: 10 }) };
      } });
    await tick(300);
    check(snapshots === 2, 'dropped 触发一次补偿快照（加首屏共 2 次）', `实际 ${snapshots} 次`);
    check(env.get('#pt-body').innerHTML.includes('丢帧补偿记录'), '补偿快照中的遗漏记录显示在明细');
  }

  console.log('\n⑪ 积分明细在看板内部：tab 切换要同时管住数据流和实时流的生死');
  {
    // 默认停在总览：这时**不该**有任何积分流量的开销（明细/流一条都不发），
    // 否则只是打开看板就会给服务端挂一个订阅队列。
    const env = makeEnv({ storage: { ops_token: 'tok', ops_view: 'admin' }, me: ADMIN_USER });
    await tick(150);
    const overviewHtml = env.get('#sec-dashboard').innerHTML;
    check(/class="dash-tabs"/.test(overviewHtml), '看板顶部渲染了 tab 条');
    check(/App\.setDashTab\('points'\)/.test(overviewHtml), 'tab 条里有「积分」入口');
    check(!env.calls.byPath['/api/admin/points'], '停在总览时不拉积分明细');
    check(!env.calls.byPath['/api/admin/points/stream'], '停在总览时不建实时流');

    // 切到积分 tab：明细 + 趋势图都要有，流也要建起来
    await env.win.App.setDashTab('points');
    await tick(150);
    const ptHtml = env.get('#sec-dashboard').innerHTML;
    check(env.calls.byPath['/api/admin/points'] === 1, '切到积分 tab 才拉明细');
    check(env.calls.byPath['/api/admin/dashboard'] >= 2, '积分 tab 复用看板载荷画趋势图');
    check(/pt-body/.test(ptHtml) && /近 7 天积分趋势/.test(ptHtml), '明细表和趋势图在同一个 tab 里');
    check(/热门礼品/.test(ptHtml), '礼品（积分花掉去哪）也在积分 tab 下');
    check(env.calls.byPath['/api/admin/points/stream'] === 1, '切到积分 tab 才建流');

    // 切回总览：流必须断，否则服务端白挂一个订阅队列
    await env.win.App.setDashTab('overview');
    await tick(80);
    check(env.streams[0].signal.aborted === true, '切回总览就断流');

    // 离开看板（去订单页）同样要断
    await env.win.App.setDashTab('points');
    await tick(120);
    const n2 = env.streams.length;
    env.win.App.setAdminSection('orders');
    await tick(80);
    check(env.streams[n2 - 1].signal.aborted === true, '离开看板去别的分区也断流');
  }

  const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
  const redeemPath = '/api/user/gifts/7/redeem';
  const intent = 'ops_intent:' + PLAIN_USER.emp_id + ':' + redeemPath;
  const successful = { ok: true, redemption_id: 31, points_cost: 60, gift_name: '礼品' };
  const posts = (env, p) => env.calls.requests.filter((r) => r.path === p && r.method === 'POST');

  console.log('\n⑬ 兑换双击、丢响应、刷新及新意图');
  {
    let resolve;
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: PLAIN_USER,
      request: (p) => p === redeemPath ? new Promise((r) => { resolve = r; }) : undefined });
    await tick();
    const first = env.win.App.redeemGift(7);
    const second = env.win.App.redeemGift(7);
    check(posts(env, redeemPath).length === 1, '并发双击只发一条兑换请求');
    check(!!env.store[intent], '请求发送前已持久化请求键');
    resolve(response(successful));
    await Promise.all([first, second]);
    check(!env.store[intent], '确认成功后清除待确认意图');
    const firstKey = posts(env, redeemPath)[0].body.request_id;
    const next = env.win.App.redeemGift(7);
    resolve(response({ ...successful, redemption_id: 32 }));
    await next;
    check(posts(env, redeemPath)[1].body.request_id !== firstKey, '确认完成后的新兑换使用新请求键');
  }
  {
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: PLAIN_USER,
      request: (p) => { if (p === redeemPath) throw new Error('响应丢失'); } });
    await tick();
    check(await env.win.App.redeemGift(7) === false, '网络失败不会显示兑换成功');
    const key = posts(env, redeemPath)[0].body.request_id;
    check(env.get('#tab-gifts').innerHTML.includes('App.confirmRedeem(7)'), '礼品不在列表时仍能确认未完成请求');
    await env.win.App.redeemGift(7);
    check(posts(env, redeemPath)[1].body.request_id === key, '网络失败重试复用原键');
    const refreshed = makeEnv({ storage: env.store, me: PLAIN_USER,
      request: (p) => p === redeemPath ? response(successful) : undefined });
    await tick();
    await refreshed.win.App.redeemGift(7);
    check(posts(refreshed, redeemPath)[0].body.request_id === key, '刷新页面后仍按原键确认结果');
    check(!refreshed.store[intent], '刷新后的成功确认清除原意图');
    await refreshed.win.App.confirmRedeem(7);
    check(posts(refreshed, redeemPath).length === 1, '已确认后的旧确认按钮不会创建新兑换');
    const other = makeEnv({ storage: env.store, me: ADMIN_USER,
      request: (p) => p === redeemPath ? response(successful) : undefined });
    await tick();
    await other.win.App.redeemGift(7);
    check(posts(other, redeemPath)[0].body.request_id !== key, '不同用户不继承他人的未确认请求键');
  }

  console.log('\n⑭ 异常状态与持久化失败');
  for (const mode of ['500', 'invalid', 'partial', 'business', '422']) {
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: PLAIN_USER,
      request: (p) => p !== redeemPath ? undefined : mode === '500' ? response({}, 500)
        : mode === 'invalid' ? response({}) : mode === 'partial' ? response({ ok: true })
        : mode === '422' ? response({ detail: [{ msg: '参数不合法' }] }, 422)
        : response({ ok: false, reason: 'out_of_stock' }) });
    await tick();
    check(await env.win.App.redeemGift(7) === false, mode + ' 不展示成功');
    const keep = mode === '500' || mode === 'invalid' || mode === 'partial';
    check(!!env.store[intent] === keep, mode + (keep ? ' 保留待确认键' : ' 明确拒绝后清除待确认键'));
    if (keep) {
      const key = posts(env, redeemPath)[0].body.request_id;
      await env.win.App.redeemGift(7);
      check(posts(env, redeemPath)[1].body.request_id === key, mode + ' 重试键不变');
    }
  }
  {
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: PLAIN_USER, storageFail: true });
    await tick();
    await env.win.App.redeemGift(7);
    check(posts(env, redeemPath).length === 0, '请求键无法持久化时不发兑换请求');
  }

  console.log('\n⑮ 礼品信息编辑与库存调整隔离');
  {
    const stockPath = '/api/admin/gifts/7/stock';
    let failStock = true;
    const env = makeEnv({ storage: { ops_token: 'tok' }, me: ADMIN_USER,
      request: (p, opts) => p === stockPath ? response(failStock ? {} : { ok: true, stock: 9, record_id: 2 }, failStock ? 500 : 200)
        : opts?.method === 'PUT' || (p === '/api/admin/gifts' && opts?.method === 'POST') ? response({ ok: true }) : undefined });
    await tick();
    env.win.App.openGiftForm(7);
    check(!env.get('#modal-box').innerHTML.includes('id="gf-stock"'), '编辑已有礼品没有可写库存字段');
    env.get('#gf-name').value = '礼品'; env.get('#gf-cost').value = '60'; env.get('#gf-stock').value = '999';
    await env.win.App.saveGift(7);
    const update = env.calls.requests.find((r) => r.path === '/api/admin/gifts/7' && r.method === 'PUT');
    check(update && !('stock' in update.body), '即使存在旧库存输入，礼品信息保存也不发送 stock');
    await env.win.App.saveGift(0);
    check(posts(env, '/api/admin/gifts')[0].body.stock === 999, '新建礼品允许指定初始库存');
    env.get('#stock-delta').value = '0'; env.get('#stock-reason').value = '补货';
    await env.win.App.saveStock(7);
    check(posts(env, stockPath).length === 0, '零库存增量不提交');
    env.get('#stock-delta').value = '5';
    await env.win.App.saveStock(7);
    const key = posts(env, stockPath)[0].body.request_id;
    env.get('#stock-delta').value = '6';
    await env.win.App.saveStock(7);
    check(posts(env, stockPath).length === 1, '库存结果未确认时禁止修改参数冒充新操作');
    env.win.App.openStockForm(7);
    check(env.get('#modal-box').innerHTML.includes('value="5"'), '重新打开库存表单回填未确认的原增量');
    env.get('#stock-delta').value = '5'; failStock = false;
    await env.win.App.saveStock(7);
    check(posts(env, stockPath)[1].body.request_id === key, '库存重试复用请求键');
  }

  console.log('\n⑯ 取消与退款终态显示');
  {
    const env = makeEnv({ storage: { ops_token: 'tok', ops_view: 'admin', ops_section: 'orders' }, me: ADMIN_USER,
      request: (p) => p === '/api/admin/orders' ? response({ items: [
        { id: 1, status: 'cancelled' }, { id: 2, status: 'refunded' }], total: 2, page: 1, size: 10 }) : undefined });
    await tick();
    const html = env.get('#sec-orders').innerHTML;
    check(html.includes('已取消并退款') && html.includes('已退货退款'), '两种终态使用准确文案');
    check(!html.includes('App.shipOrder(') && !html.includes('App.refundOrder('), '终态没有再次发货或退款按钮');
  }

  console.log('\n⑰ 批量课程标签与跨分类搜索');
  {
    // 与数据库批量样本保持相同分布；此处只验证真实前端函数，不冒充浏览器连库测试。
    const categories = ['数据安全', '后端开发', '前端开发', '项目管理', '云端运维', '办公效率'];
    const courses = [];
    categories.forEach((category, group) => {
      for (let i = 0; i < 90; i++) {
        courses.push({ id: group * 100 + i + 1, title: `模拟课程 ${group}-${i}`, category,
          description: group > 0 && i < 5 ? '数据安全案例' : '企业培训实践',
          level: '入门', points: (i % 4) * 10, emoji: '课程', my_enrolled: 0, my_completed: 0 });
      }
    });
    const env = makeEnv({ storage: { ops_token: 'tok', ops_view: 'user', ops_tab: 'courses' }, me: PLAIN_USER,
      request: (p) => p === '/api/user/courses' ? response({ courses }) : undefined });
    await tick();
    const visible = () => Array.from(env.get('#tab-courses').innerHTML.matchAll(/id="course-(\d+)"/g), (m) => Number(m[1]));
    check(visible().length === 540, '课程中心完整渲染 540 门上架课程');
    env.win.App.onCourseSearch('数据安全');
    const ids = visible();
    check(ids.length === 115, '分类词作为全文搜索时返回 115 门，而非严格分类的 90 门', `实际 ${ids.length}`);
    const cross = courses.filter((c) => ids.includes(c.id) && c.category !== '数据安全').length;
    check(cross === 25, '25 门其他分类课程因描述命中而出现', `实际 ${cross}`);
    env.win.App.clearCourseSearch();
    check(visible().length === 540, '清空搜索后恢复全部上架课程');
    console.log(`     验收数据：上架 540 门，搜索命中 ${ids.length} 门，其中跨分类 ${cross} 门`);
  }

  console.log(`\n${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
