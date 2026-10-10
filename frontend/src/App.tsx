import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, APIError } from './api';
import { ItineraryPanel } from './ItineraryPanel';
import { describeCalls, describeModel, updateCalls } from './progress';
import type { Calls } from './progress';
import type { AgentEvent, Conversation, ConversationStatus, ConversationSummary, Health, ModelProgress } from './types';

const STATUS: Record<ConversationStatus, string> = {
  idle: '新的旅程', running: '正在安排', waiting_user: '等待你的补充', completed: '已生成方案', error: '需要重试',
};
const TOOL_LABELS: Record<string, string> = {
  search_places: '查找地点', search_pois: '查找地点', get_place_details: '核实地点信息',
  get_attraction_opening_hours: '查询景点开放时间',
  geocode: '定位出行地点', get_routes: '查询路线', plan_route: '查询路线',
  get_weather: '查询天气', search_web: '查找出行资料', web_search: '查找出行资料',
  fetch_webpage: '阅读来源资料', search_flights: '查询航班', search_trains: '查询火车',
  search_coaches: '查询大巴', search_hotels: '查询住宿', validate_itinerary: '检查行程安排',
  save_itinerary: '保存行程修改',
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

function ModelProgressLabel({ progress }: { progress: ModelProgress }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [progress.call_id, progress.attempt, progress.started_at]);
  return <>{describeModel(progress, now)}</>;
}

