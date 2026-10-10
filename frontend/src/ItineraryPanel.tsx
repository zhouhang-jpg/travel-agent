import { useEffect, useState } from 'react';
import { api } from './api';
import type { ItinerarySummary, ItineraryVersion } from './types';

export function planTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}
export function changeText(section: string, value: unknown): string {
  if (value === null || value === undefined) return '无';
  if (typeof value !== 'object' || Array.isArray(value)) return '相关安排已更新';
  const row = value as Record<string, unknown>;
  const title = String(row.title ?? row.statement ?? '安排');
  if (section === 'items' || section === 'planning_window') return `${section === 'items' ? title + ' · ' : ''}${planTime(String(row.start))} — ${planTime(String(row.end))}`;
  if (section === 'lodging') return `${title} · ${row.check_in} 至 ${row.check_out}`;
  if (section === 'costs') {
    const total = row.total as { amount: string; currency: string } | null;
    return `${title} · ${total ? `${total.currency} ${total.amount}` : '费用未知'}`;
  }
  if (section === 'requirements') return title + (row.state === 'superseded' ? '（已被后续修改替代）' : '（当前有效）');
  if (section === 'party') return `${row.travelers ?? '未知'} 人，${row.rooms ?? '未知'} 间房`;
  if (section === 'budget') return `${row.currency} ${row.amount}`;
  return '相关安排已更新';
}
const sections: Record<string, string> = { items: '活动 / 交通', lodging: '住宿', costs: '费用', requirements: '需求', planning_window: '出行时间', party: '人数 / 房间', transfers: '转场与缓冲', opening_hours: '开放时间', budget: '预算', evidence: '来源适用条件', source_catalog: '来源与查询时间' };
const operations: Record<string, string> = { added: '新增', removed: '移除', changed: '修改' };
const origins: Record<string, string> = { user: '你的条件', assumption: '规划假设', evidence: '查询依据' };
const checkLabels: Record<string, string> = { transfers: '转场及缓冲', opening_hours: '开放时间', fixed_commitments: '固定安排范围', lodging: '住宿覆盖', cost_coverage: '费用完整性', budget: '预算核实', time_bounds: '安排时间', overlaps: '时间重叠' };
const lockText = (state: string) => state === 'user_reported_booked' ? '你告知已预订 · 未核验' : '已锁定';

