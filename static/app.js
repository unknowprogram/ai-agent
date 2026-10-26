(() => {
  'use strict';

  const $ = (selector) => document.querySelector(selector);
  const STORAGE_KEY = 'zhixu.conversations.v1';
  const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
  const elements = {
    form: $('#chat-form'), input: $('#message'), send: $('#send'),
    welcome: $('#welcome'), messages: $('#messages'), scroll: $('#conversation-scroll'),
    sessions: $('#session-list'), newChat: $('#new-chat'), error: $('#error-banner'),
    errorText: $('#error-text'), sidebar: $('#sidebar'), backdrop: $('#sidebar-backdrop'),
    menu: $('#menu-toggle'),
  };
  const state = { sessions: [], activeId: null, pending: null, loading: false, preview: null, uploading: false, newFiles: [] };
  let toastTimer;
  let retryMessage = '';

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function safeURL(value) {
    if (typeof value !== 'string') return null;
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) ? url : null;
    } catch { return null; }
  }

  function normalizeSources(sources) {
    if (!Array.isArray(sources)) return [];
    return sources.filter((source) => source && typeof source.title === 'string' && safeURL(source.url))
      .slice(0, 15).map((source) => ({ title: source.title, url: source.url, content: typeof source.content === 'string' ? source.content : '' }));
  }

  function normalizeFiles(files) {
    return Array.isArray(files) ? files.filter(file => file && typeof file.file_id === 'string' && typeof file.filename === 'string').slice(0, 5) : [];
  }

  function normalizeDocumentSources(sources) {
    return Array.isArray(sources) ? sources.filter(source => source && typeof source.file_id === 'string' && typeof source.filename === 'string' && typeof source.content === 'string').slice(0, 12) : [];
  }

  function normalizeDocumentSummaries(summaries) {
    if (!Array.isArray(summaries)) return [];
    return summaries.filter(item => item && typeof item.file_id === 'string' && typeof item.filename === 'string'
      && Number.isInteger(item.coverage?.total_chunks) && Number.isInteger(item.coverage?.processed_chunks))
      .slice(0, 3).map(item => ({ file_id: item.file_id, filename: item.filename, query: item.query, coverage: item.coverage }));
  }

  function stagedFiles() { return activeSession()?.stagedFiles || state.newFiles; }

  function renderAttachments() {
    const container = $('#attachments');
    container.replaceChildren();
    const items = stagedFiles();
    container.hidden = items.length === 0;
    for (const file of items) {
      const card = node('div', `attachment${file.error ? ' error' : ''}`);
      const info = node('div', 'attachment-info');
      const filename = node('div', 'attachment-name', file.filename);
      filename.title = file.filename;
      const warnings = Array.isArray(file.warnings) ? file.warnings : [];
      const label = file.error || (file.uploading ? '正在上传并识别……' : `${(file.char_count || 0).toLocaleString()} 字 · 待发送${warnings.length ? ' · 有识别提示' : ''}`);
      info.append(filename, node('div', 'attachment-meta', label));
      card.title = warnings.join('\n') || file.preview || file.filename;
      card.append(icon('book'), info);
      if (!file.bound) {
        const remove = node('button', 'icon-button'); remove.type = 'button';
        remove.title = '移除此待发送附件'; remove.setAttribute('aria-label', `移除 ${file.filename}`);
        remove.disabled = Boolean(state.pending) || state.uploading;
        remove.append(icon('close'));
        remove.addEventListener('click', () => { const files = stagedFiles(); files.splice(files.indexOf(file), 1); persist(); updateInput(); });
        card.append(remove);
      }
      container.append(card);
    }
  }

  async function uploadFiles(files) {
    if (state.pending || state.loading || state.uploading) return;
    const queue = stagedFiles();
    const bound = normalizeFiles(activeSession()?.files);
    if (files.length + queue.length + bound.length > 5) { toast('每个会话最多 5 个文件，请先移除待发送附件。'); return; }
    state.uploading = true;
    $('#upload-status').hidden = false;
    $('#upload-status').textContent = '正在上传并识别文件，请稍候……';
    renderSidebar(); updateInput();
    for (const file of files) {
      const entry = { filename: file.name, uploading: true };
      queue.push(entry); renderAttachments();
      try {
        if (!/\.(txt|md|pdf|docx)$/i.test(file.name)) throw new Error('仅支持 TXT、MD、PDF、DOCX。');
        if (file.size > MAX_UPLOAD_BYTES) throw new Error('文件不能超过 20 MB。');
        const body = new FormData(); body.append('file', file);
        const response = await fetch('/api/files', { method: 'POST', body, signal: AbortSignal.timeout(90000) });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '文件上传失败。');
        if (typeof data.file_id !== 'string' || !data.file_id || typeof data.filename !== 'string') throw new Error('服务未返回有效文件信息，请移除后重试。');
        Object.assign(entry, data, { uploading: false });
      } catch (error) {
        entry.uploading = false;
        entry.error = error.name === 'TimeoutError' ? '上传超时，请移除后重新选择。' : (error instanceof TypeError ? '网络连接失败，请移除后重试。' : error.message);
      }
      renderAttachments(); persist();
    }
    state.uploading = false;
    $('#upload-status').textContent = queue.some(file => file.error) ? '部分文件识别失败，请移除失败附件后重试。' : '文件已添加，将随这条问题一起发送。';
    renderSidebar(); updateInput(); elements.input.focus();
  }

  function restore() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (!saved || !Array.isArray(saved.sessions)) return;
      const ids = new Set();
      state.sessions = saved.sessions.filter((session) => {
        if (!session || typeof session.id !== 'string' || ids.has(session.id) || !Array.isArray(session.messages)) return false;
        ids.add(session.id);
        return true;
      }).slice(0, 20).map((session) => ({
        id: session.id,
        conversationId: typeof session.conversationId === 'string' ? session.conversationId : null,
        files: normalizeFiles(session.files),
        stagedFiles: normalizeFiles(session.stagedFiles),
        title: typeof session.title === 'string' ? session.title.slice(0, 80) : '未命名对话',
        messages: session.messages.filter((message) => message && ['user', 'assistant'].includes(message.role) && typeof message.content === 'string')
          .slice(-80).map((message) => ({ ...message, content: message.content.slice(0, 64000), sources: normalizeSources(message.sources), files: normalizeFiles(message.files) })),
      }));
      state.activeId = state.sessions.some((session) => session.id === saved.activeId) ? saved.activeId : null;
      state.newFiles = normalizeFiles(saved.newFiles);
    } catch { /* 浏览器不允许本地存储时，仍可在当前页面正常对话。 */ }
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        sessions: state.sessions.filter((session) => session.messages.length || session.stagedFiles?.length).slice(0, 20),
        activeId: state.activeId,
        newFiles: normalizeFiles(state.newFiles),
      }));
    } catch { toast('当前对话暂时无法保存到浏览器，仍可继续聊天。'); }
  }

  function activeSession() { return state.sessions.find((session) => session.id === state.activeId); }

  async function fetchJSON(url, options) {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(20000) });
    if (response.status === 204) return null;
    let data;
    try { data = await response.json(); } catch { throw new Error('服务返回了无法读取的响应。'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（HTTP ${response.status}）。`);
    return data;
  }

  async function loadSession(session) {
    if (!session?.conversationId) return;
    const data = await fetchJSON(`/api/conversations/${encodeURIComponent(session.conversationId)}`);
    const cached = new Map();
    for (const message of session.messages) {
      const key = `${message.role}\0${message.content}`;
      if (!cached.has(key)) cached.set(key, []);
      cached.get(key).push(message);
    }
    session.title = data.title;
    session.files = normalizeFiles(data.files);
    session.stagedFiles = normalizeFiles(session.stagedFiles).filter(file => !session.files.some(bound => bound.file_id === file.file_id));
    session.messages = data.messages.filter((message) => ['user', 'assistant'].includes(message.role)).map((message) => {
      const previous = cached.get(`${message.role}\0${message.content}`)?.shift();
      return { ...previous, role: message.role, content: message.content, databaseId: message.id, sources: normalizeSources(previous?.sources) };
    });
  }

  async function syncConversations() {
    if (state.pending || state.loading || state.uploading) return;
    state.loading = true; renderSidebar(); updateInput();
    try {
      const conversations = await fetchJSON('/api/conversations?limit=20');
      const cached = new Map(state.sessions.filter((session) => session.conversationId).map((session) => [session.conversationId, session]));
      const legacy = state.sessions.filter((session) => !session.conversationId && (session.messages.length || session.stagedFiles?.length));
      state.sessions = [...conversations.map((conversation) => ({
        ...(cached.get(conversation.id) || { id: conversation.id, messages: [] }),
        conversationId: conversation.id, title: conversation.title,
      })), ...legacy].slice(0, 20);
      if (!activeSession()) state.activeId = null;
      await loadSession(activeSession());
      state.preview = null;
      elements.error.hidden = true;
      persist();
    } catch (error) {
      showError(`无法读取会话记忆：${error instanceof TypeError ? '请检查服务连接。' : error.message}`, 'sync');
    } finally {
      state.loading = false;
      renderSidebar(); renderConversation(); updateInput();
    }
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3000);
  }

  function toggleSidebar(open) {
    elements.sidebar.classList.toggle('open', open);
    elements.backdrop.hidden = !open;
    elements.menu.setAttribute('aria-expanded', String(open));
  }

  function renderSidebar() {
    elements.sessions.replaceChildren();
    $('#session-count').textContent = state.sessions.length;
    $('#empty-history').hidden = state.sessions.length > 0;
    elements.newChat.disabled = Boolean(state.pending) || state.loading || state.uploading;
    for (const session of state.sessions) {
      const row = node('div', `session-row${session.id === state.activeId ? ' active' : ''}`);
      const select = node('button', 'session-select');
      select.type = 'button';
      select.title = session.title;
      select.disabled = Boolean(state.pending) || state.loading || state.uploading;
      if (session.id === state.activeId) select.setAttribute('aria-current', 'page');
      select.append(icon('chat'), node('span', '', session.title));
      select.addEventListener('click', async () => {
        retryMessage = '';
        state.preview = null;
        state.activeId = session.id;
        elements.input.value = '';
        elements.error.hidden = true;
        state.loading = true;
        renderSidebar(); renderConversation(); updateInput(); toggleSidebar(false);
        try { await loadSession(session); persist(); }
        catch (error) { showError(`无法读取会话记忆：${error.message}`, 'sync'); }
        finally { state.loading = false; renderSidebar(); renderConversation(); updateInput(); }
      });
      const remove = node('button', 'session-delete');
      remove.type = 'button';
      remove.setAttribute('aria-label', `删除对话：${session.title}`);
      remove.title = '删除这段对话';
      remove.disabled = Boolean(state.pending) || state.loading || state.uploading;
      remove.append(icon('trash'));
      remove.addEventListener('click', async () => {
        if (!window.confirm('确定删除这段对话及其历史消息吗？删除后无法恢复。')) return;
        state.loading = true; renderSidebar(); updateInput();
        try {
          if (session.conversationId) await fetchJSON(`/api/conversations/${encodeURIComponent(session.conversationId)}`, { method: 'DELETE' });
          state.sessions = state.sessions.filter((item) => item.id !== session.id);
          if (state.activeId === session.id) { state.activeId = null; state.preview = null; elements.input.value = ''; }
          elements.error.hidden = true;
          persist();
        } catch (error) { showError(`删除失败：${error.message}`, 'sync'); }
        finally { state.loading = false; renderSidebar(); renderConversation(); updateInput(); }
      });
      row.append(select, remove);
      elements.sessions.append(row);
    }
  }

  async function copy(text, button) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const field = node('textarea');
        field.value = text;
        field.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.append(field);
        field.select();
        const copied = document.execCommand('copy');
        field.remove();
        if (!copied) throw new Error('Copy failed');
      }
      if (button) {
        const original = [...button.childNodes].map((child) => child.cloneNode(true));
        button.replaceChildren(icon('check'));
        setTimeout(() => { if (button.isConnected) button.replaceChildren(...original); }, 1600);
      }
      toast('已复制');
    } catch { toast('复制失败，请选中文字后复制。'); }
  }

  // 通过 DOM 创建富文本，模型输出中的 HTML 不会被执行。
  function inline(parent, text, depth = 0) {
    if (depth > 4) { parent.append(document.createTextNode(text)); return; }
    const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\)|https?:\/\/[^\s<>]+)/g;
    let position = 0;
    for (const match of text.matchAll(tokens)) {
      parent.append(document.createTextNode(text.slice(position, match.index)));
      const value = match[0];
      if (value.startsWith('`')) parent.append(node('code', '', value.slice(1, -1)));
      else if (value.startsWith('**')) {
        const strong = node('strong'); inline(strong, value.slice(2, -2), depth + 1); parent.append(strong);
      } else {
        const markdown = value.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const href = markdown ? markdown[2] : value.replace(/[.,;!?，。；！？）]+$/u, '');
        const label = markdown ? markdown[1] : href;
        const url = safeURL(href);
        if (url) {
          const link = node('a', '', label);
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          parent.append(link);
        } else parent.append(document.createTextNode(label));
        if (!markdown && href.length < value.length) parent.append(document.createTextNode(value.slice(href.length)));
      }
      position = match.index + value.length;
    }
    parent.append(document.createTextNode(text.slice(position)));
  }

  function markdown(content) {
    const root = node('div', 'prose');
    const lines = content.replace(/\r\n?/g, '\n').split('\n');
    const listPattern = /^\s*(?:[-*+] |\d+[.)] )/;
    const tableDivider = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
    const blockStart = (line) => /^(?:\s*```|#{1,6}\s|>\s?|\s*(?:[-*+] |\d+[.)] )|\s*(?:---+|\*\*\*+)\s*$)/.test(line);
    const cells = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (/^\s*```/.test(line)) {
        const language = line.trim().slice(3).trim() || '代码';
        const body = [];
        index++;
        while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
        if (index < lines.length) index++;
        const block = node('div', 'code-block');
        const bar = node('div', 'code-toolbar');
        const copyCode = node('button', '', '复制代码');
        copyCode.type = 'button';
        copyCode.addEventListener('click', () => copy(body.join('\n')));
        bar.append(node('span', '', language), copyCode);
        const pre = node('pre'); pre.append(node('code', '', body.join('\n')));
        block.append(bar, pre); root.append(block); continue;
      }
      if (index + 1 < lines.length && line.includes('|') && tableDivider.test(lines[index + 1])) {
        const container = node('div', 'table-scroll');
        const table = node('table');
        const head = node('thead'); const row = node('tr');
        for (const cell of cells(line)) { const th = node('th'); inline(th, cell); row.append(th); }
        head.append(row); table.append(head); index += 2;
        const body = node('tbody');
        while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
          const tr = node('tr');
          for (const cell of cells(lines[index++])) { const td = node('td'); inline(td, cell); tr.append(td); }
          body.append(tr);
        }
        table.append(body); container.append(table); root.append(container); continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const h = node(`h${Math.min(heading[1].length, 4)}`); inline(h, heading[2]); root.append(h); index++; continue;
      }
      if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) { root.append(node('hr')); index++; continue; }
      if (/^>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ''));
        const block = node('blockquote'); inline(block, quote.join('\n')); root.append(block); continue;
      }
      if (listPattern.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const list = node(ordered ? 'ol' : 'ul');
        while (index < lines.length && listPattern.test(lines[index]) && /^\s*\d/.test(lines[index]) === ordered) {
          const item = node('li'); inline(item, lines[index++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, '')); list.append(item);
        }
        root.append(list); continue;
      }
      const paragraph = [line]; index++;
      while (index < lines.length && lines[index].trim() && !blockStart(lines[index])) {
        if (index + 1 < lines.length && lines[index].includes('|') && tableDivider.test(lines[index + 1])) break;
        paragraph.push(lines[index++]);
      }
      const p = node('p'); inline(p, paragraph.join('\n')); root.append(p);
    }
    return root;
  }

  function renderSources(sources) {
    const section = node('section', 'sources');
    const title = node('div', 'sources-title');
    title.append(icon('globe'), node('span', '', `参考来源 · ${sources.length}`));
    section.append(title);
    const grid = node('div', 'source-grid');
    sources.forEach((source, index) => {
      const url = safeURL(source.url);
      if (!url) return;
      const card = node('a', 'source-card');
      card.href = url.href; card.target = '_blank'; card.rel = 'noopener noreferrer';
      card.title = source.title;
      const domain = node('span', 'source-domain');
      domain.append(node('span', 'source-number', index + 1), document.createTextNode(url.hostname.replace(/^www\./, '')));
      card.append(node('strong', '', source.title || url.hostname), domain); grid.append(card);
    });
    section.append(grid);
    return section;
  }

  function messageView(message) {
    const article = node('article', `message message-${message.role}`);
    if (message.role === 'user') {
      const bubble = node('div', 'user-bubble');
      const files = normalizeFiles(message.files);
      if (files.length) {
        const attachments = node('div', 'user-files');
        attachments.setAttribute('aria-label', '随这条问题发送的文件');
        for (const file of files) {
          const card = node('div', 'attachment user-file');
          const info = node('div', 'attachment-info');
          const name = node('div', 'attachment-name', file.filename);
          name.title = file.filename;
          info.append(name, node('div', 'attachment-meta', typeof file.file_type === 'string' ? file.file_type.toUpperCase() : '文件'));
          card.append(icon('book'), info); attachments.append(card);
        }
        bubble.append(attachments);
      }
      bubble.append(node('div', 'user-text', message.content));
      article.append(bubble); return article;
    }
    const heading = node('div', 'assistant-heading');
    const avatar = node('span', 'assistant-avatar'); avatar.append(icon('spark'));
    heading.append(avatar, node('strong', '', '知序'));
    if (message.usedSearch) {
      const badge = node('span', 'search-badge'); badge.append(icon('globe'), document.createTextNode('已联网检索')); heading.append(badge);
    }
    const body = node('div', 'assistant-body');
    if (message.loading) {
      const thinking = node('div', 'thinking');
      const dots = node('span', 'thinking-dots'); dots.append(node('span'), node('span'), node('span'));
      thinking.append(dots, node('span', '', state.pending?.useSearch ? '正在检索资料并整理回答……' : '正在认真思考……'));
      thinking.setAttribute('role', 'status'); body.append(thinking);
    } else {
      body.append(markdown(message.content));
      if (message.incomplete) body.append(node('p', 'incomplete-note', '输出已中断 · 保存状态未确认，可重新加载会话查看'));
      const sources = normalizeSources(message.sources);
      if (sources.length) body.append(renderSources(sources));
      for (const item of normalizeDocumentSummaries(message.documentSummaries)) {
        const coverage = item.coverage;
        const panel = node('section', 'summary-coverage');
        panel.append(node('strong', '', `摘要覆盖范围 · ${item.filename}`));
        const complete = coverage.all_extracted_text_scanned === true && coverage.processed_chunks === coverage.total_chunks;
        panel.append(node('p', '', `${complete ? '已扫描全部已提取文字' : '已处理部分文字'} · ${coverage.processed_chunks}/${coverage.total_chunks} 个片段 · ${coverage.start_location || ''} 至 ${coverage.end_location || ''}`));
        if (typeof item.query === 'string') panel.append(node('p', '', `本次归纳范围：${item.query}`));
        if (coverage.warning_count > 0) panel.append(node('p', 'summary-warning', `文件有 ${coverage.warning_count} 条解析提示，未提取文字或扫描图片不在本次覆盖范围内。`));
        body.append(panel);
      }
      const fileSources = normalizeDocumentSources(message.documentSources);
      if (fileSources.length) {
        const section = node('section', 'sources file-sources');
        section.append(node('div', 'sources-title', `${message.documentSummaries?.length ? '摘要引用片段' : '文件参考片段'} · ${fileSources.length}`));
        for (const source of fileSources) {
          const details = node('details');
          details.append(node('summary', '', `${source.filename} · ${source.location || '正文'} · 片段 ${source.chunk_index}`), node('p', '', source.content));
          section.append(details);
        }
        body.append(section);
      }
      const actions = node('div', 'answer-actions');
      const copyButton = node('button', 'icon-button');
      copyButton.type = 'button'; copyButton.title = '复制回答'; copyButton.setAttribute('aria-label', '复制回答');
      copyButton.append(icon('copy')); copyButton.addEventListener('click', () => copy(message.content, copyButton));
      actions.append(copyButton);
      const meta = [];
      if (typeof message.model === 'string') meta.push(message.model);
      if (typeof message.usage?.total_tokens === 'number') meta.push(`${message.usage.total_tokens.toLocaleString()} tokens`);
      actions.append(node('span', 'answer-meta', meta.join(' · '))); body.append(actions);
    }
    article.append(heading, body);
    return article;
  }

  function renderConversation(optimisticMessage) {
    const session = activeSession();
    const messages = [...(session?.messages || [])];
    if (optimisticMessage) messages.push(optimisticMessage, { role: 'assistant', loading: true, usedSearch: state.pending?.useSearch });
    else if (state.preview && state.preview.sessionId === state.activeId) {
      messages.push(
        { role: 'user', content: state.preview.question, files: state.preview.files },
        { role: 'assistant', content: state.preview.content, sources: state.preview.sources, usedSearch: state.preview.usedSearch, incomplete: true },
      );
    }
    elements.welcome.hidden = messages.length > 0;
    elements.messages.hidden = messages.length === 0;
    elements.messages.replaceChildren(...messages.map(messageView));
    $('#conversation-title').textContent = session?.title || '新对话';
    $('#context-note').textContent = messages.length ? '会话记忆自动保存，下次可以接着聊。' : '好问题，值得认真回答。';
    requestAnimationFrame(() => { elements.scroll.scrollTop = elements.scroll.scrollHeight; });
  }

  function updateInput() {
    elements.input.style.height = 'auto';
    elements.input.style.height = `${Math.min(180, Math.max(51, elements.input.scrollHeight))}px`;
    const count = elements.input.value.length;
    $('#character-count').hidden = count < 6500;
    $('#character-count').textContent = `${count.toLocaleString()} / 8,000`;
    const pending = Boolean(state.pending);
    const invalidFiles = stagedFiles().some(file => file.error || file.uploading || !file.file_id);
    elements.send.disabled = state.loading || state.uploading || (!pending && (!elements.input.value.trim() || invalidFiles));
    elements.send.classList.toggle('stopping', pending);
    elements.send.replaceChildren(icon(pending ? 'stop' : 'arrow'));
    elements.send.setAttribute('aria-label', pending ? '停止生成回答' : '发送消息');
    elements.send.title = pending ? '停止接收回答' : '发送消息';
    elements.input.disabled = pending || state.loading;
    $('#upload-file').disabled = pending || state.loading || state.uploading;
    $('#retry').disabled = pending || state.loading || state.uploading;
    $('#composer-mode').textContent = state.loading ? '正在读取会话记忆……' : '从容思考，认真回答';
    renderAttachments();
  }

  function startNew() {
    if (state.pending || state.loading || state.uploading) return;
    retryMessage = '';
    state.activeId = null;
    state.preview = null;
    state.newFiles = [];
    $('#upload-status').hidden = true;
    elements.input.value = '';
    elements.error.hidden = true;
    renderSidebar(); renderConversation(); updateInput(); persist(); toggleSidebar(false);
    elements.input.focus();
  }

  function contextFor(messages, message) {
    const context = messages.slice(-40).map((item) => ({ role: item.role, content: item.content.slice(0, 8000) }));
    let length = message.length + context.reduce((total, item) => total + item.content.length, 0);
    while (context.length && length > 60000) {
      for (const item of context.splice(0, 2)) length -= item.content.length;
    }
    return context;
  }

  async function readEvents(response, onEvent) {
    if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) {
      throw new Error('服务未返回流式响应，请重启服务并刷新页面。');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let completed = false;
    try {
      while (!completed) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const block = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          let name = 'message';
          const data = [];
          for (const line of block.split(/\r?\n/)) {
            if (line.startsWith('event:')) name = line.slice(6).trim();
            else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
          }
          if (!data.length) continue;
          let payload;
          try { payload = JSON.parse(data.join('\n')); }
          catch { throw new Error('收到无法解析的回答片段，请重试。'); }
          onEvent(name, payload);
          if (name === 'done') { completed = true; break; }
        }
        if (done) break;
        if (buffer.length > 1024 * 1024) throw new Error('回答数据格式异常，请重试。');
      }
      if (!completed) {
        const error = new Error('回答连接已中断，未收到完成确认。请重新加载会话后重试。');
        error.reload = true;
        throw error;
      }
    } finally {
      try { await reader.cancel(); } catch {}
      reader.releaseLock();
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (state.pending) { state.pending.controller.abort('user'); return; }
    if (state.loading || state.uploading) return;
    const message = elements.input.value.trim();
    if (!message) return;
    if (message.length > 8000) { showError('消息最多 8000 字符，请精简后重试。'); return; }
    if (stagedFiles().some(file => file.error || file.uploading || !file.file_id)) {
      showError('请先完成附件上传，或移除失败附件后再发送。'); return;
    }
    state.preview = null;
    const attached = stagedFiles().filter(file => file.file_id && !file.error);
    const messageFiles = attached.map(({ file_id, filename, file_type }) => ({ file_id, filename, file_type }));
    let session = activeSession();
    const created = !session;
    if (!session) {
      session = { id: crypto.randomUUID(), conversationId: null, title: message.slice(0, 32), messages: [], files: [], stagedFiles: state.newFiles };
      state.sessions.unshift(session);
      state.activeId = session.id;
    }
    const controller = new AbortController();
    retryMessage = '';
    elements.input.value = '';
    state.pending = { controller, useSearch: false };
    elements.error.hidden = true;
    renderSidebar(); updateInput(); renderConversation({ role: 'user', content: message, files: messageFiles });
    const outputBody = elements.messages.lastElementChild.querySelector('.assistant-body');
    const partial = { text: '', sources: [], searched: false };
    let liveText;
    let paintFrame;
    let result;
    function appendText(text) {
      if (typeof text !== 'string') throw new Error('回答片段格式不正确。');
      partial.text += text;
      if (!liveText) {
        liveText = node('div', 'prose streaming-text');
        outputBody.replaceChildren(liveText);
        outputBody.parentElement.setAttribute('aria-busy', 'true');
      }
      if (paintFrame) return;
      paintFrame = requestAnimationFrame(() => {
        paintFrame = null;
        const follow = elements.scroll.scrollHeight - elements.scroll.scrollTop - elements.scroll.clientHeight < 100;
        liveText.textContent = partial.text;
        if (follow) elements.scroll.scrollTop = elements.scroll.scrollHeight;
      });
    }
    let timeout;
    function resetTimeout() {
      clearTimeout(timeout);
      timeout = setTimeout(() => controller.abort('timeout'), 180000);
    }
    resetTimeout();
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({
          message, conversation_id: session.conversationId,
          history: session.conversationId ? [] : contextFor(session.messages, message),
          file_ids: attached.map(file => file.file_id),
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        let data;
        try { data = await response.json(); } catch { throw new Error(`请求失败（HTTP ${response.status}），请稍后重试。`); }
        const detail = typeof data.detail === 'string' ? data.detail : '消息格式不正确或上下文过长，请调整后重试。';
        const error = new Error(detail); error.status = response.status; throw error;
      }
      await readEvents(response, (name, data) => {
        resetTimeout();
        if (name === 'sources') {
          partial.sources = normalizeSources(data.sources);
          partial.searched = data.searched === true;
          state.pending.useSearch = partial.searched;
          if (partial.searched && !outputBody.parentElement.querySelector('.search-badge')) {
            const badge = node('span', 'search-badge');
            badge.append(icon('globe'), document.createTextNode('已联网检索'));
            outputBody.parentElement.querySelector('.assistant-heading').append(badge);
          }
        }
        else if (name === 'tool_start') {
          if (data.reset) {
            if (paintFrame) cancelAnimationFrame(paintFrame);
            paintFrame = null; liveText = null; partial.text = '';
          }
          const prefix = data.name === 'summarize_document' ? '正在分段总结文档' : (data.name === 'search_documents' ? '正在检索文档' : '正在搜索网页');
          const label = typeof data.query === 'string' && data.query ? `${prefix}：${data.query}` : '正在准备查询资料……';
          const status = node('div', 'thinking', label);
          status.setAttribute('role', 'status');
          outputBody.replaceChildren(status);
        }
        else if (name === 'tool_progress') {
          const status = node('div', 'thinking', typeof data.message === 'string' ? data.message : '正在处理文档……');
          status.setAttribute('role', 'status');
          outputBody.replaceChildren(status);
        }
        else if (name === 'delta') appendText(data.text);
        else if (name === 'error') {
          const error = new Error(typeof data.detail === 'string' ? data.detail : '回答生成失败，请重试。');
          error.status = data.status;
          throw error;
        } else if (name === 'done') result = data;
      });
      const data = result;
      if (!data || typeof data.reply !== 'string' || !data.reply.trim()) throw new Error('模型未返回有效回答，请重试。');
      if (typeof data.conversation_id !== 'string') throw new Error('服务未返回会话 ID，请更新服务后重试。');
      session.conversationId = data.conversation_id;
      session.files = [...normalizeFiles(session.files), ...attached];
      session.stagedFiles = [];
      state.newFiles = [];
      $('#upload-status').hidden = true;
      session.messages.push(
        { role: 'user', content: message, files: messageFiles },
        { role: 'assistant', content: data.reply, sources: normalizeSources(data.sources), documentSources: normalizeDocumentSources(data.document_sources), documentSummaries: normalizeDocumentSummaries(data.document_summaries), model: data.model, usage: data.usage, usedSearch: data.searched === true },
      );
      session.messages = session.messages.slice(-80);
      state.sessions = [session, ...state.sessions.filter((item) => item.id !== session.id)].slice(0, 20);
      elements.input.value = '';
      persist();
    } catch (error) {
      retryMessage = message;
      if (created) { state.newFiles = session.stagedFiles; state.sessions = state.sessions.filter((item) => item.id !== session.id); state.activeId = null; }
      if (partial.text) {
        state.preview = { sessionId: state.activeId, question: message, files: messageFiles, content: partial.text, sources: partial.sources, usedSearch: partial.searched };
      }
      if (controller.signal.aborted && controller.signal.reason === 'user') toast('已停止接收。');
      else if (controller.signal.aborted) showError('等待回答超时，请稍后重试。');
      else showError(error instanceof TypeError ? '无法连接服务，请检查服务是否启动及网络连接。' : error.message, error.reload || [404, 409].includes(error.status) || (partial.text && error instanceof TypeError) ? 'sync' : 'chat');
    } finally {
      clearTimeout(timeout);
      if (paintFrame) cancelAnimationFrame(paintFrame);
      state.pending = null;
      renderSidebar(); renderConversation(); updateInput();
      elements.input.focus();
    }
  }

  function showError(message, action = 'chat') {
    elements.errorText.textContent = message;
    elements.error.dataset.action = action;
    $('#retry').textContent = action === 'sync' ? '重新加载' : '重试';
    elements.error.hidden = false;
  }

  async function checkConnection() {
    const button = $('#connection');
    button.disabled = true;
    try {
      const response = await fetch('/health', { signal: AbortSignal.timeout(5000), cache: 'no-store' });
      if (!response.ok || (await response.json()).status !== 'ok') throw new Error('Unavailable');
      button.className = 'connection connected';
      $('#connection-label').textContent = '服务已连接';
    } catch {
      button.className = 'connection disconnected';
      $('#connection-label').textContent = '服务未连接';
    } finally { button.disabled = false; }
  }

  elements.form.addEventListener('submit', submit);
  $('#upload-file').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', async (event) => {
    const files = [...event.target.files]; event.target.value = '';
    if (files.length) await uploadFiles(files);
  });
  elements.input.addEventListener('input', updateInput);
  elements.input.addEventListener('paste', async (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    let files = [...(clipboard.files || [])];
    if (!files.length) {
      files = [...(clipboard.items || [])].filter(item => item.kind === 'file').map(item => item.getAsFile()).filter(Boolean);
    }
    if (!files.length) return;
    event.preventDefault();
    if (state.pending || state.loading || state.uploading) { toast('请等当前操作完成后再粘贴文件。'); return; }
    await uploadFiles(files);
  });
  elements.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); elements.form.requestSubmit(); }
  });
  elements.newChat.addEventListener('click', startNew);
  elements.menu.addEventListener('click', () => toggleSidebar(!elements.sidebar.classList.contains('open')));
  elements.backdrop.addEventListener('click', () => toggleSidebar(false));
  $('#connection').addEventListener('click', checkConnection);
  $('#retry').addEventListener('click', () => {
    if (elements.error.dataset.action === 'sync') { syncConversations(); return; }
    if (!elements.input.value.trim() && retryMessage) elements.input.value = retryMessage;
    elements.form.requestSubmit();
  });
  $('#dismiss-error').addEventListener('click', () => { elements.error.hidden = true; });
  for (const suggestion of document.querySelectorAll('.suggestion')) {
    suggestion.addEventListener('click', () => {
      elements.input.value = suggestion.dataset.prompt;
      updateInput(); persist(); elements.input.focus();
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') toggleSidebar(false);
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); startNew(); }
  });
  window.addEventListener('online', checkConnection);
  window.addEventListener('offline', checkConnection);
  restore(); renderSidebar(); renderConversation(); updateInput(); checkConnection();
  syncConversations();
})();
