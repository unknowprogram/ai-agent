(() => {
  'use strict';
  const originalFetch = window.fetch.bind(window);
  let session;
  function clearCache() {
    try {
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith('zhixu.conversations.')) localStorage.removeItem(key);
      }
    } catch { /* 本地存储不可用时无需清理。 */ }
  }
  const ready = originalFetch('/api/auth/me', { cache: 'no-store', credentials: 'same-origin' })
    .then(async response => {
      if (response.status === 401) {
        clearCache(); window.location.replace('/login');
        throw new Error('请先登录。');
      }
      if (!response.ok) throw new Error('暂时无法验证登录状态，请稍后刷新页面。');
      session = await response.json();
      // 匿名时代的缓存不自动导入任何新账号。
      try { localStorage.removeItem('zhixu.conversations.v1'); } catch { /* 可选缓存 */ }
      document.querySelector('#current-user').textContent = session.user.username;
      return session.user;
    });
  async function authenticatedFetch(url, options = {}) {
    await ready;
    const headers = new Headers(options.headers);
    headers.set('X-User-Id', session.user.id);
    if (!['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase())) {
      headers.set('X-CSRF-Token', session.csrf_token);
    }
    const response = await originalFetch(url, { ...options, headers, credentials: 'same-origin' });
    if (response.status === 401) {
      clearCache(); window.location.replace('/login');
      throw new Error('登录已失效，请重新登录。');
    }
    return response;
  }
  document.querySelector('#logout').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await authenticatedFetch('/api/auth/logout', { method: 'POST' });
      if (!response.ok) throw new Error('退出失败，请稍后重试。');
      clearCache(); window.location.replace('/login');
    } catch (error) { alert(error.message); button.disabled = false; }
  });
  // 浏览器后退缓存、其他标签页切换账号时刷新身份，避免旧内容继续显示。
  async function checkIdentity() {
    if (!session) return;
    try {
      const response = await originalFetch('/api/auth/me', { cache: 'no-store', credentials: 'same-origin' });
      if (response.status === 401) { clearCache(); window.location.replace('/login'); }
      else if (response.ok && (await response.json()).user.id !== session.user.id) window.location.reload();
    } catch { /* 临时网络错误交由下一次请求展示。 */ }
  }
  window.addEventListener('pageshow', event => { if (event.persisted) checkIdentity(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkIdentity(); });
  window.ZhixuAuth = { ready, fetch: authenticatedFetch };
  ready.catch(error => {
    if (error.message === '请先登录。') return;
    const message = document.createElement('p');
    message.className = 'auth-startup-error'; message.textContent = error.message;
    document.querySelector('.app-shell').replaceWith(message);
  });
})();