export function ItineraryPanel({ conversationId, current }: { conversationId: string; current: ItineraryVersion }) {
  const [versions, setVersions] = useState<ItinerarySummary[]>([]);
  const [selected, setSelected] = useState(current);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(true);
  useEffect(() => {
    let disposed = false;
    setSelected(current);
    void api.itineraries(conversationId).then((response) => { if (!disposed) { setVersions(response.items); setError(''); } })
      .catch(() => { if (!disposed) setError('版本列表暂时无法载入。'); });
    return () => { disposed = true; };
  }, [conversationId, current.id]);
  const choose = async (id: string) => {
    setLoading(true);
    try { setSelected(id === current.id ? current : await api.itinerary(conversationId, id)); setError(''); }
    catch { setError('无法载入这个版本，请稍后重试。'); }
    finally { setLoading(false); }
  };
  const document = selected.document;
  const unknown = [...new Set(selected.review.checks.filter((c) => c.status === 'unknown').map((c) => checkLabels[c.category] ?? '相关信息'))];
  const pendingGroups = [...new Set(selected.review.pending.map((p) => p.message))].map((message) => ({
    message, count: selected.review.pending.filter((p) => p.message === message).length,
  }));
  return <section id="current-itinerary" className="itinerary-panel" aria-label="已保存行程">
    <div className="itinerary-header"><button onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      <span>已保存行程 · 版本 {selected.revision}</span><strong>{document.title}</strong></button>
      <select aria-label="查看行程版本" value={selected.id} disabled={loading} onChange={(e) => void choose(e.target.value)}>
        {versions.length ? versions.map((v) => <option key={v.id} value={v.id}>版本 {v.revision}{v.id === current.id ? ' · 当前' : ''}</option>) : <option value={current.id}>版本 {current.revision} · 当前</option>}
      </select></div>
    {error && <p role="alert">{error}</p>}
    {expanded && <div className="itinerary-body">
      {selected.review.status === 'unknown' && <p className="plan-notice">方案有待核实项，时间与费用尚未全部确认。</p>}
      {selected.id !== current.id && <p className="plan-notice">正在查看旧版本，当前方案仍是版本 {current.revision}。</p>}
      <p>{selected.change_reason}</p><p className="plan-context">{document.party.travelers ?? '人数待确认'}{document.party.travelers ? ' 人' : ''}{document.party.rooms && ` · ${document.party.rooms} 间房`} · 中国当地时间</p>
      {document.items.map((item) => <div key={item.id} className="plan-item"><time>{planTime(item.start)} — {planTime(item.end)}</time>
        <strong>{item.title}</strong>{item.protection && <span className="plan-lock">{lockText(item.protection.state)}</span>}{item.description && <p>{item.description}</p>}
      </div>)}
      {document.lodging.map((stay) => <p key={stay.id}><strong>{stay.title ?? '住宿安排'}</strong> · {stay.check_in} 至 {stay.check_out}{stay.protection && <span className="plan-lock">{lockText(stay.protection.state)}</span>}</p>)}
      <details open><summary>费用与待确认项</summary>
        <p>{selected.review.known_cost_totals.length ? selected.review.known_cost_totals.map((t) => `${t.currency} ${t.amount}`).join(' / ') + ' · 已知小计，未必是完整总价' : '尚无已知费用'}</p>
        <ul>{document.costs.map((cost) => <li key={cost.id}><strong>{cost.title}</strong>：{cost.total ? `${cost.total.currency} ${cost.total.amount}` : '费用未知，未按零计'}
          {cost.total && (cost.kind === 'reference' ? ' · 参考金额' : ' · 查询时金额，当前适用性见复核项')}
          {cost.basis && <span> · {cost.basis.travelers ?? '未知'} 人{cost.basis.rooms ? ` / ${cost.basis.rooms} 间` : ''}{cost.basis.nights ? ` / ${cost.basis.nights} 晚` : ''} · {cost.basis.calculation}</span>}
          <span> · 税{cost.tax_basis === 'included' ? '已含' : '未完全确认'} / 附加费{cost.fee_basis === 'included' ? '已含' : '未完全确认'}</span></li>)}</ul>
        {pendingGroups.length > 0 && <ul className="plan-pending">{pendingGroups.map((p) => <li key={p.message}>{p.message}{p.count > 1 && `（${p.count} 项）`}</li>)}</ul>}
        {unknown.length > 0 && <p className="plan-notice">待核实：{unknown.join('、')}。</p>}
      </details>
      {selected.base_version_id && <details open><summary>本次修改与费用变化</summary>
        <ul>{selected.diff.changes.map((change, i) => <li key={i}><strong>{operations[change.operation]}{sections[change.section] ?? '安排'}</strong><span>{changeText(change.section, change.before)} → {changeText(change.section, change.after)}</span></li>)}</ul>
        {selected.diff.cost_deltas.map((d) => <p key={d.currency}>{d.currency} 已知小计：{d.before ?? '未知'} → {d.after ?? '未知'}{d.delta !== null && `（变化 ${d.delta}）`}。不包含未知费用。</p>)}
      </details>}
      {document.requirements.length > 0 && <details><summary>当前条件与假设</summary><ul>{document.requirements.filter((r) => r.state === 'active').map((r) => <li key={r.id}>{origins[r.origin]} · {r.statement}</li>)}</ul></details>}
      {Object.keys(document.source_catalog).length > 0 && <details><summary>来源与查询时间</summary><ul>{Object.entries(document.source_catalog).map(([id, source]) => {
        const report = selected.review.evidence[id];
        const href = source.url && /^https?:\/\//i.test(source.url) ? source.url : null;
        return <li key={id}>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{source.provider}</a> : source.provider}
          <span> · {report?.state === 'observed' ? '查询于' : report?.state === 'user_reported' ? '记录于' : report?.state === 'assumption' ? '参考记录时间' : '来源声明时间（未核实）'} {planTime(source.retrieved_at)}{source.data_time && ` · 数据时次 ${planTime(source.data_time)}`}</span>
          {report?.snapshot_only && <span> · 查询时快照，临近出行需复核</span>}{report?.reasons.map((reason, i) => <span className="plan-notice" key={i}> · {reason}</span>)}</li>;
      })}</ul></details>}
    </div>}
  </section>;
}