export default function App() {
  const toolCalls = useRef<Record<string, Calls>>({});
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
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [modelProgress, setModelProgress] = useState<Record<string, ModelProgress | null>>({});
  const streams = useRef(new Set<string>());
  const eventCursors = useRef<Record<string, number>>({});
  const sendLock = useRef(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const active = activeId ? snapshots[activeId] : undefined;
  const draftKey = activeId ?? 'new';
  const draft = drafts[draftKey] ?? '';
  const running = !!activeId && (active?.status === 'running' || streaming.has(activeId));
  const unavailable = health?.model_configured === false;
  const inputDisabled = running || creating || loading || (!!activeId && !active) || unavailable;
  const error = activeId ? errors[activeId] : '';


  const updateSnapshot = useCallback((conversation: Conversation) => {
    if ((conversation.event_seq ?? 0) >= (eventCursors.current[conversation.id] ?? 0)) {
      setModelProgress((previous) => ({ ...previous, [conversation.id]: conversation.model_progress ?? null }));
    }
    if (conversation.tool_progress && (conversation.event_seq ?? 0) >= (eventCursors.current[conversation.id] ?? 0)) {
      toolCalls.current[conversation.id] = conversation.tool_progress;
      setProgress((previous) => ({ ...previous, [conversation.id]: describeCalls(conversation.tool_progress!, TOOL_LABELS) }));
    }
    if (conversation.event_seq !== undefined) {
      eventCursors.current[conversation.id] = Math.max(eventCursors.current[conversation.id] ?? 0, conversation.event_seq);
    }
    setSnapshots((previous) => ({ ...previous, [conversation.id]: conversation }));
    setItems((previous) => [conversation, ...previous.filter((item) => item.id !== conversation.id)]
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
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [activeId, active?.transcript.length, running]);

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
    if (event.type === 'status' || event.type === 'done') {
      updateStatus(id, event.status);
      if (event.status === 'running') {
        setModelProgress((previous) => ({ ...previous, [id]: null }));
        toolCalls.current[id] = {};
        setProgress((previous) => ({ ...previous, [id]: '正在理解你的需求，安排下一步…' }));
      }
    } else if (event.type === 'model_progress') {
      setModelProgress((previous) => ({ ...previous, [id]: event }));
    } else if (event.type === 'tool_started' || event.type === 'tool_progress' || event.type === 'tool_finished') {
      toolCalls.current[id] = updateCalls(toolCalls.current[id] ?? {}, event);
      setProgress((previous) => ({ ...previous, [id]: describeCalls(toolCalls.current[id], TOOL_LABELS) }));
    } else if (event.type === 'itinerary_updated') {
      void refreshConversation(id).catch(() => undefined);
    } else if (event.type === 'message') {
      if (event.role === 'user') return;
      setSnapshots((previous) => {
        const conversation = previous[id];
        if (!conversation) return previous;
        if (event.message_id && conversation.transcript.some((message) => message.message_id === event.message_id)) return previous;
        const lastMessage = conversation.transcript.at(-1);
        if (!event.message_id && lastMessage?.role === 'assistant' && lastMessage.content === event.content && lastMessage.kind === event.kind) return previous;
        return { ...previous, [id]: { ...conversation,
          question_id: event.kind === 'question' ? event.question_id : conversation.question_id,
          transcript: [...conversation.transcript, {
          role: 'assistant', content: event.content, kind: event.kind, created_at: new Date().toISOString(),
          message_id: event.message_id, question_id: event.question_id,
        }] } };
      });
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
        const conversation = await api.create();
        id = conversation.id;
        updateSnapshot(conversation);
        setActiveId(id);
        setDrafts((previous) => ({ ...previous, new: '' }));
      }
      const runId = id;
      streams.current.add(runId);
      setStreaming(new Set(streams.current));
      setDrafts((previous) => ({ ...previous, [runId]: '' }));
      setErrors((previous) => ({ ...previous, [runId]: '' }));
      setProgress((previous) => ({ ...previous, [runId]: '正在理解你的需求，安排下一步…' }));
      setSnapshots((previous) => ({ ...previous, [runId]: {
        ...previous[runId], status: 'running', last_error: null,
        transcript: [...previous[runId].transcript, {
          role: 'user', content, kind: 'message', created_at: new Date().toISOString(),
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
    setSidebarOpen(false);
    setGlobalError('');
    textarea.current?.focus();
  };

  const retryConnection = () => {
    void refresh();
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
          <button className="icon-button" onClick={retryConnection} aria-label="重新连接服务" title="重新连接服务"><Icon name="refresh" size={16} /></button>
        </div>
      </header>

      {(globalError || unavailable) && <div className="notice" role="alert"><span>{globalError || '请先在后端配置 DeepSeek API Key，再开始你的旅程。'}</span><button onClick={retryConnection}>重新连接</button></div>}

      <div className="chat-scroll">
        {active?.itinerary && activeId && <ItineraryPanel key={activeId} conversationId={activeId} current={active.itinerary} />}
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
            {active.transcript.map((message, index) => <article className={`message ${message.role} ${message.kind === 'error' ? 'message-error' : ''}`} key={`${index}-${message.created_at}`}>
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
            </article>)}
            {running && <div className="run-progress" role="status"><span className="loader" /><div><strong>{modelProgress[activeId]?.status === 'running' ? <ModelProgressLabel progress={modelProgress[activeId]!} /> : progress[activeId] ?? '正在继续安排你的旅程…'}</strong>{modelProgress[activeId]?.status === 'running' && Object.keys(toolCalls.current[activeId] ?? {}).length > 0 && <details><summary>查看查询与校验记录</summary><p>{describeCalls(toolCalls.current[activeId], TOOL_LABELS)}</p></details>}<p>执行期间请稍候，你可以切换会话或稍后回来。</p></div></div>}
          </section>}
        {(error || (active?.last_error && active.status === 'error')) && <div className="conversation-error" role="alert"><span>{error || active?.last_error?.message}</span><button onClick={retryConnection}>重新载入</button></div>}
        <div ref={bottom} />
      </div>

      <footer className="composer-area"><form className={`composer ${inputDisabled ? 'is-disabled' : ''}`} onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <label className="sr-only" htmlFor="travel-message">描述你的出行需求或补充信息</label>
        <textarea ref={textarea} id="travel-message" value={draft} rows={2} disabled={inputDisabled}
          placeholder={running ? '助手正在执行，完成后可以继续补充…' : active?.status === 'waiting_user' ? '补充信息，让我们继续安排…' : '想去哪里？有什么想法？告诉我你的出行需求…'}
          onChange={(event) => setDrafts((previous) => ({ ...previous, [draftKey]: event.target.value }))}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
        <div className="composer-bottom"><span>{running ? '安排中 · 请等待本轮完成' : 'Enter 发送 · Shift + Enter 换行'}</span><button className="send-button" type="submit" disabled={inputDisabled || !draft.trim()} aria-label={running ? '正在执行，请稍候' : '发送消息'}><Icon name="arrow" size={21} /></button></div>
      </form><div className="composer-footnote"><span>重要的营业时间、价格与预订信息，请以官方渠道为准。</span><span className="model-label">{health?.model || 'DeepSeek Flash'}</span></div></footer>
    </main>
  </div>;
}
