(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const form = $('#login-form'), username = $('#username'), password = $('#password');
  const confirmation = $('#confirm-password'), submit = $('#login-submit'), error = $('#login-error');
  const tabs = [...document.querySelectorAll('[data-mode]')];
  let mode = 'login', busy = false;
  function clearError() {
    error.hidden = true;
    for (const field of [username, password, confirmation]) field.removeAttribute('aria-invalid');
  }
  function showError(message, field) {
    error.textContent = message; error.hidden = false;
    if (field) { field.setAttribute('aria-invalid', 'true'); field.focus(); } else error.focus();
  }
  function resetVisibility() {
    for (const button of document.querySelectorAll('[data-password]')) {
      const input = document.getElementById(button.dataset.password);
      input.type = 'password'; button.setAttribute('aria-pressed', 'false');
      button.setAttribute('aria-label', input === confirmation ? '显示确认密码' : '显示密码');
      button.querySelector('use').setAttribute('href', '#auth-eye');
    }
  }
  function setMode(next) {
    if (busy) return;
    mode = next === 'register' ? 'register' : 'login';
    const registering = mode === 'register';
    document.title = (registering ? '注册' : '登录') + ' · 知序';
    $('#auth-title').textContent = registering ? '开启你的知序' : '欢迎回来';
    $('#auth-description').textContent = registering ? '创建一个账号，让想法与对话有自己的空间。' : '登录知序，继续你的对话与探索。';
    $('#submit-label').textContent = registering ? '创建账号并开始' : '登录';
    $('#switch-description').textContent = registering ? '已经有账号了？' : '第一次来到知序？';
    $('#switch-mode').textContent = registering ? '去登录' : '创建账号';
    $('#switch-mode').setAttribute('href', registering ? '#login' : '#register');
    for (const element of document.querySelectorAll('.register-only')) element.hidden = !registering;
    password.minLength = registering ? 12 : 1;
    password.autocomplete = registering ? 'new-password' : 'current-password';
    password.placeholder = registering ? '设置你的密码' : '输入你的密码';
    confirmation.disabled = !registering; confirmation.required = registering;
    password.value = ''; confirmation.value = ''; confirmation.setCustomValidity('');
    clearError(); resetVisibility();
    for (const tab of tabs) {
      const active = tab.dataset.mode === mode;
      tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1;
    }
    $('#auth-panel').setAttribute('aria-labelledby', mode + '-tab');
  }
  function navigate(next) { window.location.hash = next; }
  tabs.forEach(tab => {
    tab.addEventListener('click', () => navigate(tab.dataset.mode));
    tab.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) && !busy) {
        event.preventDefault();
        const next = event.key === 'Home' ? 'login' : event.key === 'End' ? 'register' : mode === 'login' ? 'register' : 'login';
        navigate(next); document.getElementById(next + '-tab').focus();
      }
    });
  });
  window.addEventListener('hashchange', () => setMode(window.location.hash.slice(1)));
  setMode(window.location.hash.slice(1));
  for (const button of document.querySelectorAll('[data-password]')) {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.password);
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password'; button.setAttribute('aria-pressed', String(visible));
      button.setAttribute('aria-label', (visible ? '隐藏' : '显示') + (input === confirmation ? '确认密码' : '密码'));
      button.querySelector('use').setAttribute('href', visible ? '#auth-eye-off' : '#auth-eye');
    });
  }
  function validateConfirmation() {
    confirmation.setCustomValidity(mode === 'register' && confirmation.value && confirmation.value !== password.value ? '两次输入的密码不一致。' : '');
  }
  for (const input of [password, confirmation]) input.addEventListener('input', validateConfirmation);
  username.addEventListener('input', clearError);
  fetch('/api/auth/me', { cache: 'no-store', credentials: 'same-origin' })
    .then(response => { if (response.ok) window.location.replace('/'); }).catch(() => {});
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    clearError(); validateConfirmation();
    if (!form.reportValidity()) return;
    const registering = mode === 'register';
    const body = { username: username.value.trim(), password: password.value };
    if (registering) body.confirm_password = confirmation.value;
    busy = true; submit.disabled = true; form.setAttribute('aria-busy', 'true');
    tabs.forEach(tab => { tab.disabled = true; }); $('#switch-mode').setAttribute('aria-disabled', 'true');
    $('#submit-label').textContent = registering ? '正在创建账号…' : '正在登录…';
    try {
      const response = await fetch('/api/auth/' + mode, {
        method: 'POST', credentials: 'same-origin', signal: AbortSignal.timeout(20000),
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        let message = typeof data.detail === 'string' ? data.detail : '请检查填写的信息。';
        if (Array.isArray(data.detail) && data.detail.length) {
          const issue = data.detail[0], field = issue.loc?.[issue.loc.length - 1];
          message = field === 'username' ? '用户名需为 3–64 位字母、数字、下划线、点或短横线。'
            : field === 'password' || field === 'confirm_password' ? '注册密码需为 12–128 个字符。'
            : '两次输入的密码不一致。';
        }
        showError(message, response.status === 409 ? username : undefined); return;
      }
      password.value = ''; confirmation.value = '';
      $('#submit-label').textContent = registering ? '注册成功，正在进入…' : '登录成功，正在进入…';
      window.location.replace('/');
    } catch (exception) {
      showError(exception.name === 'TimeoutError' ? '请求超时，请稍后重试。' : exception instanceof TypeError ? '无法连接服务，请稍后重试。' : '暂时无法完成请求，请稍后重试。');
    } finally {
      busy = false; submit.disabled = false; form.removeAttribute('aria-busy');
      tabs.forEach(tab => { tab.disabled = false; }); $('#switch-mode').removeAttribute('aria-disabled');
      if (!error.hidden) $('#submit-label').textContent = registering ? '创建账号并开始' : '登录';
      setTimeout(() => { if (mode !== (window.location.hash === '#register' ? 'register' : 'login')) setMode(window.location.hash.slice(1)); }, 0);
    }
  });
  $('#switch-mode').addEventListener('click', event => { if (busy) event.preventDefault(); });
})();
