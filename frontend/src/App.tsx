import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, APIError } from './api';
import { ItineraryPanel } from './ItineraryPanel';
import { PreferencePanel, preferenceError } from './PreferencePanel';
import { PreferenceProposalCards } from './PreferenceProposalCards';
import { activityForTurn, applyConversationEvent, latestModelProgress, mergeConversationSnapshot } from './activity';
import { RunActivity } from './RunActivity';
import type { AgentEvent, Conversation, ConversationStatus, ConversationSummary, Health, ModelProgress, PreferenceProfile } from './types';

const STATUS: Record<ConversationStatus, string> = {
  idle: '新的旅程', running: '正在安排', waiting_user: '等待你的补充', completed: '已生成方案', error: '需要重试',
};
const EXAMPLES = [
  { mark: '01', title: '去一座喜欢的城市', text: '从上海出发，去成都玩 4 天，想吃当地美食、看看街巷，节奏轻松一点。' },
  { mark: '02', title: '找一点出发的灵感', text: '这个周末想从北京出去放松，两天时间，预算 1500 元，帮我找个适合散心的地方。' },
  { mark: '03', title: '让空闲时间有个去处', text: '下周去杭州出差，周三下午 3 点会议结束，晚上 8 点从杭州东站返程，想顺便逛一逛。' },
];

function Icon({ name, size = 20 }: { name: 'plus' | 'arrow' | 'compass' | 'chat' | 'menu' | 'refresh' | 'close'; size?: number }) {
  const paths = {
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <><path d="M12 19V5M6 11l6-6 6 6" /></>,
    compass: <><circle cx="12" cy="12" r="9" /><path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z" /></>,
    chat: <path d="M20 11.5a8 8 0 0 1-8 8 9 9 0 0 1-4-.9L3 20l1.4-5a9 9 0 0 1-.9-4A8 8 0 0 1 20 11.5Z" />,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    refresh: <><path d="M20 4v6h-6M4 20v-6h6" /><path d="M6.1 7a7 7 0 0 1 11.6-1L20 10M4 14l2.3 4A7 7 0 0 0 18 17" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function readableError(error: unknown) {
  if (error instanceof TypeError) return '暂时无法连接服务。请确认后端已启动，重新连接后会自动恢复会话。';
  return error instanceof Error ? error.message : '请求失败，请稍后再试。';
}

function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

function safeLink(url: string) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
  } catch {
    return '';
  }
}

