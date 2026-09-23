/* IT 运营门户 · 单页前端（原生 JS） */
(function () {
  'use strict';

  // ---------- 状态 ----------
  let token = localStorage.getItem('ops_token') || '';
  let sessionId = localStorage.getItem('ops_session') || '';
  let user = null;
  // 界面位置也落 localStorage：刷新后停在原来那一屏，而不是被打回用户端首页
  let view = localStorage.getItem('ops_view') || 'user';        // user | admin
  let tab = localStorage.getItem('ops_tab') || 'home';
  let adminSection = localStorage.getItem('ops_section') || 'dashboard';
  // 数据看板内部的两个 tab。积分明细属于看板的一层视图，不是独立菜单页 ——
  // 它讲的是「积分发出去、花在哪」，和看板的指标卡是同一件事的两种粒度。
  let dashTab = localStorage.getItem('ops_dash_tab') || 'overview';   // overview | points
  let carouselIndex = 0;
  let carouselTimer = null;
  let searchTimer = null;
  let adminPage = { courses: 1, activities: 1, gifts: 1, orders: 1, audit: 1, points: 1 };
  let allCourses = [];
  let courseQuery = '';
  let lastSearchResults = [];
  let allGifts = [];
  let giftSort = 'hot';
  // 积分明细：当前页的行放在数组里，实时推送就插进数组再整体重渲染。
  // 不直接对 tbody 做 DOM 手术（insertAdjacentHTML / 删最后一个子节点）——
  // 那样「第 1 页插一条、裁到 PAGE_SIZE」的逻辑只能靠浏览器验证，
  // 而这里是纯字符串渲染，node 里就能断言。
  let pointsRows = [];
  let pointsTotal = 0;
  let pointFilter = { emp_id: '', ref_type: '', direction: '', days: 0 };
  let pointRefTypes = null;      // ref_type -> 中文标签，服务端出（避免前后端各写一份枚举）
  let pointFilterTimer = null;
  let pointsLiveCount = 0;       // 本次停在积分页期间实时收到的条数
  let pointsNewCount = 0;        // 不在第一页/有筛选时攒下的新记录（提示用的，不硬塞进表格）
  const PAGE_SIZE = 10;
  const ROLE_LABELS = { super_admin: '超级管理员', content_admin: '内容运营', shop_admin: '商城运营', viewer: '只读运营', user: '员工' };
  const ADMIN_ROLES = ['super_admin', 'content_admin', 'shop_admin', 'viewer'];
  const isAdminRole = (r) => ADMIN_ROLES.includes(r);
  const isSuper = () => !!user && user.role === 'super_admin';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------- 工具 ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function fmtDate(s) {
    if (!s) return '';
    const d = new Date(s);
    if (isNaN(d)) return String(s);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function toast(msg, type) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast ' + (type || '');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add('hidden'), 2200);
  }
  function openModal(html) {
    $('#modal-box').innerHTML = html;
    $('#modal-overlay').classList.remove('hidden');
  }
  function closeModal() {
    $('#modal-overlay').classList.add('hidden');
    $('#modal-box').innerHTML = '';
  }
  async function api(path, opts) {
    opts = opts || {};
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': token ? 'Bearer ' + token : '',
        ...(opts.headers || {}),
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) {
      // 没有 token 时不要再调 logout：那会再发一个请求，白绕一圈
      if (token) {
        $('#login-error').textContent = '登录已过期，请重新登录';
        logout(false);
      }
      throw new Error('登录已过期');
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((x) => x.msg).join('；') : data.detail;
      const error = new Error(detail || '请求失败');
      error.status = res.status;
      throw error;
    }
    return data;
  }

  // 结果未确认的写请求按用户持久化；网络失败、500、刷新页面都复用原请求键。
  const intentBusy = new Set();
  const intentKey = (path) => 'ops_intent:' + user.emp_id + ':' + path;
  async function submitIntent(path, body, validate) {
    const key = intentKey(path);
    if (intentBusy.has(key)) return null;
    intentBusy.add(key);
    let saved;
    try {
      const raw = localStorage.getItem(key);
      saved = raw ? JSON.parse(raw) : { ...body, request_id: newEventId() };
      if (Object.keys(body).some((k) => saved[k] !== body[k])) {
        throw new Error('上次操作结果尚未确认，请按原参数重试后再发起新操作');
      }
      localStorage.setItem(key, JSON.stringify(saved));
      const result = await api(path, { method: 'POST', body: saved });
      if (typeof result.ok !== 'boolean' || (result.ok && !validate(result))) {
        throw new Error('响应不完整，请重试确认原操作结果');
      }
      localStorage.removeItem(key);
      return result;
    } catch (e) {
      if ([400, 403, 404, 409, 422].includes(e.status)) localStorage.removeItem(key);
      throw e;
    } finally {
      intentBusy.delete(key);
    }
  }
  async function redeemGift(id) {
    try {
      const result = await submitIntent(`/api/user/gifts/${id}/redeem`, {},
        (r) => Number.isInteger(r.redemption_id) && r.redemption_id > 0 && Number.isInteger(r.points_cost));
      if (!result) return false;
      if (!result.ok) { toast(reasonText(result.reason), 'error'); return false; }
      toast('兑换已确认，订单 #' + result.redemption_id, 'success');
      await refreshAll();
      return true;
    } catch (e) {
      toast(e.message + '；重试将确认原操作', 'error');
      if (user) renderGiftList();
      return false;
    }
  }
  function confirmRedeem(id) {
    try {
      if (!localStorage.getItem(intentKey(`/api/user/gifts/${id}/redeem`))) {
        toast('此操作已确认，请刷新订单列表查看', 'success'); return;
      }
      return redeemGift(id);
    } catch (e) { toast(e.message, 'error'); }
  }
  function orderStatus(status) {
    const labels = { pending: '⏳ 待发货', shipped: '🚚 已发货', cancelled: '已取消并退款', refunded: '已退货退款' };
    return `<span class="tag ${status === 'pending' ? 'tag-amber' : 'tag-gray'}">${esc(labels[status] || status)}</span>`;
  }
  async function cancelOrder(id, admin) {
    const reason = prompt('取消后将返还积分并恢复库存，请填写原因：');
    if (!reason || !reason.trim()) return;
    try {
      await api(`/api/${admin ? 'admin' : 'user'}/orders/${id}/cancel`, { method: 'POST', body: { reason } });
      toast('订单已取消并退款', 'success');
      if (admin) await loadAdminOrders();
      else await refreshAll();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function refundOrder(id) {
    if (!confirm('是否已收到退货并确认可重新入库？确认后返还积分和库存。')) return;
    const reason = prompt('请填写退货退款原因：');
    if (!reason || !reason.trim()) return;
    try {
      await api(`/api/admin/orders/${id}/refund`, { method: 'POST', body: { reason, returned: true } });
      toast('退货退款已完成', 'success'); await loadAdminOrders();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------- 埋点：本地队列 + 幂等键 + 批量重投 ----------
  // 埋点是旁路，不能拖慢也不能拖垮主流程，所以：
  //   ① 只入本地队列，不 await 网络；
  //   ② 每条事件带客户端生成的 event_id，服务端唯一约束兜底 ——
  //      重投多少次都只落一条，这是「重复上报」的解法；
  //   ③ 批量发 + 发失败留在队列下次重投 —— 这是「上报失败丢数」的解法；
  //   ④ 队列封顶 300，溢出丢最旧的并补一条 track_dropped 事件：
  //      丢数必须能被观测到，不能静默消失。
  const TRACK_KEY = 'ops_track_queue';
  const TRACK_CAP = 300;
  const TRACK_FLUSH_MS = 5000;
  let trackQueue = [];
  let trackFlushing = false;
  let trackTimer = null;
  try { trackQueue = JSON.parse(localStorage.getItem(TRACK_KEY) || '[]') || []; }
  catch (e) { trackQueue = []; }

  function saveTrackQueue() {
    try { localStorage.setItem(TRACK_KEY, JSON.stringify(trackQueue)); } catch (e) {}
  }
  function newEventId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'e-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }
  function track(eventType, refType, refId, properties) {
    if (!token) return;
    trackQueue.push({
      event_id: newEventId(),
      event_type: eventType,
      ref_type: refType || '',
      ref_id: refId == null ? null : refId,
      properties: properties || null,
      client_time: new Date().toISOString(),
    });
    if (trackQueue.length > TRACK_CAP) {
      const dropped = trackQueue.length - TRACK_CAP;
      trackQueue.splice(0, dropped);
      trackQueue.push({
        event_id: newEventId(), event_type: 'track_dropped', ref_type: '',
        ref_id: null, properties: { dropped }, client_time: new Date().toISOString(),
      });
    }
    saveTrackQueue();
    if (!trackTimer) {
      trackTimer = setTimeout(() => { trackTimer = null; flushTrack(); }, TRACK_FLUSH_MS);
    }
  }
  // keepalive=true 用于页面卸载时发出最后一批。
  // 不用 navigator.sendBeacon：它不支持自定义请求头，带不上 Authorization，
  // 只能把 token 塞进 body，反而多一个泄露面。
  async function flushTrack(keepalive) {
    if (trackFlushing || !token || !trackQueue.length) return;
    trackFlushing = true;
    const batch = trackQueue.slice(0, 200);   // 和服务端 MAX_BATCH 对齐
    try {
      const res = await fetch('/api/analytics/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
        body: JSON.stringify({ session_id: sessionId, events: batch }),
        keepalive: !!keepalive,
      });
      if (!res.ok) throw new Error('track ' + res.status);
      // 只有服务端确认收到才从队列移除；失败就原样留着等下次重投。
      // 万一确认没等到（页面卸载时很常见），下次会重投，靠 event_id 去重。
      trackQueue = trackQueue.slice(batch.length);
      saveTrackQueue();
    } catch (e) {
      // 失败不丢数：留在队列里，等下一次 flush
    } finally {
      trackFlushing = false;
    }
  }
  // pagehide 关流：标签页要没了，留着这条连接只是让服务端多挂一个订阅者。
  // 注意这里只挂 pagehide —— 不挂 visibilitychange。那个钩子是给埋点队列用的，
  // 管理员切回来时积分页的数据应当已经在推了，不该重新建连、也不该漏掉中间那几笔。
  window.addEventListener('pagehide', () => { stopPointsStream(); flushTrack(true); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushTrack(true);
  });

  // ---------- 登录 / 会话 ----------
  function showLogin() {
    $('#app').classList.add('hidden');
    $('#login-screen').classList.remove('hidden');
  }
  function showApp() {
    $('#login-screen').classList.add('hidden');
    $('#app').classList.remove('hidden');
  }
  async function login() {
    const username = $('#login-username').value.trim();
    const password = $('#login-password').value;
    const err = $('#login-error');
    err.textContent = '';
    if (!username || !password) { err.textContent = '请输入用户名和密码'; return; }
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { err.textContent = data.detail || '登录失败'; return; }
      token = data.token;
      user = data.user;
      sessionId = data.session_id || '';
      localStorage.setItem('ops_token', token);
      localStorage.setItem('ops_session', sessionId);
      enterApp();
    } catch (e) { err.textContent = '网络错误，请稍后再试'; }
  }
  let loggingOut = false;

  async function logout(redirect) {
    // 重入锁。api() 拿到 401 会回调 logout()，而 logout() 又要发一个请求 ——
    // 两者互相调用就成死循环（实测刷出过 6 万条 logout 请求，页面被拖死点不动）。
    if (loggingOut) return;
    loggingOut = true;
    try {
      // 先把实时流断掉：它带着 Authorization 头长开着，token 清空后
      // 那条连接仍会继续收数据（服务端只在心跳里复查会话）。
      stopPointsStream();
      await flushTrack();   // 先把队列发掉：此刻 token 还有效，事件才归得到本人名下
      const oldToken = token;
      // 先清本地会话，再发登出请求。顺序反了就是上面那个死循环：
      // 登出请求带着已经失效的 token → 又 401 → 又回调 logout()。
      // token 清空后，后来的 401 连请求都不会再发，循环必然终止。
      token = ''; user = null; sessionId = '';
      localStorage.removeItem('ops_token');
      localStorage.removeItem('ops_session');
      // 界面位置一并清掉：下一个登录的人不该继承上一个人的视图和 tab
      localStorage.removeItem('ops_view');
      localStorage.removeItem('ops_tab');
      localStorage.removeItem('ops_section');
      view = 'user'; tab = 'home'; adminSection = 'dashboard';
      // 队列必须清空：事件在服务端是按「当前登录人」归属的（不信客户端自报的工号），
      // 把上一个人的队列留到下一个人登录再发，会被记到下一个人名下。
      trackQueue = [];
      saveTrackQueue();
      if (oldToken) {
        try {
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + oldToken },
          });
        } catch (e) {}
      }
      if (redirect !== false) showLogin();
    } finally {
      loggingOut = false;
    }
  }
  function enterApp() {
    showApp();
    $('#user-name').textContent = (user.name || user.username) + ' · ' + (ROLE_LABELS[user.role] || '员工');
    refreshPoints();
    refreshBell();
    // 管理角色显示后台入口
    $('#admin-toggle').classList.toggle('hidden', !isAdminRole(user.role));
    track('page_visit');
    // 回到刷新前那一屏。后台视图要按角色卡一道：换了普通用户登录时
    // 不能因为上一个人留在 admin 就恢复到后台（会看到一个空面板）。
    if (view === 'admin' && isAdminRole(user.role)) setView('admin');
    else setView('user');
  }
  function refreshPoints() {
    if (user) $('#points-badge').textContent = '⭐ ' + user.points;
  }

  // ---------- 站内信通知 ----------
  async function refreshBell() {
    try {
      const d = await api('/api/user/notifications/unread');
      const badge = $('#bell-badge');
      if (d.count > 0) { badge.textContent = d.count > 99 ? '99+' : d.count; badge.classList.remove('hidden'); }
      else badge.classList.add('hidden');
    } catch (e) {}
  }
  function notifIcon(t) {
    return { redeem: '🎁', ship: '🚚', low_stock: '⚠️', system: '💬' }[t] || '💬';
  }
  async function toggleBell() {
    const dd = $('#bell-dropdown');
    if (!dd.classList.contains('hidden')) { dd.classList.add('hidden'); return; }
    try {
      const d = await api('/api/user/notifications');
      const list = d.notifications;
      dd.innerHTML = list.length
        ? list.map((n) => `
          <div class="bell-item ${n.is_read ? '' : 'unread'}">
            <div class="bell-title">${notifIcon(n.ntype)} ${esc(n.title)}</div>
            <div class="bell-content">${esc(n.content)}</div>
            <div class="bell-time">${fmtDate(n.created_at)}</div>
          </div>`).join('')
        : '<div class="bell-empty">暂无通知</div>';
      dd.classList.remove('hidden');
      await api('/api/user/notifications/read', { method: 'POST' });
      refreshBell();
    } catch (e) {}
  }

  // ---------- 视图切换 ----------
  function setView(v) {
    view = v;
    localStorage.setItem('ops_view', v);
    const isAdmin = v === 'admin';
    $('#user-nav').classList.toggle('hidden', isAdmin);
    $('#user-main').classList.toggle('hidden', isAdmin);
    $('#admin-panel').classList.toggle('hidden', !isAdmin);
    $('#admin-toggle').textContent = isAdmin ? '👤 返回用户端' : '⚙️ 进入后台';
    if (isAdmin) { renderAdminNav(); renderAdmin(); }
    else { stopPointsStream(); renderUser(); }
  }
  function renderAdminNav() {
    const perms = {
      dashboard: ADMIN_ROLES,
      courses: ['super_admin', 'content_admin'],
      activities: ['super_admin', 'content_admin'],
      gifts: ['super_admin', 'shop_admin'],
      orders: ['super_admin', 'shop_admin'],
      audit: ['super_admin'],
    };
    $$('.admin-nav').forEach((b) => {
      const allowed = perms[b.dataset.section] || [];
      b.classList.toggle('hidden', !allowed.includes(user.role));
    });
  }

  // ================= 用户端 =================
  function renderUser() {
    setTab(tab);
  }
  function setTab(t) {
    if (t === 'courses') courseQuery = '';   // 手动点课程 tab 时重置筛选
    activateTab(t);
  }
  function activateTab(t) {
    tab = t;
    localStorage.setItem('ops_tab', t);
    $$('#user-nav .tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
    $$('.tab-panel').forEach((p) => p.classList.add('hidden'));
    $('#tab-' + t).classList.remove('hidden');
    if (t === 'home') loadHome();
    else if (t === 'courses') loadCourses();
    else if (t === 'gifts') loadGifts();
    else if (t === 'activities') loadActivities();
    else if (t === 'points') loadPoints();
  }

  async function loadHome() {
    const el = $('#tab-home');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/user/home');
      let html = '';
      if (d.carousel && d.carousel.length) {
        html += renderCarousel(d.carousel);
      }
      html += '<div class="grid grid-2">';
      html += '<div class="card"><h2>📢 最新公告</h2>' + renderAnnounceList(d.announcements) + '</div>';
      html += '<div class="card"><h2>🔥 热门课程</h2>' + renderHotCourses(d.hot_courses) + '</div>';
      html += '</div>';
      el.innerHTML = html;
      if (d.carousel && d.carousel.length) startCarousel();
    } catch (e) { el.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
  }

  function renderCarousel(items) {
    const slides = items.map((s, i) => `
      <div class="carousel-slide" style="background: linear-gradient(135deg, hsl(${(i * 60 + 210) % 360} 60% 45%), hsl(${(i * 60 + 250) % 360} 55% 40%));">
        <div class="cs-emoji">${esc(s.emoji)}</div>
        <div class="cs-title">${esc(s.title)}</div>
        <div class="cs-sub">${esc(s.subtitle || '')}</div>
      </div>`).join('');
    const dots = items.map((_, i) => `<span class="${i === 0 ? 'active' : ''}" data-i="${i}"></span>`).join('');
    return `
      <div class="carousel" id="carousel">
        <div class="carousel-track" id="carousel-track">${slides}</div>
        <div class="carousel-dots" id="carousel-dots">${dots}</div>
      </div>`;
  }
  function startCarousel() {
    clearInterval(carouselTimer);
    carouselIndex = 0;
    const track = $('#carousel-track');
    const dots = $$('#carousel-dots span');
    const count = dots.length;
    function go(i) {
      carouselIndex = (i + count) % count;
      track.style.transform = `translateX(-${carouselIndex * 100}%)`;
      dots.forEach((d, k) => d.classList.toggle('active', k === carouselIndex));
    }
    carouselTimer = setInterval(() => go(carouselIndex + 1), 4000);
  }

  function renderAnnounceList(list) {
    if (!list || !list.length) return '<div class="empty">暂无公告</div>';
    return list.map((a) => `
      <div class="announce-item clickable" onclick="App.openAnnouncementDetail(${a.id})">
        <span class="a-emoji">📢</span>
        <div class="a-body">
          <div class="a-title">${esc(a.title)}</div>
          <div class="a-summary">${esc(a.summary || '')}</div>
          <div class="a-meta">${esc(a.category)} · +${a.points} 积分 · ${fmtDate(a.created_at)}</div>
        </div>
      </div>`).join('');
  }
  function renderHotCourses(list) {
    if (!list || !list.length) return '<div class="empty">暂无课程</div>';
    return list.map((c) => `
      <div class="announce-item">
        <span class="a-emoji">${esc(c.emoji)}</span>
        <div class="a-body">
          <div class="a-title">${esc(c.title)}</div>
          <div class="a-meta">${esc(c.category)} · ${esc(c.level)} · ${c.enrolls} 人报名</div>
        </div>
        <span class="tag tag-green">+${c.points}</span>
      </div>`).join('');
  }

  async function loadCourses() {
    const el = $('#tab-courses');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/user/courses');
      allCourses = d.courses;
      renderCourseList();
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  function renderCourseList() {
    const el = $('#tab-courses');
    const q = courseQuery.trim().toLowerCase();
    const filtered = q
      ? allCourses.filter((c) => (c.title + ' ' + c.category + ' ' + (c.description || '')).toLowerCase().includes(q))
      : allCourses;
    let html = '<div class="section-title">📚 课程中心</div>';
    html += `<div class="course-search">
      <input id="course-search-input" placeholder="🔍 在课程内搜索…" value="${esc(courseQuery)}" oninput="App.onCourseSearch(this.value)" />
      <button class="btn-outline" onclick="App.clearCourseSearch()">✕ 清空</button>
    </div>`;
    if (!filtered.length) html += `<div class="empty">未找到「${esc(courseQuery)}」相关课程</div>`;
    else html += '<div class="grid grid-3">' + filtered.map(renderCourseCard).join('') + '</div>';
    el.innerHTML = html;
  }
  function onCourseSearch(v) { courseQuery = v; renderCourseList(); }
  function clearCourseSearch() { courseQuery = ''; renderCourseList(); }
  function renderCourseCard(c) {
    let btn = '';
    if (c.my_completed) btn = '<span class="tag tag-green">✅ 已完成</span>';
    else if (c.my_enrolled) btn = `<button class="btn-sm" onclick="event.stopPropagation();App.completeCourse(${c.id})">✅ 完成课程 +${c.points}</button>`;
    else btn = `<button class="btn-sm" onclick="event.stopPropagation();App.enrollCourse(${c.id})">📝 报名</button>`;
    return `
      <div class="item-card clickable" id="course-${c.id}" onclick="App.openCourseDetail(${c.id})">
        <div class="item-emoji">${esc(c.emoji)}</div>
        <div class="item-title">${esc(c.title)}</div>
        <div class="item-meta">${esc(c.category)} · ${esc(c.level)} · ${esc(c.duration || '时长未知')}</div>
        <div class="item-meta">👨‍🏫 ${esc(c.instructor || '内部讲师')}</div>
        <div class="item-footer">
          <span class="tag">完成得 ${c.points} 积分</span>
          ${btn}
        </div>
      </div>`;
  }

  async function loadGifts() {
    const el = $('#tab-gifts');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/user/gifts');
      allGifts = d.gifts;
      renderGiftList();
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  function renderGiftList() {
    const el = $('#tab-gifts');
    const sorted = [...allGifts];
    if (giftSort === 'hot') sorted.sort((a, b) => (b.redeemed || 0) - (a.redeemed || 0));
    else if (giftSort === 'points_desc') sorted.sort((a, b) => b.points_cost - a.points_cost);
    else if (giftSort === 'points_asc') sorted.sort((a, b) => a.points_cost - b.points_cost);
    let html = '<div class="section-title">🎁 积分商城</div>';
    // 礼品售罄或下架后仍保留原请求的确认入口。
    try {
      const prefix = 'ops_intent:' + user.emp_id + ':/api/user/gifts/';
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        const match = key.startsWith(prefix) && key.slice(prefix.length).match(/^(\d+)\/redeem$/);
        if (match) html += `<div class="card">礼品 #${match[1]} 的兑换结果尚未确认 <button class="btn-outline" onclick="App.confirmRedeem(${Number(match[1])})">确认原兑换结果</button></div>`;
      }
    } catch (e) { toast('无法读取待确认兑换记录', 'error'); }
    html += `<div class="gift-sort">
      <label>排序：</label>
      <select onchange="App.onGiftSort(this.value)">
        <option value="hot" ${giftSort === 'hot' ? 'selected' : ''}>🔥 按热度</option>
        <option value="points_desc" ${giftSort === 'points_desc' ? 'selected' : ''}>💰 积分从高到低</option>
        <option value="points_asc" ${giftSort === 'points_asc' ? 'selected' : ''}>💰 积分从低到高</option>
      </select>
    </div>`;
    if (!sorted.length) html += '<div class="empty">暂无礼品</div>';
    else html += '<div class="grid grid-4">' + sorted.map(renderGiftCard).join('') + '</div>';
    el.innerHTML = html;
  }
  function onGiftSort(v) { giftSort = v; renderGiftList(); }
  function renderGiftCard(g) {
    const out = g.stock <= 0;
    const btn = out
      ? '<span class="tag tag-gray">已兑完</span>'
      : `<button class="btn-sm" onclick="event.stopPropagation();App.redeemGift(${g.id})">兑换</button>`;
    return `
      <div class="item-card clickable" id="gift-${g.id}" onclick="App.openGiftDetail(${g.id})">
        <div class="item-emoji">${esc(g.icon)}</div>
        <div class="item-title">${esc(g.name)}</div>
        <div class="item-meta">${esc(g.category)} · 🔥 ${g.redeemed || 0}</div>
        <div class="item-footer">
          <div>
            <div class="points-num">${g.points_cost} 积分</div>
            <div class="stock-num ${out ? 'stock-out' : ''}">库存 ${g.stock}</div>
          </div>
          ${btn}
        </div>
      </div>`;
  }

  async function openCourseDetail(id) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/user/courses/' + id);
      const c = d.course;
      let btn = '';
      let quizBtn = '';
      if (c.my_completed) {
        btn = '<span class="tag tag-green">✅ 已完成</span>';
        quizBtn = `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.openQuiz(${c.id})">📝 开始答题</button>`;
      } else if (c.my_enrolled) btn = `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.completeCourseDetail(${c.id})">✅ 完成课程 +${c.points}</button>`;
      else btn = `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.enrollCourseDetail(${c.id})">📝 报名</button>`;
      openModal(`
        <h2>${esc(c.emoji)} ${esc(c.title)}</h2>
        <div class="detail-meta">${esc(c.category)} · ${esc(c.level)} · ${esc(c.duration || '时长未知')} · 👨‍🏫 ${esc(c.instructor || '内部讲师')}</div>
        <div class="detail-tags"><span class="tag">完成得 ${c.points} 积分</span>${c.my_enrolled ? '<span class="tag tag-green">已报名</span>' : ''}</div>
        <div class="detail-section"><h3>📖 课程概述</h3><p>${esc(c.description || '暂无描述')}</p></div>
        <div class="detail-section"><h3>📎 课程资料</h3><p class="detail-placeholder">📄 文档 / 🎬 视频（后续上线，敬请期待）</p></div>
        <div class="form-actions">${btn}${quizBtn}<button class="btn-outline" onclick="App.closeModal()">关闭</button></div>
      `);
    } catch (e) { openModal('<div class="empty">加载失败</div>'); }
  }

  async function openGiftDetail(id) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/user/gifts/' + id);
      const g = d.gift;
      const out = g.stock <= 0;
      const btn = out
        ? '<span class="tag tag-gray">已兑完</span>'
        : `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.redeemGiftDetail(${g.id})">🎁 兑换（${g.points_cost} 积分）</button>`;
      openModal(`
        <h2>${esc(g.icon)} ${esc(g.name)}</h2>
        <div class="detail-meta">${esc(g.category)} · 需 <b>${g.points_cost}</b> 积分 · 库存 ${g.stock}</div>
        <div class="detail-section"><h3>📝 礼品介绍</h3><p>${esc(g.description || '暂无描述')}</p></div>
        <div class="form-actions">${btn}<button class="btn-outline" onclick="App.closeModal()">关闭</button></div>
      `);
    } catch (e) { openModal('<div class="empty">加载失败</div>'); }
  }

  async function openAnnouncementDetail(id) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/user/announcements/' + id);
      const a = d.announcement;
      const btn = a.my_read
        ? '<span class="tag tag-green">✅ 已读</span>'
        : `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.readAnnouncementDetail(${a.id})">阅读 +${a.points}</button>`;
      openModal(`
        <h2>📢 ${esc(a.title)}</h2>
        <div class="detail-meta">${esc(a.category)} · ${fmtDate(a.created_at)}</div>
        <div class="detail-section"><h3>📄 公告内容</h3><p>${esc(a.content || a.summary || '')}</p></div>
        <div class="form-actions">${btn}<button class="btn-outline" onclick="App.closeModal()">关闭</button></div>
      `);
    } catch (e) { openModal('<div class="empty">加载失败</div>'); }
  }

  async function openActivityDetail(id) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/user/activities/' + id);
      const a = d.activity;
      const btn = a.my_participated
        ? '<span class="tag tag-green">✅ 已参与</span>'
        : `<button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.participateDetail(${a.id})">报名参与 +${a.points}</button>`;
      openModal(`
        <h2>${esc(a.emoji)} ${esc(a.title)}</h2>
        <div class="detail-meta">${esc(a.subtitle || '')}</div>
        <div class="detail-tags"><span class="tag">📅 ${esc(a.event_time || '')}</span><span class="tag">📍 ${esc(a.location || '')}</span><span class="tag">参与得 ${a.points} 积分</span></div>
        <div class="detail-section"><h3>📝 活动介绍</h3><p>${esc(a.description || '暂无描述')}</p></div>
        <div class="form-actions">${btn}<button class="btn-outline" onclick="App.closeModal()">关闭</button></div>
      `);
    } catch (e) { openModal('<div class="empty">加载失败</div>'); }
  }

  async function openQuiz(cid) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/user/courses/' + cid + '/quiz');
      if (!d.questions.length) { openModal('<div class="empty">该课程暂无题目</div>'); return; }
      let html = '<h2>📝 课程测验</h2>';
      html += d.questions.map((q, i) => `
        <div class="quiz-q">
          <div class="qq-title">${i + 1}. ${esc(q.question)}</div>
          ${['A', 'B', 'C', 'D'].map((opt) => q['option_' + opt.toLowerCase()] ? `
            <label class="qq-opt"><input type="radio" name="q${q.id}" value="${opt}" /> ${opt}. ${esc(q['option_' + opt.toLowerCase()])}</label>` : '').join('')}
        </div>`).join('');
      html += `<div class="form-actions"><button class="btn-primary" style="width:auto" onclick="App.submitQuiz(${cid})">提交</button><button class="btn-outline" onclick="App.closeModal()">取消</button></div>`;
      openModal(html);
    } catch (e) { openModal('<div class="empty">' + esc(e.message) + '</div>'); }
  }

  async function submitQuiz(cid) {
    const answers = [];
    $$('#modal-box input[type="radio"]:checked').forEach((r) => {
      answers.push({ qid: parseInt(r.name.slice(1), 10), answer: r.value });
    });
    try {
      const d = await api('/api/user/courses/' + cid + '/quiz', { method: 'POST', body: { answers } });
      openModal(`
        <h2>📊 答题结果</h2>
        <div class="quiz-score">${d.score} / ${d.total} 分</div>
        ${d.results.map((r, i) => `<div class="quiz-result ${r.correct ? 'ok' : 'wrong'}">第 ${i + 1} 题：${r.correct ? '✅ 正确' : '❌ 错误（正确答案 ' + r.answer + '）'}</div>`).join('')}
        <div class="form-actions"><button class="btn-outline" onclick="App.closeModal()">关闭</button></div>
      `);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadActivities() {
    const el = $('#tab-activities');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/user/activities');
      let html = '<div class="section-title">🎪 活动中心</div><div class="grid grid-3">';
      html += d.activities.map((a) => {
        const btn = a.my_participated
          ? '<span class="tag tag-green">✅ 已参与</span>'
          : `<button class="btn-sm" onclick="event.stopPropagation();App.participate(${a.id})">报名参与 +${a.points}</button>`;
        return `
          <div class="item-card clickable" onclick="App.openActivityDetail(${a.id})">
            <div class="item-emoji">${esc(a.emoji)}</div>
            <div class="item-title">${esc(a.title)}</div>
            <div class="item-meta">${esc(a.subtitle || '')}</div>
            <div class="item-meta">📅 ${esc(a.event_time || '')} · 📍 ${esc(a.location || '')}</div>
            <div class="item-footer"><span class="tag">参与得 ${a.points} 积分</span>${btn}</div>
          </div>`;
      }).join('');
      html += '</div>';
      html += '<div class="section-title">📢 公告中心</div><div class="card">';
      html += d.announcements.map((a) => {
        const btn = a.my_read
          ? '<span class="tag tag-green">已读</span>'
          : `<button class="btn-outline" onclick="event.stopPropagation();App.readAnnouncement(${a.id})">阅读 +${a.points}</button>`;
        return `
          <div class="announce-item clickable" onclick="App.openAnnouncementDetail(${a.id})">
            <span class="a-emoji">📢</span>
            <div class="a-body">
              <div class="a-title">${esc(a.title)}</div>
              <div class="a-summary">${esc(a.summary || '')}</div>
              <div class="a-meta">${esc(a.category)} · ${fmtDate(a.created_at)}</div>
            </div>
            ${btn}
          </div>`;
      }).join('') + '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }

  async function loadPoints() {
    const el = $('#tab-points');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const [p, o] = await Promise.all([api('/api/user/points'), api('/api/user/orders')]);
      let html = `<div class="card"><h2>💰 我的积分</h2>
        <div class="stat-card" style="box-shadow:none;padding:0"><div class="s-value">${p.points}</div>
        <div class="s-label">当前可用积分</div></div></div>`;
      html += '<div class="grid grid-2">';
      html += '<div class="card"><h2>🧾 积分流水</h2>' + (p.records.length ? p.records.map((r) => `
        <div class="record-row">
          <div><div class="rec-note">${esc(r.note)}</div><div class="rec-time">${fmtDate(r.created_at)}</div></div>
          <div class="${r.points >= 0 ? 'rec-pos' : 'rec-neg'}">${r.points >= 0 ? '+' : ''}${r.points}</div>
        </div>`).join('') : '<div class="empty">暂无流水</div>') + '</div>';
      html += '<div class="card"><h2>📦 我的兑换</h2>' + (o.orders.length ? o.orders.map((r) => `
        <div class="record-row">
          <div><div class="rec-note">${esc(r.gift_icon)} ${esc(r.gift_name)}</div>
          <div class="rec-time">${fmtDate(r.created_at)}</div></div>
          <div>${orderStatus(r.status)}${r.status === 'pending' ? `<button class="btn-outline" onclick="App.cancelOrder(${r.id}, false)">取消兑换</button>` : ''}</div>
        </div>`).join('') : '<div class="empty">暂无兑换记录</div>') + '</div>';
      html += '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }

  // ---------- 用户动作 ----------
  async function doAction(path, msg) {
    try {
      const r = await api(path, { method: 'POST' });
      if (r.ok) { toast(msg || '操作成功', 'success'); await refreshAll(); return true; }
      toast(reasonText(r.reason), 'error');
      return false;
    } catch (e) { toast(e.message, 'error'); return false; }
  }
  function reasonText(r) {
    const map = {
      already_enrolled: '已经报名过了', already_completed: '已完成该课程',
      already_participated: '已参与过该活动', already_read: '已阅读过该公告',
      out_of_stock: '库存不足', insufficient_points: '积分不足', not_found: '内容不存在',
    };
    return map[r] || '操作失败';
  }
  async function refreshAll() {
    try {
      const me = await api('/api/auth/me');
      user = me.user;
      refreshPoints();
      refreshBell();
      renderUser();
    } catch (e) {}
  }

  // ================= 管理后台 =================
  function renderAdmin() {
    setAdminSection(adminSection);
  }
  function setAdminSection(s) {
    adminSection = s;
    localStorage.setItem('ops_section', s);
    $$('.admin-nav').forEach((b) => b.classList.toggle('active', b.dataset.section === s));
    $$('.admin-section').forEach((p) => p.classList.add('hidden'));
    $('#sec-' + s).classList.remove('hidden');
    // 积分 tab 是看板内部的一层视图，离开看板（或切回总览）就没人看这条流了。
    // 一条开着的流在服务端占着一个订阅队列（上限 100 条），所以主动断。
    if (s !== 'dashboard' || dashTab !== 'points') stopPointsStream();
    if (s === 'dashboard') loadDashboard();
    else if (s === 'courses') loadAdminCourses();
    else if (s === 'activities') loadAdminActivities();
    else if (s === 'gifts') loadAdminGifts();
    else if (s === 'orders') loadAdminOrders();
    else if (s === 'audit') loadAdminAudit();
  }

  // 看板内的 tab 切换。和 setAdminSection 一样要处理实时流的生死。
  function setDashTab(t) {
    dashTab = t;
    localStorage.setItem('ops_dash_tab', t);
    if (t !== 'points') stopPointsStream();
    loadDashboard();
  }

  // 看板顶栏的两个 tab。放在看板内部而不是左侧菜单：积分明细和看板指标卡
  // 讲的是同一件事（积分发出去、花在哪），只是粒度不同（汇总 vs 逐笔）。
  function dashTabBar() {
    const tabs = [['overview', '📊 总览'], ['points', '💰 积分']];
    return '<div class="dash-tabs">' + tabs.map(([k, t]) =>
      `<button class="dash-tab ${dashTab === k ? 'active' : ''}"
               onclick="App.setDashTab('${k}')">${t}</button>`).join('') + '</div>';
  }
  const statCard = (icon, label, value) => `
    <div class="stat-card"><div class="s-icon">${icon}</div>
    <div class="s-value">${value}</div><div class="s-label">${label}</div></div>`;

  async function loadDashboard() {
    const el = $('#sec-dashboard');
    // 加载中也先画 tab 条：否则切 tab 的那一瞬间按钮会消失，看着像点坏了
    el.innerHTML = dashTabBar() + '<div class="empty">加载中…</div>';
    try {
      if (dashTab === 'points') { await loadAdminPoints(); return; }
      const d = await api('/api/admin/dashboard');
      const o = d.overview;
      const stat = statCard;
      let html = dashTabBar() + '<div class="section-title">📊 数据看板</div>';
      html += '<div class="stat-grid">' +
        stat('👥', '用户总数', o.total_users) +
        stat('🖱️', '今日访问 DAU', o.today_visits) +
        stat('📚', '在架课程', o.active_courses) +
        stat('🎪', '在架活动', o.active_activities) +
        stat('🎁', '在架礼品', o.active_gifts) +
        stat('💹', '累计发放积分', o.points_issued) +
        stat('💸', '累计消耗积分', o.points_spent) +
        stat('📦', '待发货订单', o.pending_orders) +
        '</div>';

      html += '<div class="card"><h2>📈 近 7 天积分趋势</h2>' + renderBarChart(d.daily_points) + '</div>';

      html += '<div class="card"><h2>🔻 转化漏斗</h2>' + renderFunnel(d.funnel, d.rates) + '</div>';

      html += '<div class="card"><h2>🚚 履约时效</h2><div class="insight-line">平均发货时长 <b>' + (d.shipping ? d.shipping.avg_hours : 0) + '</b> 小时 · 已发货 <b>' + (d.shipping ? d.shipping.c : 0) + '</b> 单 · 待发货 <b>' + o.pending_orders + '</b> 单</div></div>';

      html += '<div class="grid grid-2">';
      html += '<div class="card"><h2>🔍 搜索热词 TOP</h2>' + renderKeywords(d.search_keywords) + '</div>';
      html += '<div class="card"><h2>📊 课程答题正确率</h2>' + renderAccuracy(d.course_accuracy) + '</div>';
      html += '</div>';
      html += '<div class="grid grid-2">';
      html += '<div class="card"><h2>👁️ 课程点击热度 TOP5</h2>' + renderViewRank(d.top_course_views) + '</div>';
      html += '<div class="card"><h2>👁️ 礼品点击热度 TOP5</h2>' + renderViewRank(d.top_gift_views) + '</div>';
      html += '</div>';

      html += '<div class="grid grid-2">';
      html += '<div class="card"><h2>🏆 热门礼品 TOP5</h2>' + (d.top_gifts.length ? d.top_gifts.map((g, i) => `
        <div class="record-row"><div class="rec-note">${i + 1}. ${esc(g.icon)} ${esc(g.name)}</div>
        <div class="tag">兑换 ${g.c} 次</div></div>`).join('') : '<div class="empty">暂无数据</div>') + '</div>';
      html += '<div class="card"><h2>🕒 最近订单</h2>' + (d.recent_orders.length ? d.recent_orders.map((r) => `
        <div class="record-row"><div class="rec-note">${esc(r.gift_icon)} ${esc(r.gift_name)} · ${esc(r.user_name)}</div>
        ${orderStatus(r.status)}</div>`).join('') : '<div class="empty">暂无订单</div>') + '</div>';
      html += '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
  }
  function renderBarChart(daily) {
    if (!daily || !daily.length) return '<div class="empty">暂无数据</div>';
    const max = Math.max(1, ...daily.flatMap((d) => [d.issued, d.spent]));
    const bars = daily.map((d) => {
      const h1 = Math.round((d.issued / max) * 130);
      const h2 = Math.round((d.spent / max) * 130);
      return `<div class="bar-col">
        <div style="display:flex;gap:4px;align-items:flex-end;height:130px;">
          <div class="bar issued" style="height:${h1}px" title="发放 ${d.issued}"></div>
          <div class="bar spent" style="height:${h2}px" title="消耗 ${d.spent}"></div>
        </div>
        <div class="bar-label">${String(d.d).slice(5)}</div>
      </div>`;
    }).join('');
    return `<div class="bar-chart">${bars}</div>
      <div style="display:flex;gap:16px;margin-top:10px;font-size:12px;color:#6b7280">
        <span>🟩 发放</span><span>🟥 消耗</span></div>`;
  }
  function renderFunnel(f, r) {
    const rows = [
      ['📚 课程：搜索 → 浏览 → 报名 → 完成', `${f.search} → ${f.course_view} → ${f.course_enroll} → ${f.course_complete}`, `浏览→报名 ${r.course_view_to_enroll}% · 完成率 ${r.course_complete}%`],
      ['🎁 礼品：浏览 → 兑换', `${f.gift_view} → ${f.redeem}`, `兑换率 ${r.gift_redeem}%`],
      ['📢 公告：浏览 → 阅读', `${f.announcement_view} → ${f.announcement_read}`, `阅读率 ${r.announcement_read}%`],
      ['🎪 活动 · 🔍 搜索 · ⭐ 积分', `活动 ${f.activity_participants} 人次 · 搜索 ${f.search} 次 · 积分查看 ${f.points_view} 次`, ''],
    ];
    return rows.map(([label, val, rate]) => `
      <div class="funnel-row">
        <div class="fr-label">${label}</div>
        <div class="fr-val">${val}</div>
        <div class="fr-rate">${rate}</div>
      </div>`).join('');
  }
  function renderKeywords(list) {
    if (!list || !list.length) return '<div class="empty">暂无搜索数据</div>';
    return '<div class="kw-cloud">' + list.map((k) => `<span class="kw-tag">${esc(k.kw)} <b>${k.c}</b></span>`).join('') + '</div>';
  }
  function renderAccuracy(list) {
    if (!list || !list.length) return '<div class="empty">暂无答题数据</div>';
    return list.map((a) => `
      <div class="record-row">
        <div class="rec-note">${esc(a.emoji)} ${esc(a.title)} <span class="tag tag-gray">${a.attempts} 人次</span></div>
        <div class="tag ${a.accuracy >= 60 ? 'tag-green' : 'tag-amber'}">${a.accuracy}% 正确率</div>
      </div>`).join('');
  }
  function renderViewRank(list) {
    if (!list || !list.length) return '<div class="empty">暂无浏览数据</div>';
    return list.map((x, i) => `
      <div class="record-row">
        <div class="rec-note">${i + 1}. ${esc(x.emoji || x.icon || '📄')} ${esc(x.title || x.name)}</div>
        <div class="tag">${x.views} 次</div>
      </div>`).join('');
  }

  // ----- 课程管理 -----
  async function loadAdminCourses() {
    const el = $('#sec-courses');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/admin/courses?page=' + adminPage.courses + '&size=' + PAGE_SIZE);
      window._adminCoursesCache = d.items;
      let html = '<div class="section-title">📚 课程管理</div>';
      html += `<button class="btn-primary" style="width:auto;padding:10px 18px" onclick="App.openCourseForm()">➕ 新增课程</button>`;
      html += '<div class="card"><table class="table"><thead><tr><th>emoji</th><th>标题</th><th>分类</th><th>等级</th><th>积分</th><th>报名</th><th>状态</th><th>操作</th></tr></thead><tbody>';
      html += d.items.map((c) => `
        <tr>
          <td>${esc(c.emoji)}</td>
          <td>${esc(c.title)}</td>
          <td>${esc(c.category)}</td>
          <td>${esc(c.level)}</td>
          <td>${c.points}</td>
          <td>${c.enrolls}</td>
          <td>${c.status === 'active' ? '<span class="tag tag-green">上架</span>' : '<span class="tag tag-gray">下架</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn-outline" onclick="App.openCourseForm(${c.id})">✏️ 编辑</button>
            <button class="btn-outline" onclick="App.openQuestionManager(${c.id})">📝 题目</button>
            <button class="btn-outline" onclick="App.toggleStatus('courses', ${c.id}, '${c.status === 'active' ? 'offline' : 'active'}')">${c.status === 'active' ? '⏸️ 下架' : '✅ 上架'}</button>
          </td>
        </tr>`).join('');
      html += '</tbody></table>' + renderPager(d.total, d.page, d.size, 'courses') + '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  function openCourseForm(id) {
    const cur = id ? (window._adminCoursesCache || []).find((c) => c.id === id) : null;
    openModal(`
      <h2>${id ? '✏️ 编辑课程' : '➕ 新增课程'}</h2>
      <div class="form-grid">
        <div class="form-field"><label>标题</label><input id="cf-title" value="${esc(cur?.title || '')}" /></div>
        <div class="form-field"><label>分类</label><input id="cf-category" value="${esc(cur?.category || '通用')}" /></div>
        <div class="form-field"><label>等级</label>
          <select id="cf-level"><option>入门</option><option>进阶</option><option>高级</option></select></div>
        <div class="form-field"><label>时长</label><input id="cf-duration" value="${esc(cur?.duration || '')}" /></div>
        <div class="form-field"><label>讲师</label><input id="cf-instructor" value="${esc(cur?.instructor || '')}" /></div>
        <div class="form-field"><label>完成积分</label><input id="cf-points" type="number" value="${cur?.points ?? 0}" /></div>
        <div class="form-field full"><label>emoji</label><input id="cf-emoji" value="${esc(cur?.emoji || '📚')}" /></div>
        <div class="form-field full"><label>课程描述</label><textarea id="cf-desc" rows="3">${esc(cur?.description || '')}</textarea></div>
      </div>
      <div class="form-actions">
        <button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.saveCourse(${id || 0})">保存</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
    if (cur) $('#cf-level').value = cur.level;
  }
  async function saveCourse(id) {
    const body = {
      title: $('#cf-title').value, category: $('#cf-category').value, level: $('#cf-level').value,
      duration: $('#cf-duration').value, instructor: $('#cf-instructor').value,
      points: parseInt($('#cf-points').value || '0', 10), description: $('#cf-desc').value, emoji: $('#cf-emoji').value,
    };
    try {
      await api(id ? '/api/admin/courses/' + id : '/api/admin/courses', { method: id ? 'PUT' : 'POST', body });
      toast('已保存', 'success'); closeModal(); loadAdminCourses();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 活动管理 -----
  async function loadAdminActivities() {
    const el = $('#sec-activities');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/admin/activities?page=' + adminPage.activities + '&size=' + PAGE_SIZE);
      window._adminActivitiesCache = d.items;
      let html = '<div class="section-title">🎪 活动管理</div>';
      html += `<button class="btn-primary" style="width:auto;padding:10px 18px" onclick="App.openActivityForm()">➕ 新增活动</button>`;
      html += '<div class="card"><table class="table"><thead><tr><th>emoji</th><th>标题</th><th>时间</th><th>积分</th><th>参与</th><th>轮播</th><th>状态</th><th>操作</th></tr></thead><tbody>';
      html += d.items.map((a) => `
        <tr>
          <td>${esc(a.emoji)}</td>
          <td>${esc(a.title)}</td>
          <td>${esc(a.event_time)}</td>
          <td>${a.points}</td>
          <td>${a.parts}</td>
          <td>${a.is_carousel ? '<span class="tag">轮播 #' + a.carousel_order + '</span>' : '<span class="tag tag-gray">—</span>'}</td>
          <td>${a.status === 'active' ? '<span class="tag tag-green">上架</span>' : '<span class="tag tag-gray">下架</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn-outline" onclick="App.openActivityForm(${a.id})">✏️ 编辑</button>
            <button class="btn-outline" onclick="App.toggleStatus('activities', ${a.id}, '${a.status === 'active' ? 'offline' : 'active'}')">${a.status === 'active' ? '⏸️ 下架' : '✅ 上架'}</button>
          </td>
        </tr>`).join('');
      html += '</tbody></table>' + renderPager(d.total, d.page, d.size, 'activities') + '</div>';
      html += '<div class="card"><h2>🖼️ 轮播图顺序管理</h2><p class="section-sub">勾选要上轮播图的活动并拖动排序（数字越小越靠前）</p><div id="carousel-manager"></div></div>';
      el.innerHTML = html;
      renderCarouselManager(d.items);
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  function renderCarouselManager(acts) {
    const wrap = $('#carousel-manager');
    const onCarousel = acts.filter((a) => a.is_carousel).sort((a, b) => a.carousel_order - b.carousel_order);
    const others = acts.filter((a) => !a.is_carousel);
    const row = (a, checked) => `
      <div class="announce-item">
        <span class="a-emoji">${esc(a.emoji)}</span>
        <div class="a-body"><div class="a-title">${esc(a.title)}</div></div>
        <input type="checkbox" ${checked ? 'checked' : ''} data-id="${a.id}" class="carousel-check" />
        ${checked ? `<input type="number" value="${a.carousel_order}" data-order="${a.id}" class="carousel-order" style="width:70px" />` : ''}
      </div>`;
    wrap.innerHTML = [...onCarousel, ...others].map((a) => row(a, a.is_carousel)).join('') +
      `<button class="btn-primary" style="width:auto;margin-top:14px" onclick="App.saveCarousel()">💾 保存轮播图</button>`;
  }
  async function saveCarousel() {
    const items = [];
    $$('.carousel-check').forEach((chk) => {
      if (!chk.checked) return;
      const id = parseInt(chk.dataset.id, 10);
      const orderEl = document.querySelector(`.carousel-order[data-order="${id}"]`);
      const order = orderEl ? parseInt(orderEl.value || '0', 10) : 0;
      items.push({ activity_id: id, order });
    });
    items.sort((a, b) => a.order - b.order);
    items.forEach((it, i) => it.order = i + 1);
    try {
      await api('/api/admin/carousel', { method: 'PUT', body: { items } });
      toast('轮播图已更新', 'success'); loadAdminActivities();
    } catch (e) { toast(e.message, 'error'); }
  }
  function openActivityForm(id) {
    const cur = id ? (window._adminActivitiesCache || []).find((a) => a.id === id) : null;
    openModal(`
      <h2>${id ? '✏️ 编辑活动' : '➕ 新增活动'}</h2>
      <div class="form-grid">
        <div class="form-field"><label>标题</label><input id="af-title" value="${esc(cur?.title || '')}" /></div>
        <div class="form-field"><label>副标题</label><input id="af-subtitle" value="${esc(cur?.subtitle || '')}" /></div>
        <div class="form-field"><label>时间</label><input id="af-time" value="${esc(cur?.event_time || '')}" placeholder="2026-09-25 09:00" /></div>
        <div class="form-field"><label>地点</label><input id="af-location" value="${esc(cur?.location || '')}" /></div>
        <div class="form-field"><label>参与积分</label><input id="af-points" type="number" value="${cur?.points ?? 0}" /></div>
        <div class="form-field"><label>emoji</label><input id="af-emoji" value="${esc(cur?.emoji || '🎪')}" /></div>
        <div class="form-field full"><label>描述</label><textarea id="af-desc" rows="3">${esc(cur?.description || '')}</textarea></div>
      </div>
      <div class="form-actions">
        <button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.saveActivity(${id || 0})">保存</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
  }
  async function saveActivity(id) {
    const body = {
      title: $('#af-title').value, subtitle: $('#af-subtitle').value, event_time: $('#af-time').value,
      location: $('#af-location').value, points: parseInt($('#af-points').value || '0', 10),
      description: $('#af-desc').value, emoji: $('#af-emoji').value,
    };
    try {
      await api(id ? '/api/admin/activities/' + id : '/api/admin/activities', { method: id ? 'PUT' : 'POST', body });
      toast('已保存', 'success'); closeModal(); loadAdminActivities();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 礼品管理（积分兑换）-----
  async function loadAdminGifts() {
    const el = $('#sec-gifts');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/admin/gifts?page=' + adminPage.gifts + '&size=' + PAGE_SIZE);
      window._adminGiftsCache = d.items;
      let html = '<div class="section-title">🎁 积分兑换 · 礼品管理</div>';
      html += `<button class="btn-primary" style="width:auto;padding:10px 18px" onclick="App.openGiftForm()">➕ 新增礼品</button>`;
      html += '<div class="card"><table class="table"><thead><tr><th>icon</th><th>名称</th><th>分类</th><th>积分</th><th>库存</th><th>已兑换</th><th>状态</th><th>操作</th></tr></thead><tbody>';
      html += d.items.map((g) => `
        <tr>
          <td>${esc(g.icon)}</td>
          <td>${esc(g.name)}</td>
          <td>${esc(g.category)}</td>
          <td>${g.points_cost}</td>
          <td>${g.stock}</td>
          <td>${g.redeemed}</td>
          <td>${g.status === 'active' ? '<span class="tag tag-green">上架</span>' : '<span class="tag tag-gray">下架</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn-outline" onclick="App.openGiftForm(${g.id})">✏️ 编辑</button>
            <button class="btn-outline" onclick="App.openStockForm(${g.id})">调整库存</button>
            <button class="btn-outline" onclick="App.toggleStatus('gifts', ${g.id}, '${g.status === 'active' ? 'offline' : 'active'}')">${g.status === 'active' ? '⏸️ 下架' : '✅ 上架'}</button>
          </td>
        </tr>`).join('');
      html += '</tbody></table>' + renderPager(d.total, d.page, d.size, 'gifts') + '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  function openGiftForm(id) {
    const cur = id ? (window._adminGiftsCache || []).find((g) => g.id === id) : null;
    openModal(`
      <h2>${id ? '✏️ 编辑礼品' : '➕ 新增礼品'}</h2>
      <div class="form-grid">
        <div class="form-field"><label>名称</label><input id="gf-name" value="${esc(cur?.name || '')}" /></div>
        <div class="form-field"><label>分类</label><input id="gf-category" value="${esc(cur?.category || '周边')}" /></div>
        <div class="form-field"><label>积分价格</label><input id="gf-cost" type="number" min="0" max="1000000" value="${cur?.points_cost ?? 0}" /></div>
        ${id ? '<div class="form-field">库存通过独立「调整库存」操作修改</div>' : '<div class="form-field"><label>初始库存</label><input id="gf-stock" type="number" min="0" max="1000000000" value="0" /></div>'}
        <div class="form-field full"><label>emoji 图标</label><input id="gf-icon" value="${esc(cur?.icon || '🎁')}" /></div>
      </div>
      <div class="form-actions">
        <button class="btn-primary" style="width:auto;padding:10px 20px" onclick="App.saveGift(${id || 0})">保存</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
  }
  async function saveGift(id) {
    const body = {
      name: $('#gf-name').value, category: $('#gf-category').value,
      points_cost: Number($('#gf-cost').value || '0'),
      icon: $('#gf-icon').value,
    };
    if (!id) body.stock = Number($('#gf-stock').value || '0');
    try {
      await api(id ? '/api/admin/gifts/' + id : '/api/admin/gifts', { method: id ? 'PUT' : 'POST', body });
      toast('已保存', 'success'); closeModal(); loadAdminGifts();
    } catch (e) { toast(e.message, 'error'); }
  }

  function openStockForm(id) {
    let pending = {};
    try { pending = JSON.parse(localStorage.getItem(intentKey(`/api/admin/gifts/${id}/stock`)) || '{}'); }
    catch (e) { toast('无法读取待确认库存操作', 'error'); return; }
    openModal(`<h2>调整库存</h2>
      <p>填写增减量，不是最终库存；未确认的操作按原参数重试。</p>
      <div class="form-field"><label>增减量（补货为正，调减为负）</label><input id="stock-delta" type="number" value="${esc(pending.delta ?? '')}" /></div>
      <div class="form-field"><label>原因</label><input id="stock-reason" maxlength="100" value="${esc(pending.reason || '')}" /></div>
      <div class="form-actions"><button class="btn-primary" onclick="App.saveStock(${id})">提交</button><button class="btn-outline" onclick="App.closeModal()">关闭</button></div>`);
  }
  async function saveStock(id) {
    const delta = Number($('#stock-delta').value);
    const reason = $('#stock-reason').value.trim();
    if (!Number.isInteger(delta) || delta === 0 || !reason) { toast('请输入非零整数和调整原因', 'error'); return; }
    try {
      const result = await submitIntent(`/api/admin/gifts/${id}/stock`, { delta, reason },
        (r) => Number.isInteger(r.record_id) && r.record_id > 0 && Number.isInteger(r.stock));
      if (!result) return;
      if (!result.ok) { toast(reasonText(result.reason), 'error'); return; }
      toast('库存调整已确认', 'success'); closeModal(); await loadAdminGifts();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function reconcileOrders() {
    try {
      const d = await api('/api/admin/orders/reconciliation');
      openModal(`<h2>兑换对账</h2><p>${d.ok ? '当前核对项一致' : '发现差异，请核查，未自动改账'}</p>
        <p>账户差异 ${d.accounts.length}，订单差异 ${d.orders.length}，库存差异 ${d.stocks.length}，孤立流水 ${d.orphan_records.length}</p>
        <p>历史订单 ${d.legacy_orders} 笔：接入前的库存出库记录不在本次完整性保证内。</p>
        <pre style="white-space:pre-wrap">${esc(JSON.stringify(d, null, 2))}</pre>
        <button class="btn-outline" onclick="App.closeModal()">关闭</button>`);
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 订单发货 -----
  async function loadAdminOrders() {
    const el = $('#sec-orders');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/admin/orders?page=' + adminPage.orders + '&size=' + PAGE_SIZE);
      let html = '<div class="section-title">🚚 订单履约</div><button class="btn-outline" onclick="App.reconcileOrders()">核对积分／订单／库存</button><div class="card"><table class="table">';
      html += '<thead><tr><th>订单号</th><th>礼品</th><th>用户</th><th>消耗积分</th><th>下单时间</th><th>状态</th><th>物流单号</th><th>操作</th></tr></thead><tbody>';
      html += d.items.map((r) => `
        <tr>
          <td>#${r.id}</td>
          <td>${esc(r.gift_icon)} ${esc(r.gift_name)}</td>
          <td>${esc(r.user_name)} (${esc(r.username)})</td>
          <td>${r.points_cost}</td>
          <td>${fmtDate(r.created_at)}</td>
          <td>${orderStatus(r.status)}</td>
          <td>${esc(r.express || '—')}</td>
          <td>${r.status === 'pending' ? `<button class="btn-green btn-outline" onclick="App.shipOrder(${r.id})">📦 发货</button><button class="btn-outline" onclick="App.cancelOrder(${r.id}, true)">取消</button>` : r.status === 'shipped' ? `<button class="btn-outline" onclick="App.refundOrder(${r.id})">确认退货退款</button>` : '<span class="tag tag-gray">已关闭</span>'}</td>
        </tr>`).join('');
      html += '</tbody></table>' + renderPager(d.total, d.page, d.size, 'orders') + '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  }
  async function shipOrder(id) {
    const express = prompt('请输入物流单号：');
    if (!express) return;
    try {
      await api('/api/admin/orders/' + id + '/ship', { method: 'POST', body: { express } });
      toast('已发货', 'success'); loadAdminOrders();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 积分明细（全员流水 + 实时推送 + 人工操作）-----
  //
  // 数据来源两条腿：首屏走 REST（可分页、可筛选），增量走 SSE。
  // 实时帧**不重新拉列表** —— 收到一帧就重拉一次，一阵流水会变成一轮请求风暴。
  // 断线/丢帧才重拉，那条路径是显式的（见 applyPointFrame 与 dropped 分支）。

  // 实时通道状态。三个字段的含义：
  //   ctrl   非空 = 有一个连接正开着（也是「重入锁」，防止开出两条流）
  //   timer  重连定时器
  //   giveUp 不再重连（401/403，或服务端回了 event: bye 说会话没了）
  const pointsStream = { ctrl: null, timer: null, retry: 0, giveUp: false };

  // 趋势图的刷新节奏与实时流分开：流是「来一条推一条」，趋势是按天聚合，
  // 连着来十条也只有当天那一格变了，没必要跟着刷十次。
  const pointsTrend = { timer: null, busy: false };

  function pointQuery(page) {
    const q = [];
    if (pointFilter.emp_id) q.push('emp_id=' + encodeURIComponent(pointFilter.emp_id));
    if (pointFilter.ref_type) q.push('ref_type=' + encodeURIComponent(pointFilter.ref_type));
    if (pointFilter.direction) q.push('direction=' + encodeURIComponent(pointFilter.direction));
    if (pointFilter.days) q.push('days=' + pointFilter.days);
    q.push('page=' + page, 'size=' + PAGE_SIZE);
    return q.join('&');
  }
  function pointFilterActive() {
    return !!(pointFilter.emp_id || pointFilter.ref_type || pointFilter.direction || pointFilter.days);
  }
  function setPointFilter(key, value) {
    pointFilter[key] = value;
    // 每敲一个字就查一次太吵，和搜索框一样做防抖
    clearTimeout(pointFilterTimer);
    pointFilterTimer = setTimeout(() => { adminPage.points = 1; loadAdminPoints(); }, 300);
  }
  function resetPointFilter() {
    pointFilter = { emp_id: '', ref_type: '', direction: '', days: 0 };
    adminPage.points = 1;
    loadAdminPoints();
  }

  async function loadAdminPoints() {
    const el = $('#sec-dashboard');
    el.innerHTML = dashTabBar() + '<div class="empty">加载中…</div>';
    try {
      // 类型清单只用拉一次，服务端是单一来源（POINT_REF_LABELS）
      if (!pointRefTypes) {
        const rt = await api('/api/admin/points/ref-types');
        pointRefTypes = {};
        rt.ref_types.forEach((x) => { pointRefTypes[x.value] = x.label; });
      }
      // 趋势图和礼品卡都在看板载荷里，和明细并行拉 —— 两处分别是「汇总」和「逐笔」，
      // 分开请求才能各自独立失败（明细挂了也不该让整页空白）。
      const [d, p] = await Promise.all([
        api('/api/admin/dashboard'),
        api('/api/admin/points?' + pointQuery(adminPage.points)),
      ]);
      pointsRows = p.items;
      pointsTotal = p.total;
      pointsNewCount = 0;
      el.innerHTML = pointShell(d);
      renderPointTable();
      openPointsStream();
    } catch (e) { el.innerHTML = dashTabBar() + '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
  }

  function pointShell(d) {
    const sup = isSuper();
    const o = (d && d.overview) || {};
    const typeOpts = [''].concat(Object.keys(pointRefTypes))
      .map((k) => `<option value="${esc(k)}" ${pointFilter.ref_type === k ? 'selected' : ''}>${k ? esc(pointRefTypes[k]) : '全部类型'}</option>`).join('');
    const dirOpts = [['', '全部方向'], ['in', '增加'], ['out', '减少']]
      .map(([v, t]) => `<option value="${v}" ${pointFilter.direction === v ? 'selected' : ''}>${t}</option>`).join('');
    const dayOpts = [[0, '不限时间'], [7, '近 7 天'], [30, '近 30 天']]
      .map(([v, t]) => `<option value="${v}" ${String(pointFilter.days) === String(v) ? 'selected' : ''}>${t}</option>`).join('');
    return dashTabBar() + `
      <div class="section-title">📊 数据看板 · 积分</div>
      <div class="stat-grid">
        ${statCard('💹', '累计发放积分', o.points_issued)}
        ${statCard('💸', '累计消耗积分', o.points_spent)}
        ${statCard('🎁', '在架礼品', o.active_gifts)}
        ${statCard('📦', '待发货订单', o.pending_orders)}
      </div>
      <div class="card"><h2>📈 近 7 天积分趋势</h2><div id="pt-trend">${renderBarChart(d && d.daily_points)}</div></div>
      <div class="card">
        <h2>💰 积分明细（全员流水 · 实时）</h2>
        <div class="pt-toolbar">
          <input id="pt-emp" placeholder="按工号精确筛选，如 1002" value="${esc(pointFilter.emp_id)}"
                 oninput="App.setPointFilter('emp_id', this.value.trim())" />
          <select id="pt-type" onchange="App.setPointFilter('ref_type', this.value)">${typeOpts}</select>
          <select id="pt-dir" onchange="App.setPointFilter('direction', this.value)">${dirOpts}</select>
          <select id="pt-days" onchange="App.setPointFilter('days', parseInt(this.value, 10) || 0)">${dayOpts}</select>
          <button class="btn-outline" onclick="App.resetPointFilter()">重置</button>
          <span class="pt-spacer"></span>
          ${sup ? `<button class="btn-primary" style="width:auto" onclick="App.openPointAdjust()">➕ 发放 / 扣减</button>
                   <button class="btn-outline" onclick="App.openPointsDrift()">🧮 盘点校平</button>` : ''}
        </div>
        <div class="pt-live" id="pt-live"></div>
        <table class="table">
          <thead><tr><th>时间</th><th>工号</th><th>姓名</th><th>变动</th><th>类型</th>
            <th>说明</th><th>操作人</th>${sup ? '<th>操作</th>' : ''}</tr></thead>
          <tbody id="pt-body"></tbody>
        </table>
        <div id="pt-pager"></div>
      </div>
      <div class="grid grid-2">
        <div class="card"><h2>🏆 热门礼品 TOP5（兑换 / 库存）</h2>${renderGiftSpend(d && d.top_gifts)}</div>
        <div class="card"><h2>👁️ 礼品点击热度 TOP5</h2>${renderViewRank(d && d.top_gift_views)}</div>
      </div>`;
  }

  // 礼品 = 积分花掉之后的去处。带上库存和门槛分，才能回答
  // 「分发出去了、花在哪、还兑不兑得动」这个连贯的问题。
  function renderGiftSpend(top) {
    if (!top || !top.length) return '<div class="empty">暂无兑换记录</div>';
    return top.map((g, i) => `
      <div class="record-row">
        <div class="rec-note">${i + 1}. ${esc(g.icon)} ${esc(g.name)}</div>
        <div class="tag ${g.stock > 0 ? 'tag-green' : 'tag-gray'}">库存 ${g.stock}</div>
        <div class="tag">${g.points_cost} 分</div>
        <div class="tag tag-amber">兑出 ${g.c}</div>
      </div>`).join('');
  }

  function pointRowHtml(r) {
    const pos = r.points > 0;
    const cols = supRevertCell(r);
    return `<tr id="pt-row-${r.id}" class="${r._live ? 'pt-new' : ''}">
      <td style="white-space:nowrap">${fmtDate(r.created_at)}</td>
      <td>${esc(r.emp_id)}</td>
      <td>${esc(r.user_name || r.username || '—')}</td>
      <td class="${pos ? 'rec-pos' : 'rec-neg'}">${pos ? '+' : ''}${r.points}</td>
      <td><span class="tag ${pos ? 'tag-green' : 'tag-amber'}">${esc(r.ref_label || r.ref_type)}</span></td>
      <td>${esc(r.note || '—')}</td>
      <td>${esc(r.operator_label || '本人')}</td>
      ${cols}</tr>`;
  }
  // 回滚按钮的显示条件读服务端给的 revertible，不在前端再写一遍
  // 「哪些 ref_type 是人工操作」—— 那条规则由后端强制，前端照抄一份早晚会漂移。
  function supRevertCell(r) {
    if (!isSuper()) return '';
    if (!r.revertible) return '<td><span class="tag tag-gray">不可回滚</span></td>';
    return `<td><button class="btn-outline btn-danger" onclick="App.revertPoint(${r.id})">↩ 回滚</button></td>`;
  }
  function renderPointTable() {
    const body = $('#pt-body');
    if (!body) return;
    body.innerHTML = pointsRows.length
      ? pointsRows.map(pointRowHtml).join('')
      : `<tr><td colspan="8"><div class="empty">暂无流水</div></td></tr>`;
    refreshPointLiveBar();
    const pg = $('#pt-pager');
    if (pg) pg.innerHTML = renderPager(pointsTotal, adminPage.points, PAGE_SIZE, 'points');
  }
  function refreshPointLiveBar() {
    const bar = $('#pt-live');
    if (!bar) return;
    const bits = [pointsStream.giveUp ? '⚪ 实时通道已断开（重新登录后恢复）'
      : (pointsStream.ctrl ? '🟢 实时' : '🟡 连接中…')];
    if (pointsLiveCount) bits.push(`本次推送 ${pointsLiveCount} 条`);
    if (pointsNewCount) bits.push(`有 ${pointsNewCount} 条新记录，点「重置」或翻回第 1 页查看`);
    bits.push(`共 ${pointsTotal} 条`);
    bar.textContent = bits.join(' · ');
  }

  // ---- SSE 客户端 ----

  // 纯函数：把累积缓冲切成帧，返回 {frames, rest}。rest 是最后一个不完整帧
  // （TCP 分片不看你的帧边界，半帧必须留到下一块拼）。
  // 心跳（以 : 开头）本来就该忽略；它同时兼作服务端的会话复查。
  // CRLF 在**这里**归一化而不是在读取循环里：换行怎么写属于帧语法的一部分，
  // 放到调用方去处理，这个函数就只在「服务端恰好用 LF」时才正确。
  function parseSSE(buf) {
    const frames = [];
    let rest = String(buf == null ? '' : buf).replace(/\r\n/g, '\n');
    let idx;
    while ((idx = rest.indexOf('\n\n')) >= 0) {
      const block = rest.slice(0, idx);
      rest = rest.slice(idx + 2);
      let event = 'message';
      const data = [];
      block.split('\n').forEach((raw) => {
        const line = raw.replace(/\r$/, '');
        if (!line || line.charAt(0) === ':') return;   // 注释 / 心跳
        const m = /^([a-zA-Z]+):\s?(.*)$/.exec(line);
        if (!m) return;
        if (m[1] === 'event') event = m[2];
        else if (m[1] === 'data') data.push(m[2]);
      });
      if (!data.length) continue;                      // 只有 event: 没有 data 的帧不带载荷
      let payload = null;
      try { payload = JSON.parse(data.join('\n')); } catch (e) { payload = null; }
      frames.push({ event, data: payload });
    }
    return { frames, rest };
  }

  // 实时流只该在「管理端 + 看板的积分 tab」这两层同时成立时开着。
  // 判断收在一处：这条条件在开流、重连、收帧三处都要用，写三遍早晚漏一处。
  const pointsTabVisible = () => view === 'admin' && adminSection === 'dashboard' && dashTab === 'points';

  function openPointsStream() {
    if (pointsStream.ctrl || pointsStream.giveUp) return;
    if (!token || !isAdminRole(user.role)) return;
    if (!pointsTabVisible()) return;
    const ctrl = new AbortController();
    pointsStream.ctrl = ctrl;
    runPointsStream(ctrl);
  }
  async function runPointsStream(ctrl) {
    const auth = token;
    try {
      const res = await fetch('/api/admin/points/stream', {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + auth },
        signal: ctrl.signal,
      });
      if (res.status === 401 || res.status === 403) {
        // 重连多少次都是这个结果，只会变成请求风暴。停止重试。
        pointsStream.giveUp = true;
        if (res.status === 401 && token) { $('#login-error').textContent = '登录已过期，请重新登录'; logout(false); }
        return;
      }
      if (!res.ok || !res.body || !res.body.getReader) throw new Error('实时通道不可用（' + res.status + '）');
      pointsStream.retry = 0;                          // 连上了才重置退避
      refreshPointLiveBar();
      await readPointsStream(res);
    } catch (e) {
      if (e && e.name === 'AbortError') return;        // 主动关闭，不重连
    } finally {
      if (pointsStream.ctrl === ctrl) pointsStream.ctrl = null;
      refreshPointLiveBar();
    }
    schedulePointReconnect();
  }
  async function readPointsStream(res) {
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    for (;;) {
      const chunk = await reader.read();
      // stream:true 必须给：汉字是多字节的，一个 chunk 可能在半个字中间断开，
      // 不给它就会把两个字各解成一个替换字符，页面上出现 �。
      // 最后一块要给 stream:false 把解码器里残留的字节吐出来（否则它一直攒着）
      buf += dec.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      const parsed = parseSSE(buf);
      buf = parsed.rest;
      parsed.frames.forEach(dispatchPointFrame);
      if (chunk.done) break;
    }
  }
  function dispatchPointFrame(f) {
    if (f.event === 'bye') {
      // 服务端心跳里发现会话已经没了（登出/被清）。此时重连毫无意义。
      pointsStream.giveUp = true;
      return;
    }
    if (f.event !== 'message' || !f.data) return;
    if (f.data.type === 'dropped') {
      // 队列满了，中间缺帧。不能假装数据是连续的 —— 重拉当前页补齐。
      pointsNewCount = 0;
      loadAdminPoints();
      return;
    }
    if (f.data.type === 'point') applyPointFrame(f.data);
  }
  function schedulePointReconnect() {
    if (pointsStream.giveUp) return;
    if (!pointsTabVisible()) return;
    // 有上限的指数退避：没有上限的话断网一小时会攒出几千次重试
    const delay = Math.min(1000 * Math.pow(2, pointsStream.retry), 15000);
    pointsStream.retry += 1;
    pointsStream.timer = setTimeout(() => { pointsStream.timer = null; openPointsStream(); }, delay);
  }
  function stopPointsStream() {
    pointsStream.giveUp = false;
    pointsStream.retry = 0;
    if (pointsStream.timer) { clearTimeout(pointsStream.timer); pointsStream.timer = null; }
    // 待刷的趋势也要撤掉：切走之后再把图写回一个已经不在屏上的元素没意义
    if (pointsTrend.timer) { clearTimeout(pointsTrend.timer); pointsTrend.timer = null; }
    const ctrl = pointsStream.ctrl;
    pointsStream.ctrl = null;
    if (ctrl) ctrl.abort();
  }
  function normalizePoint(p) {
    return {
      id: p.id, emp_id: p.emp_id, username: p.username, user_name: p.user_name,
      points: p.points, note: p.note, ref_type: p.ref_type, ref_id: p.ref_id,
      created_at: p.created_at, revertible: p.revertible,
      ref_label: (pointRefTypes && pointRefTypes[p.ref_type]) || p.ref_type,
      operator_label: p.operator_emp_id
        ? (p.operator_emp_id + ' ' + (p.operator_name || '')).trim() : '本人',
      _live: true,
    };
  }
  function applyPointFrame(p) {
    if (!p || !p.id || typeof p.points !== 'number') return;
    pointsLiveCount += 1;
    // 翻到别的页或者有筛选时，这条不属于当前结果集 —— 硬插进去就是错的。
    // 只提示，让管理员自己决定什么时候看。
    if (adminPage.points !== 1 || pointFilterActive()) {
      pointsNewCount += 1;
      refreshPointLiveBar();
      return;
    }
    if (!pointsTabVisible()) return;
    pointsRows.forEach((r) => { r._live = false; });
    pointsRows.unshift(normalizePoint(p));
    pointsRows = pointsRows.slice(0, PAGE_SIZE);
    pointsTotal += 1;
    renderPointTable();
    // 明细更新了，趋势图也必须跟着动 —— 否则同一屏上「逐笔」是新的、
    // 「汇总」是旧的，两个数字互相打脸。
    scheduleTrendRefresh();
  }

  // 趋势图按天聚合，一帧动一格。做成防抖 + 只刷这一块：
  // 一阵流水连帧时不该每帧重算一次看板（那正是 test_frontend 里
  // 「收到普通帧绝不重拉列表」要防的那类请求风暴）。
  function scheduleTrendRefresh() {
    if (pointsTrend.timer) clearTimeout(pointsTrend.timer);
    pointsTrend.timer = setTimeout(refreshTrend, 1200);
  }
  async function refreshTrend() {
    pointsTrend.timer = null;
    if (!pointsTabVisible() || pointsTrend.busy) return;
    if (!$('#pt-trend')) return;                 // 已经切走了，别写进空气里
    pointsTrend.busy = true;
    try {
      const t = await api('/api/admin/points/trend');
      const el = $('#pt-trend');
      if (el && pointsTabVisible()) el.innerHTML = renderBarChart(t.daily_points);
    } catch (e) {
      // 刷新失败不影响已经拿到的明细；下一帧或下次进页会再拉
    } finally { pointsTrend.busy = false; }
  }

  // ---- 人工操作（仅 super_admin，按钮也只在超管登录时渲染）----

  function openPointAdjust() {
    // request_id 在**打开表单时**生成一次，整个表单生命周期复用。
    // 每次提交都新生成一个的话，「双击提交 / 超时重投」会各自成为一笔独立操作，
    // 幂等键就白设了。
    window._ptReqId = newEventId();
    openModal(`
      <h2>💰 人工发放 / 扣减</h2>
      <div class="form-grid">
        <div class="form-field"><label>工号</label><input id="pt-adj-emp" placeholder="如 1002" /></div>
        <div class="form-field"><label>变动值（正数=发放，负数=扣减）</label>
          <input id="pt-adj-points" type="number" value="50" /></div>
        <div class="form-field full"><label>原因（会作为说明显示在用户的「我的积分」里）</label>
          <input id="pt-adj-reason" maxlength="100" placeholder="如 项目上线奖励" /></div>
      </div>
      <div class="form-actions">
        <button class="btn-primary" id="pt-adj-submit" style="width:auto" onclick="App.savePointAdjust()">提交</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
  }
  async function savePointAdjust() {
    const emp_id = $('#pt-adj-emp').value.trim();
    const points = parseInt($('#pt-adj-points').value, 10);
    const reason = $('#pt-adj-reason').value.trim();
    if (!emp_id) return toast('请填工号', 'error');
    if (!points) return toast('变动值不能为 0', 'error');
    if (!reason) return toast('请填原因', 'error');
    const btn = $('#pt-adj-submit');
    if (btn && btn.disabled) return;      // 手快点两下也只是同一笔
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/admin/points/adjust', { method: 'POST', body: {
        emp_id, points, reason, request_id: window._ptReqId || newEventId(),
      } });
      closeModal();
      toast(`已${points > 0 ? '发放' : '扣减'} ${Math.abs(points)} 分，余额 ${r.balance_after}`, 'success');
      loadAdminPoints();
    } catch (e) {
      toast(e.message, 'error');
      if (btn) btn.disabled = false;
    }
  }
  async function revertPoint(rid) {
    const reason = prompt('回滚原因（可留空）：');
    if (reason === null) return;
    try {
      const r = await api('/api/admin/points/' + rid + '/revert', { method: 'POST', body: {
        reason, request_id: newEventId(),
      } });
      toast(`已回滚 ${r.points} 分，余额 ${r.balance_after}`, 'success');
      loadAdminPoints();
    } catch (e) { toast(e.message, 'error'); }
  }
  async function openPointsDrift() {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/admin/points/drift');
      let html = '<h2>🧮 账实盘点</h2>';
      html += '<p class="pt-note">余额是缓存，流水是账本。下面这些账户两者对不上 —— ' +
        '「校平」会把余额重算为流水合计（不产生积分流水，留痕在操作记录和审计日志里）。</p>';
      html += d.items.length ? d.items.map((x) => `
        <div class="record-row" id="drift-${esc(x.emp_id)}">
          <div class="rec-note">${esc(x.emp_id)} ${esc(x.user_name || '（无此用户）')} ·
            余额 <b>${x.balance}</b> · 流水合计 <b>${x.records_sum}</b></div>
          <div class="tag tag-amber">差额 ${x.drift > 0 ? '+' : ''}${x.drift}</div>
          <button class="btn-outline btn-danger" onclick="App.confirmReconcile('${esc(x.emp_id)}')">🧮 校平</button>
        </div>`).join('') : '<div class="empty">所有账户都对得上 ✅</div>';
      html += '<div class="form-actions" style="margin-top:14px"><button class="btn-outline" onclick="App.closeModal()">关闭</button></div>';
      openModal(html);
    } catch (e) { openModal('<div class="empty">加载失败：' + esc(e.message) + '</div>'); }
  }
  function confirmReconcile(emp) {
    window._ptRecReqId = newEventId();
    openModal(`
      <h2>🧮 校平 ${esc(emp)}</h2>
      <p class="pt-note">校平只改余额缓存，<b>不会</b>补发积分。如果这个账户还欠着一笔该发的分，
      请先用「发放」补上再校平，否则这笔加分就永久丢了。</p>
      <div class="form-grid"><div class="form-field full"><label>原因</label>
        <input id="pt-rec-reason" maxlength="100" placeholder="如 历史数据修正" /></div></div>
      <div class="form-actions">
        <button class="btn-primary" style="width:auto" onclick="App.doReconcile('${esc(emp)}')">确认校平</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
  }
  async function doReconcile(emp) {
    try {
      const r = await api('/api/admin/points/reconcile', { method: 'POST', body: {
        emp_id: emp, reason: $('#pt-rec-reason').value.trim(),
        request_id: window._ptRecReqId || newEventId(),
      } });
      closeModal();
      toast(r.delta === 0
        ? `账已平（余额 ${r.balance_after}），无需修正`
        : `已校平：余额 ${r.balance_before} → ${r.balance_after}（差额 ${r.delta > 0 ? '+' : ''}${r.delta}）`,
        'success');
      loadAdminPoints();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 分页 + 审计日志 -----
  function renderPager(total, page, size, key) {
    const pages = Math.max(1, Math.ceil(total / size));
    if (pages <= 1) return '';
    return `<div class="pager">
      <button class="btn-outline" ${page <= 1 ? 'disabled' : ''} onclick="App.goPage('${key}', ${page - 1})">‹ 上一页</button>
      <span class="pager-info">第 ${page} / ${pages} 页 · 共 ${total} 条</span>
      <button class="btn-outline" ${page >= pages ? 'disabled' : ''} onclick="App.goPage('${key}', ${page + 1})">下一页 ›</button>
    </div>`;
  }
  function goPage(key, page) {
    adminPage[key] = page;
    if (key === 'courses') loadAdminCourses();
    else if (key === 'activities') loadAdminActivities();
    else if (key === 'gifts') loadAdminGifts();
    else if (key === 'orders') loadAdminOrders();
    else if (key === 'points') loadAdminPoints();
    else if (key === 'audit') loadAdminAudit();
  }
  async function loadAdminAudit() {
    const el = $('#sec-audit');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const d = await api('/api/admin/audit-logs?page=' + adminPage.audit + '&size=' + PAGE_SIZE);
      let html = '<div class="section-title">📜 审计日志</div><div class="card"><table class="table">';
      html += '<thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th></tr></thead><tbody>';
      html += d.items.map((a) => `
        <tr>
          <td style="white-space:nowrap">${fmtDate(a.created_at)}</td>
          <td>${esc(a.user_name || a.emp_id)}</td>
          <td><span class="tag">${esc(a.action)}</span></td>
          <td>${esc(a.target_type)}${a.target_id ? ' #' + a.target_id : ''}</td>
          <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(a.detail || '')}">${esc(a.detail || '')}</td>
        </tr>`).join('');
      html += '</tbody></table>' + renderPager(d.total, d.page, d.size, 'audit') + '</div>';
      el.innerHTML = html;
    } catch (e) { el.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
  }

  // ----- 题目管理 -----
  async function openQuestionManager(cid) {
    openModal('<div class="empty">加载中…</div>');
    try {
      const d = await api('/api/admin/courses/' + cid + '/questions');
      window._questionCourse = cid;
      window._questionsCache = d.questions;
      let html = '<h2>📝 题目管理</h2>';
      html += `<button class="btn-primary" style="width:auto;padding:10px 18px" onclick="App.openQuestionForm(0)">➕ 新增题目</button>`;
      html += '<div style="margin-top:14px">';
      html += d.questions.length ? d.questions.map((q) => `
        <div class="quiz-row">
          <div class="qr-body"><div class="qr-title">${esc(q.question)}</div>
          <div class="qr-opts">A. ${esc(q.option_a)} · B. ${esc(q.option_b)} · C. ${esc(q.option_c)} · D. ${esc(q.option_d)}</div>
          <div class="qr-answer">✅ 正确答案：${esc(q.answer)}</div></div>
          <div class="qr-actions">
            <button class="btn-outline" onclick="App.openQuestionForm(${q.id})">✏️</button>
            <button class="btn-outline btn-danger" onclick="App.deleteQuestion(${q.id})">🗑️</button>
          </div>
        </div>`).join('') : '<div class="empty">暂无题目</div>';
      html += '</div>';
      html += '<div class="form-actions" style="margin-top:14px"><button class="btn-outline" onclick="App.closeModal()">关闭</button></div>';
      openModal(html);
    } catch (e) { openModal('<div class="empty">' + esc(e.message) + '</div>'); }
  }
  function openQuestionForm(qid) {
    const cur = qid ? (window._questionsCache || []).find((q) => q.id === qid) : null;
    openModal(`
      <h2>${qid ? '✏️ 编辑题目' : '➕ 新增题目'}</h2>
      <div class="form-grid">
        <div class="form-field full"><label>题目</label><textarea id="qq-question" rows="2">${esc(cur?.question || '')}</textarea></div>
        <div class="form-field"><label>选项 A</label><input id="qq-a" value="${esc(cur?.option_a || '')}" /></div>
        <div class="form-field"><label>选项 B</label><input id="qq-b" value="${esc(cur?.option_b || '')}" /></div>
        <div class="form-field"><label>选项 C</label><input id="qq-c" value="${esc(cur?.option_c || '')}" /></div>
        <div class="form-field"><label>选项 D</label><input id="qq-d" value="${esc(cur?.option_d || '')}" /></div>
        <div class="form-field"><label>正确答案</label>
          <select id="qq-answer"><option>A</option><option>B</option><option>C</option><option>D</option></select></div>
      </div>
      <div class="form-actions">
        <button class="btn-primary" style="width:auto" onclick="App.saveQuestion(${qid || 0})">保存</button>
        <button class="btn-outline" onclick="App.closeModal()">取消</button>
      </div>`);
    if (cur) $('#qq-answer').value = cur.answer;
  }
  async function saveQuestion(qid) {
    const cid = window._questionCourse;
    const body = {
      question: $('#qq-question').value, option_a: $('#qq-a').value, option_b: $('#qq-b').value,
      option_c: $('#qq-c').value, option_d: $('#qq-d').value, answer: $('#qq-answer').value,
    };
    try {
      await api(qid ? '/api/admin/questions/' + qid : '/api/admin/courses/' + cid + '/questions',
        { method: qid ? 'PUT' : 'POST', body });
      toast('已保存', 'success');
      openQuestionManager(cid);
    } catch (e) { toast(e.message, 'error'); }
  }
  async function deleteQuestion(qid) {
    if (!confirm('确定删除这道题？')) return;
    try {
      await api('/api/admin/questions/' + qid, { method: 'DELETE' });
      toast('已删除', 'success');
      openQuestionManager(window._questionCourse);
    } catch (e) { toast(e.message, 'error'); }
  }

  // ----- 通用：上架/下架 -----
  async function toggleStatus(type, id, status) {
    try {
      await api(`/api/admin/${type}/${id}/status`, { method: 'POST', body: { status } });
      toast(status === 'active' ? '已上架' : '已下架', 'success');
      if (type === 'courses') loadAdminCourses();
      else if (type === 'activities') loadAdminActivities();
      else loadAdminGifts();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------- 搜索 ----------
  function onSearchInput() {
    clearTimeout(searchTimer);
    const q = $('#search-input').value.trim();
    if (!q) { hideSearch(); return; }
    searchTimer = setTimeout(async () => {
      try {
        const d = await api('/api/search?q=' + encodeURIComponent(q));
        renderSearchResults(d, q);
      } catch (e) { hideSearch(); }
    }, 200);
  }
  function renderSearchResults(d, q) {
    const box = $('#search-results');
    const items = [];
    d.courses.forEach((c) => items.push({ type: 'course', id: c.id, icon: c.emoji, title: c.title, meta: c.category + ' · 课程' }));
    d.gifts.forEach((g) => items.push({ type: 'gift', id: g.id, icon: g.icon, title: g.name, meta: g.category + ' · ' + g.points_cost + ' 积分' }));
    lastSearchResults = items;
    if (!items.length) { box.innerHTML = `<div class="search-empty">未找到「${esc(q)}」相关结果</div>`; box.classList.remove('hidden'); return; }
    box.innerHTML = items.map((it, i) => `
      <div class="search-item" onclick="App.goSearchResult(${i})">
        <span class="si-icon">${esc(it.icon)}</span>
        <div><div>${esc(it.title)}</div><div class="si-meta">${esc(it.meta)}</div></div>
      </div>`).join('');
    box.classList.remove('hidden');
  }
  function hideSearch() { $('#search-results').classList.add('hidden'); }
  function goSearchResult(idx) {
    const it = lastSearchResults[idx];
    hideSearch();
    $('#search-input').value = '';
    if (!it) return;
    if (it.type === 'course') {
      courseQuery = it.title;      // 只显示这一门课程
      activateTab('courses');
    } else {
      setTab('gifts');
      setTimeout(() => highlight('#gift-' + it.id), 300);
    }
  }
  function highlight(sel) {
    const el = $(sel);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.boxShadow = '0 0 0 3px var(--primary)';
    setTimeout(() => el.style.boxShadow = '', 2000);
  }

  // ---------- 事件绑定 ----------
  function bind() {
    $('#login-btn').addEventListener('click', login);
    $('#login-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
    $('#logout-btn').addEventListener('click', () => logout(true));
    $('#admin-toggle').addEventListener('click', () => setView(view === 'admin' ? 'user' : 'admin'));
    $('#search-input').addEventListener('input', onSearchInput);
    $('#search-input').addEventListener('blur', () => setTimeout(hideSearch, 200));
    $$('#user-nav .tab').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
    $$('.admin-nav').forEach((b) => b.addEventListener('click', () => setAdminSection(b.dataset.section)));
    $('#modal-overlay').addEventListener('click', (e) => { if (e.target === $('#modal-overlay')) closeModal(); });
    $('#bell-btn').addEventListener('click', (e) => { e.stopPropagation(); toggleBell(); });
    document.addEventListener('click', () => { const dd = $('#bell-dropdown'); if (!dd.classList.contains('hidden')) dd.classList.add('hidden'); });
  }

  // 暴露给 onclick 使用
  window.App = {
    enrollCourse: (id) => doAction(`/api/user/courses/${id}/enroll`, '报名成功'),
    completeCourse: (id) => doAction(`/api/user/courses/${id}/complete`, '完成课程，积分已到账'),
    participate: (id) => doAction(`/api/user/activities/${id}/participate`, '参与成功，积分已到账'),
    readAnnouncement: (id) => doAction(`/api/user/announcements/${id}/read`, '阅读成功，积分已到账'),
    redeemGift, confirmRedeem, cancelOrder, refundOrder, reconcileOrders, openStockForm, saveStock,
    goSearchResult,
    onCourseSearch, clearCourseSearch, onGiftSort,
    openCourseForm, saveCourse,
    openActivityForm, saveActivity, saveCarousel,
    openGiftForm, saveGift,
    toggleStatus, shipOrder, closeModal, goPage,
    openCourseDetail, openGiftDetail, openAnnouncementDetail, openActivityDetail,
    enrollCourseDetail: (id) => { closeModal(); return doAction(`/api/user/courses/${id}/enroll`, '报名成功'); },
    completeCourseDetail: (id) => { closeModal(); return doAction(`/api/user/courses/${id}/complete`, '完成课程，积分已到账'); },
    redeemGiftDetail: (id) => { closeModal(); return redeemGift(id); },
    readAnnouncementDetail: (id) => { closeModal(); return doAction(`/api/user/announcements/${id}/read`, '阅读成功，积分已到账'); },
    participateDetail: (id) => { closeModal(); return doAction(`/api/user/activities/${id}/participate`, '参与成功，积分已到账'); },
    openQuiz, submitQuiz,
    openQuestionManager, openQuestionForm, saveQuestion, deleteQuestion,
    setPointFilter, resetPointFilter,
    setDashTab,
    // 侧栏菜单的入口。挂出来是为了单测：node 里的假 DOM 把 querySelectorAll
    // 桩成了空数组，侧栏按钮点不动，只能直接调这个函数验证「离开看板要断流」。
    setAdminSection,
    openPointAdjust, savePointAdjust, revertPoint,
    openPointsDrift, confirmReconcile, doReconcile,
    // 纯解析器挂出来是为了单测：帧边界是「点一下才知道」的那类逻辑，
    // 而 node 里没有真的网络分片，只能直接喂缓冲。
    parseSSE,
  };

  // ---------- 启动 ----------
  async function bootstrap() {
    bind();
    if (token) {
      try {
        const d = await api('/api/auth/me');
        user = d.user;
        enterApp();
        return;
      } catch (e) { /* token 失效 */ }
    }
    showLogin();
  }
  bootstrap();
})();
