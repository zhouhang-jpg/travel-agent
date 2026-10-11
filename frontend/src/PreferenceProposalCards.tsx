import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { mutatePreferences, preferenceError } from './PreferencePanel';
import type { PreferenceProfile, PreferenceProposal } from './types';

interface CardProps {
  proposal: PreferenceProposal;
  profile: PreferenceProfile | null;
  disabled: boolean;
  applyDisabled: boolean;
  dismissDisabled: boolean;
  pending: boolean;
  onApply: () => void;
  onDismiss: () => void;
}

export function PreferenceProposalCard({ proposal, profile, disabled, applyDisabled, dismissDisabled, pending, onApply, onDismiss }: CardProps) {
  const entries = new Map(profile?.entries.map((entry) => [entry.id, entry]) ?? []);
  const stale = !!profile && proposal.base_revision !== profile.revision;
  return <article className="preference-proposal">
    <div className="preference-proposal-title"><strong>待保存的出行偏好</strong><span>需要你确认</span></div>
    <p>根据你在这次对话中的表达，助手建议保留以下修改：</p>
    <blockquote>{proposal.user_reference.quote}</blockquote>
    <ul>{proposal.patch.upsert.map((entry) => {
      const old = entries.get(entry.id);
      return <li key={`upsert-${entry.id}`}><strong>{old ? '修改' : '新增'} · {entry.label}</strong>
        {old && <p className="preference-before">之前：{old.label} · {old.value}</p>}
        <p>{entry.value}</p></li>;
    })}{proposal.patch.remove_ids.map((id) => <li key={`remove-${id}`}>
      <strong>删除 · {entries.get(id)?.label ?? '已保存条目'}</strong>
      {entries.get(id) && <p>{entries.get(id)!.value}</p>}</li>)}</ul>
    {stale && <p className="preference-notice">这份建议基于旧版偏好，已不能直接保存。请重新确认你要保留的内容。</p>}
    <div className="preference-actions"><button type="button" className="preference-primary" disabled={disabled || applyDisabled || stale || !profile} onClick={onApply}>
      {pending ? '正在处理…' : '保存为长期偏好'}</button>
      <button type="button" className="preference-secondary" disabled={disabled || dismissDisabled} onClick={onDismiss}>忽略这份建议</button></div>
    <p className="preference-help">点击保存后才会加入长期偏好，聊天记录按普通会话保留。</p>
  </article>;
}

interface ProposalCardsProps {
  conversationId: string;
  refreshKey: string;
  profile: PreferenceProfile | null;
  busy: boolean;
  conversationBusy: boolean;
  onRefreshProfile: () => Promise<PreferenceProfile>;
  onChanged: (profile: PreferenceProfile) => void;
  onMutating: (pending: boolean) => void;
}

export function PreferenceProposalCards({ conversationId, refreshKey, profile, busy, conversationBusy, onRefreshProfile, onChanged, onMutating }: ProposalCardsProps) {
  const [items, setItems] = useState<PreferenceProposal[]>([]);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const lock = useRef(false);
  const requestSequence = useRef(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requestSequence.current++; }; }, []);

  const refresh = useCallback(async () => {
    const sequence = ++requestSequence.current;
    try {
      const response = await api.preferenceProposals(conversationId);
      if (mounted.current && sequence === requestSequence.current) {
        setItems(response.items.filter((item) => item.status === 'pending')); setLoadError('');
      }
    } catch (reason) {
      if (mounted.current && sequence === requestSequence.current) setLoadError(preferenceError(reason));
      throw reason;
    }
  }, [conversationId]);

  useEffect(() => {
    void refresh().catch(() => undefined);
  }, [refresh, refreshKey, profile?.revision]);

  const act = async (proposal: PreferenceProposal, apply: boolean) => {
    if ((apply ? busy || !profile : conversationBusy) || lock.current) return;
    lock.current = true; setPending(proposal.id); onMutating(true); setError(''); setNotice('');
    try {
      await mutatePreferences(async () => {
        if (apply) {
          // Preserve the proposal's reviewed revision; never substitute the newest revision.
          const next = await api.applyPreferenceProposal(conversationId, proposal.id, proposal.base_revision);
          onChanged(next);
        } else { await api.dismissPreferenceProposal(conversationId, proposal.id); }
      }, async () => { await Promise.all([onRefreshProfile(), refresh()]); });
      if (mounted.current) {
        setItems((previous) => previous.filter((item) => item.id !== proposal.id));
        setNotice(apply ? '已保存为长期偏好，可在「出行偏好」中查看和修改。' : '已忽略这份建议，没有修改长期偏好。');
      }
      try { await refresh(); } catch { /* The successful action is already reflected locally. */ }
    } catch (reason) { if (mounted.current) setError(preferenceError(reason)); }
    finally { lock.current = false; if (mounted.current) setPending(null); onMutating(false); }
  };

  if (!items.length && !error && !loadError && !notice) return null;
  return <section className="preference-proposals" aria-label="待确认的偏好建议">
    {busy && items.length > 0 && <p className="preference-notice">有会话正在执行，结束后可以保存长期偏好。</p>}
    {items.map((proposal) => <PreferenceProposalCard key={proposal.id} proposal={proposal} profile={profile}
      disabled={pending !== null} applyDisabled={busy} dismissDisabled={conversationBusy} pending={pending === proposal.id}
      onApply={() => void act(proposal, true)} onDismiss={() => void act(proposal, false)} />)}
    {(error || loadError) && <p className="preference-error" role="alert">{error || loadError} <button type="button" className="preference-remove"
      onClick={() => { setError(''); void Promise.all([onRefreshProfile(), refresh()]).catch((reason) => setError(preferenceError(reason))); }}>刷新偏好建议</button></p>}
    {notice && <p className="preference-success" role="status">{notice}</p>}
  </section>;
}