function savedConversation() {
  try { return localStorage.getItem('travel-agent.activeConversation'); } catch { return null; }
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [snapshots, setSnapshots] = useState<Record<string, Conversation>>({});
  const [activeId, setActiveId] = useState<string | null>(savedConversation);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [globalError, setGlobalError] = useState('');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [streaming, setStreaming] = useState<Set<string>>(new Set());
  const [modelProgress, setModelProgress] = useState<Record<string, ModelProgress | null>>({});
  const [preferenceProfile, setPreferenceProfile] = useState<PreferenceProfile | null>(null);
  const latestPreferenceProfile = useRef<PreferenceProfile | null>(null);
  const [preferenceLoadError, setPreferenceLoadError] = useState('');
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [preferenceMutating, setPreferenceMutating] = useState(false);
  const [preferenceRefresh, setPreferenceRefresh] = useState(0);
  const [newUseSaved, setNewUseSaved] = useState(true);
  const [preferenceControls, setPreferenceControls] = useState<Record<string, boolean>>({});
  const [controlErrors, setControlErrors] = useState<Record<string, string>>({});
  const [controlChanging, setControlChanging] = useState(false);
  const [controlNotices, setControlNotices] = useState<Record<string, string>>({});
  const controlReadSequence = useRef<Record<string, number>>({});
  const streams = useRef(new Set<string>());
  const eventCursors = useRef<Record<string, number>>({});
  const sendLock = useRef(false);
  const followProgress = useRef(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const active = activeId ? snapshots[activeId] : undefined;
  const draftKey = activeId ?? 'new';
  const draft = drafts[draftKey] ?? '';
  const running = !!activeId && (active?.status === 'running' || streaming.has(activeId));
  const unavailable = health?.model_configured === false;
  const inputDisabled = running || creating || loading || preferenceMutating || controlChanging || (!!activeId && !active) || unavailable;
  const error = activeId ? errors[activeId] : '';
  const anyRunning = running || creating || streaming.size > 0 || items.some((item) => item.status === 'running');
  const backgroundRunning = items.some((item) => item.status === 'running' && item.id !== activeId);
  const useSaved = activeId ? preferenceControls[activeId] : newUseSaved;
  const controlDisabled = running || creating || (!!activeId && (controlChanging || preferenceMutating || useSaved === undefined));
  const controlNotice = activeId ? controlNotices[activeId] : '';

  const publishPreferenceProfile = useCallback((profile: PreferenceProfile) => {
    if (!latestPreferenceProfile.current || profile.revision >= latestPreferenceProfile.current.revision) {
      latestPreferenceProfile.current = profile;
      setPreferenceProfile(profile);
    }
    setPreferenceLoadError('');
    return latestPreferenceProfile.current;
  }, []);

  const refreshPreferences = useCallback(async () => {
    try {
      const profile = await api.preferences();
      // A delayed GET cannot roll back a profile already returned by a successful write.
      return publishPreferenceProfile(profile);
    } catch (reason) {
      setPreferenceLoadError(preferenceError(reason));
      throw reason;
    }
  }, [publishPreferenceProfile]);

  const refreshPreferenceControl = useCallback(async (id: string) => {
    const sequence = (controlReadSequence.current[id] ?? 0) + 1;
    controlReadSequence.current[id] = sequence;
    try {
      const control = await api.conversationPreferences(id);
      if (controlReadSequence.current[id] !== sequence) return;
      setPreferenceControls((previous) => ({ ...previous, [id]: control.use_saved }));
      setControlErrors((previous) => ({ ...previous, [id]: '' }));
    } catch (reason) {
      if (controlReadSequence.current[id] !== sequence) return;
      setControlErrors((previous) => ({ ...previous, [id]: preferenceError(reason) }));
    }
  }, []);

  const openPreferences = () => {
    setPreferencesOpen(true); setSidebarOpen(false);
    void refreshPreferences().catch(() => undefined);
  };
  const closePreferences = useCallback(() => setPreferencesOpen(false), []);

  const changePreferenceControl = async (use: boolean) => {
    if (controlDisabled) return;
    if (activeId) setControlNotices((previous) => ({ ...previous, [activeId]: '' }));
    if (!activeId) { setNewUseSaved(use); return; }
    const id = activeId;
    controlReadSequence.current[id] = (controlReadSequence.current[id] ?? 0) + 1;
    setControlChanging(true);
    try {
      const control = await api.setConversationPreferences(id, use);
      setPreferenceControls((previous) => ({ ...previous, [id]: control.use_saved }));
      setControlErrors((previous) => ({ ...previous, [id]: '' }));
      setControlNotices((previous) => ({ ...previous, [id]: use ? '本次偏好使用已开启。' : '本次不使用偏好，聊天记录仍会保留。' }));
    } catch (reason) {
      setControlErrors((previous) => ({ ...previous, [id]: preferenceError(reason) }));
      // Retrieve the canonical setting after rejection without repeating the mutation.
      await refreshPreferenceControl(id);
      setControlErrors((previous) => ({ ...previous, [id]: preferenceError(reason) }));
    } finally { setControlChanging(false); }
  };


  const updateSnapshot = useCallback((conversation: Conversation) => {
    const current = (conversation.event_seq ?? 0) >= (eventCursors.current[conversation.id] ?? 0);
    if (current) {
      setModelProgress((previous) => ({ ...previous, [conversation.id]: conversation.model_progress
        ? latestModelProgress(previous[conversation.id], conversation.model_progress) : null }));
    }
    if (conversation.event_seq !== undefined) {
      eventCursors.current[conversation.id] = Math.max(eventCursors.current[conversation.id] ?? 0, conversation.event_seq);
    }
    setSnapshots((previous) => ({ ...previous, [conversation.id]: mergeConversationSnapshot(previous[conversation.id], conversation) }));
    if (current) setItems((previous) => [conversation, ...previous.filter((item) => item.id !== conversation.id)]
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
  }, []);

  const refreshConversation = useCallback(async (id: string) => {
    const conversation = await api.get(id);
    updateSnapshot(conversation);
    return conversation;
  }, [updateSnapshot]);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([api.health(), api.list()]);
    let failure = '';
    if (results[0].status === 'fulfilled') setHealth(results[0].value);
    else failure = readableError(results[0].reason);
    if (results[1].status === 'fulfilled') setItems(results[1].value.items);
    else failure = readableError(results[1].reason);
    setGlobalError(failure);
    setLoading(false);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { void refreshPreferences().catch(() => undefined); }, [refreshPreferences]);
  useEffect(() => {
    if (activeId) void refreshPreferenceControl(activeId);
  }, [activeId, refreshPreferenceControl]);

  useEffect(() => {
    if (!backgroundRunning) return;
    let disposed = false;
    const timer = window.setInterval(() => {
      void api.list().then((result) => { if (!disposed) setItems(result.items); }).catch(() => undefined);
    }, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [backgroundRunning]);

  useEffect(() => {
    try {
      if (activeId) localStorage.setItem('travel-agent.activeConversation', activeId);
      else localStorage.removeItem('travel-agent.activeConversation');
    } catch { /* Storage restrictions do not prevent using the chat. */ }
  }, [activeId]);

  useEffect(() => {
    if (!activeId || streams.current.has(activeId)) return;
    let disposed = false;
    void api.get(activeId).then((conversation) => {
      if (!disposed) updateSnapshot(conversation);
    }).catch((reason: unknown) => {
      if (!disposed) setErrors((previous) => ({ ...previous, [activeId]: readableError(reason) }));
    });
    return () => { disposed = true; };
  }, [activeId, updateSnapshot]);

  // Refreshes reconnect to the persisted run; closing a stream never cancels it.
  useEffect(() => {
    if (!activeId || active?.status !== 'running' || streaming.has(activeId)) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const conversation = await api.get(activeId);
        if (disposed || streams.current.has(activeId)) return;
        updateSnapshot(conversation);
        setErrors((previous) => ({ ...previous, [activeId]: '' }));
      } catch (reason) {
        if (!disposed) setErrors((previous) => ({ ...previous, [activeId]: readableError(reason) }));
      }
      if (!disposed) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 2000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [activeId, active?.status, streaming, updateSnapshot]);

  useEffect(() => {
    followProgress.current = true;
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [activeId]);

  useEffect(() => {
    if (followProgress.current) bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [active?.transcript.length, active?.activity_events?.length, running]);

  useEffect(() => {
    if (textarea.current) {
      textarea.current.style.height = 'auto';
      textarea.current.style.height = `${Math.min(textarea.current.scrollHeight, 170)}px`;
    }
  }, [draft]);

  const updateStatus = (id: string, status: ConversationStatus) => {
    setSnapshots((previous) => previous[id]
      ? { ...previous, [id]: { ...previous[id], status } } : previous);
    setItems((previous) => previous.map((item) => item.id === id ? { ...item, status } : item));
  };

  const onEvent = useCallback((id: string, event: AgentEvent) => {
    if (event.event_seq !== undefined) {
      if (event.event_seq <= (eventCursors.current[id] ?? 0)) return;
      eventCursors.current[id] = event.event_seq;
    }
    setSnapshots((previous) => previous[id] ? { ...previous, [id]: applyConversationEvent(previous[id], event) } : previous);
    if (event.type === 'status' || event.type === 'done') {
      setItems((previous) => previous.map((item) => item.id === id ? { ...item, status: event.status } : item));
      if (event.status === 'running') {
        setModelProgress((previous) => ({ ...previous, [id]: null }));
      }
    } else if (event.type === 'model_progress') {
      setModelProgress((previous) => ({ ...previous, [id]: latestModelProgress(previous[id], event) }));
    } else if (event.type === 'tool_started' || event.type === 'tool_progress' || event.type === 'tool_finished') {
      if (event.type === 'tool_finished') setPreferenceRefresh((previous) => previous + 1);
    } else if (event.type === 'itinerary_updated') {
      void refreshConversation(id).catch(() => undefined);
    } else if (event.type === 'error') {
      setErrors((previous) => ({ ...previous, [id]: event.message || '执行出错，请稍后重试。' }));
    }
  }, [refreshConversation]);

  useEffect(() => {
    if (!activeId || active?.status !== 'running' || streaming.has(activeId) || !active.engine) return;
    const controller = new AbortController();
    void api.replay(activeId, eventCursors.current[activeId] ?? 0,
      (event) => onEvent(activeId, event), controller.signal).catch(() => {
      // The canonical transcript poll above also covers service restarts.
    });
    return () => controller.abort();
  }, [activeId, active?.status, active?.engine, streaming, onEvent]);

  const send = async () => {
    const content = draft.trim();
    if (!content || inputDisabled || sendLock.current) return;
    sendLock.current = true;
    let id = activeId;
    setGlobalError('');
    try {
      if (!id) {
        setCreating(true);
        const conversation = await api.create(newUseSaved);
        id = conversation.id;
        setPreferenceControls((previous) => ({ ...previous, [conversation.id]: newUseSaved }));
        updateSnapshot(conversation);
        setActiveId(id);
        setDrafts((previous) => ({ ...previous, new: '' }));
      }
      const runId = id;
      streams.current.add(runId);
      setStreaming(new Set(streams.current));
      setDrafts((previous) => ({ ...previous, [runId]: '' }));
      setErrors((previous) => ({ ...previous, [runId]: '' }));
      setModelProgress((previous) => ({ ...previous, [runId]: null }));
      const optimisticRun = `local-${crypto.randomUUID()}`;
      setSnapshots((previous) => ({ ...previous, [runId]: {
        ...previous[runId], status: 'running', last_error: null, active_run_id: optimisticRun,
        transcript: [...previous[runId].transcript, {
          role: 'user', content, kind: 'message', created_at: new Date().toISOString(), optimistic: true, run_id: optimisticRun,
          optimistic_after_seq: eventCursors.current[runId] ?? 0,
        }],
      } }));
      setCreating(false);
      sendLock.current = false;
      try {
        await api.send(runId, content, (event) => onEvent(runId, event), snapshots[runId]?.question_id);
      } catch (reason) {
        setErrors((previous) => ({ ...previous, [runId]: readableError(reason) }));
        if (reason instanceof APIError) {
          setDrafts((previous) => ({ ...previous, [runId]: content }));
          if (reason.status >= 400 && reason.status < 500) setSnapshots((previous) => ({ ...previous, [runId]: {
            ...previous[runId], transcript: previous[runId].transcript.filter((message) => !message.optimistic || message.run_id !== optimisticRun),
          } }));
        }
      } finally {
        // Replace temporary stream messages with the canonical transcript, never append it.
        try {
          const saved = await refreshConversation(runId);
          const lastUserMessage = [...saved.transcript].reverse().find((message) => message.role === 'user');
          if (saved.status !== 'running' && lastUserMessage?.content !== content) {
            setDrafts((previous) => ({ ...previous, [runId]: previous[runId] || content }));
          }
        } catch (reason) {
          updateStatus(runId, 'running');
          setErrors((previous) => ({ ...previous, [runId]: readableError(reason) }));
        }
        streams.current.delete(runId);
        setStreaming(new Set(streams.current));
        void api.list().then((result) => setItems(result.items)).catch(() => undefined);
      }
    } catch (reason) {
      setGlobalError(readableError(reason));
    } finally {
      sendLock.current = false;
      setCreating(false);
    }
  };

  const startNew = () => {
    setActiveId(null);
    setNewUseSaved(true);
    setSidebarOpen(false);
    setGlobalError('');
    textarea.current?.focus();
  };

  const retryConnection = () => {
    void refresh();
    void refreshPreferences().catch(() => undefined);
    if (activeId) void refreshPreferenceControl(activeId);
    if (activeId && !streams.current.has(activeId)) {
      void refreshConversation(activeId).then(() => setErrors((previous) => ({ ...previous, [activeId]: '' })))
        .catch((reason: unknown) => setErrors((previous) => ({ ...previous, [activeId]: readableError(reason) })));
    }
  };

  return <div className="app-shell">
    {sidebarOpen && <button className="sidebar-scrim" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'is-open' : ''}`} aria-label="会话列表">
      <button className="brand" onClick={startNew} aria-label="出行助手，返回新旅程">
        <span className="brand-icon"><Icon name="compass" size={27} /></span>
        <span><strong>出行助手</strong><small>把想法变成旅程</small></span>
      </button>
      <button className="new-chat" onClick={startNew} disabled={creating}><Icon name="plus" size={18} />开启新旅程</button>
      <button className="preferences-entry sidebar-preferences" onClick={openPreferences}>出行偏好<span>管理长期偏好</span></button>
      <div className="list-label"><span>我的会话</span><span>{items.length.toString().padStart(2, '0')}</span></div>
      <nav className="conversation-list">
        {loading ? <p className="list-empty">正在载入会话…</p> : items.length === 0
          ? <p className="list-empty">每一次出发，<br />都从一个想法开始。</p>
          : items.map((item) => <button key={item.id} className={`conversation-item ${activeId === item.id ? 'selected' : ''}`}
              onClick={() => { setActiveId(item.id); setSidebarOpen(false); }} aria-current={activeId === item.id ? 'page' : undefined}>
              <Icon name="chat" size={17} />
              <span className="conversation-info"><strong>{item.title || '新的旅程'}</strong><small>
                <span className={item.status === 'running' ? 'status-running' : ''}>{STATUS[item.status] ?? '会话'}</span>
                <span>{dateLabel(item.updated_at)}</span>
              </small></span>
            </button>)}
      </nav>
      <div className="sidebar-bottom"><span className="small-compass">↗</span><p>远方，或是附近。<br /><strong>总有值得出发的地方。</strong></p></div>
    </aside>

    <main className="main-panel">
      <header className="topbar">
        <div className="topbar-title"><button className="icon-button mobile-menu" aria-label="打开会话列表" onClick={() => setSidebarOpen(true)}><Icon name="menu" /></button>
          <span>{active?.title || (activeId ? '载入会话' : '新的旅程')}</span>
          {active && <span className={`status-tag ${running ? 'is-running' : ''}`}>{running ? '正在安排' : STATUS[active.status] ?? '会话'}</span>}
          {active?.itinerary && <button className="view-itinerary" onClick={() => document.getElementById('current-itinerary')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>查看行程 · v{active.itinerary.revision}</button>}
        </div>
        <div className="connection"><span className={`connection-dot ${health?.model_configured ? 'ready' : ''}`} /><span>{health?.model_configured ? '助手已就绪' : health ? '等待模型配置' : '连接服务中'}</span>
          <button className="preferences-entry" onClick={openPreferences} aria-expanded={preferencesOpen}>出行偏好</button>
          <button className="icon-button" onClick={retryConnection} aria-label="重新连接服务" title="重新连接服务"><Icon name="refresh" size={16} /></button>
        </div>
      </header>

      {(globalError || unavailable) && <div className="notice" role="alert"><span>{globalError || '请先在后端配置 DeepSeek API Key，再开始你的旅程。'}</span><button onClick={retryConnection}>重新连接</button></div>}

      <div className="chat-scroll" onScroll={(event) => {
        const element = event.currentTarget;
        followProgress.current = element.scrollHeight - element.scrollTop - element.clientHeight < 160;
      }}>
        {active?.itinerary && activeId && <ItineraryPanel key={`itinerary:${activeId}`} conversationId={activeId} current={active.itinerary} />}
        {!activeId || (active && active.transcript.length === 0) ? <section className="welcome">
          <div className="welcome-eyebrow"><span />从这里，走向想去的地方</div>
          <h1>下一站，<br /><span>去哪里？</span></h1>
          <p className="welcome-copy">一个目的地，一段空闲时间，或只是想出去走走。<br />告诉我你的想法，我们一起安排。</p>
          <div className="welcome-line"><span>试着从一个想法开始</span><i /></div>
          <div className="example-grid">{EXAMPLES.map((example) => <button key={example.mark} className="example-card" disabled={inputDisabled}
            onClick={() => { setDrafts((previous) => ({ ...previous, [draftKey]: example.text })); textarea.current?.focus(); }}>
            <span className="example-top"><span>{example.mark}</span><span>↗</span></span><strong>{example.title}</strong><p>{example.text}</p>
          </button>)}</div>
          <p className="welcome-note">不必一次说清所有细节，助手会在需要时向你确认。</p>
        </section> : !active ? <div className="loading-conversation"><span className="loader" />正在找回这段旅程…</div>
          : <section className="transcript" aria-label="聊天记录" aria-live="polite" aria-relevant="additions">
            {active.transcript.map((message, index) => <Fragment key={message.message_id ?? `${index}-${message.created_at}`}><article className={`message ${message.role} ${message.kind === 'error' ? 'message-error' : ''}`}>
              <div className="message-avatar">{message.role === 'assistant' ? <Icon name="compass" size={20} /> : '你'}</div>
              <div className="message-main"><div className="message-meta"><strong>{message.role === 'assistant' ? '出行助手' : '你'}</strong>{message.kind === 'question' && <span>需要你补充</span>}</div>
                {message.role === 'user' ? <div className="user-text">{message.content}</div> : <div className="markdown">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeLink} components={{
                    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}<span className="external-link" aria-label="新窗口打开">↗</span></a> : <span>{children}</span>,
                    img: () => null,
                    table: ({ children }) => <div className="table-wrap"><table>{children}</table></div>,
                  }}>{message.content}</ReactMarkdown>
                </div>}
              </div>
            </article>{message.role === 'user' && <RunActivity events={activityForTurn(active, index)}
              running={running && !active.transcript.slice(index + 1).some((entry) => entry.role === 'user')}
              modelProgress={modelProgress[activeId]} />}</Fragment>)}
          </section>}
        {(error || (active?.last_error && active.status === 'error')) && <div className="conversation-error" role="alert"><span>{error || active?.last_error?.message}</span><button onClick={retryConnection}>重新载入</button></div>}
        {activeId && active && <PreferenceProposalCards key={`preference-proposals:${activeId}`} conversationId={activeId}
          refreshKey={`${active.event_seq ?? 0}:${active.status}:${preferenceRefresh}`} profile={preferenceProfile}
          busy={anyRunning || preferenceMutating || controlChanging} conversationBusy={running || preferenceMutating || controlChanging}
          onRefreshProfile={refreshPreferences} onChanged={publishPreferenceProfile} onMutating={setPreferenceMutating} />}
        <div ref={bottom} />
      </div>

      <footer className="composer-area"><div className="conversation-preferences">
        <label><input type="checkbox" checked={useSaved ?? false} disabled={controlDisabled}
          onChange={(event) => void changePreferenceControl(event.target.checked)} />本次使用偏好</label>
        <span>{controlChanging ? '正在更新开关…' : running ? '本轮结束后可以切换' : useSaved === undefined ? '正在载入开关…'
          : !useSaved ? '本次不使用偏好，聊天记录仍会保留' : preferenceProfile?.enabled === false ? '长期偏好总开关已关闭'
          : preferenceProfile?.entries.length === 0 ? '还没有保存偏好' : '本次要求优先于已保存偏好'}</span>
        {(activeId && controlErrors[activeId]) && <p role="alert">{controlErrors[activeId]}</p>}
        {controlNotice && <p role="status">{controlNotice}</p>}
      </div><form className={`composer ${inputDisabled ? 'is-disabled' : ''}`} onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <label className="sr-only" htmlFor="travel-message">描述你的出行需求或补充信息</label>
        <textarea ref={textarea} id="travel-message" value={draft} rows={2} disabled={inputDisabled}
          placeholder={running ? '助手正在执行，完成后可以继续补充…' : active?.status === 'waiting_user' ? '补充信息，让我们继续安排…' : '想去哪里？有什么想法？告诉我你的出行需求…'}
          onChange={(event) => setDrafts((previous) => ({ ...previous, [draftKey]: event.target.value }))}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
        <div className="composer-bottom"><span>{running ? '安排中 · 请等待本轮完成' : 'Enter 发送 · Shift + Enter 换行'}</span><button className="send-button" type="submit" disabled={inputDisabled || !draft.trim()} aria-label={running ? '正在执行，请稍候' : '发送消息'}><Icon name="arrow" size={21} /></button></div>
      </form><div className="composer-footnote"><span>重要的营业时间、价格与预订信息，请以官方渠道为准。</span><span className="model-label">{health?.model || 'DeepSeek Flash'}</span></div></footer>
    </main>
    {preferencesOpen && <PreferencePanel profile={preferenceProfile} loadError={preferenceLoadError}
      busy={anyRunning || preferenceMutating || controlChanging} onRefresh={refreshPreferences} onChanged={publishPreferenceProfile}
      onMutating={setPreferenceMutating} onClose={closePreferences} />}
  </div>;
}
