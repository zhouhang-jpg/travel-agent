import { useEffect, useRef, useState } from 'react';
import { api, APIError } from './api';
import type { PreferenceInput, PreferenceProfile, PreferenceUpdate } from './types';

export function preferenceError(error: unknown) {
  if (error instanceof APIError) {
    if (error.status === 503) return '偏好服务暂时不可用，请稍后重新载入。';
    if (error.code === 'preferences_busy') return '有会话正在执行，请等待本轮结束后再修改偏好。';
    if (error.code === 'unknown_preference_targets') return error.message;
    if (error.status === 409) return '偏好或建议已发生变化，本次操作未保存。请载入最新版本后重新确认，不会自动覆盖。';
  }
  return error instanceof TypeError ? '暂时无法连接服务。请重新载入偏好后再试。'
    : error instanceof Error ? error.message : '偏好操作失败，请稍后再试。';
}

export function isPreferenceConflict(error: unknown) {
  return error instanceof APIError && error.status === 409
    && error.code !== 'preferences_busy' && error.code !== 'unknown_preference_targets';
}

/** Refresh conflicts for review, but never retry an action the user has not reconfirmed. */
export async function mutatePreferences<T>(operation: () => Promise<T>, refresh: () => Promise<unknown>) {
  try { return await operation(); }
  catch (error) {
    if (isPreferenceConflict(error)) {
      try { await refresh(); } catch { /* Keep the original conflict and require explicit reload. */ }
    }
    throw error;
  }
}

const rowsOf = (profile: PreferenceProfile | null): PreferenceInput[] =>
  profile?.entries.map(({ id, label, value }) => ({ id, label, value })) ?? [];

export function createPreferenceInput(uuid: string = crypto.randomUUID()): PreferenceInput {
  // PreferenceId starts with a lowercase letter; a raw UUID frequently starts with a digit.
  return { id: `pref_${uuid}`, label: '', value: '' };
}

export function buildPreferenceUpdate(profile: PreferenceProfile, rows: PreferenceInput[], enabled: boolean): PreferenceUpdate {
  const normalized = rows.map(({ id, label, value }) => ({ id, label: label.trim(), value: value.trim() }));
  if (normalized.some((row) => !row.id || !row.label || !row.value)) {
    throw new Error('请为每条偏好填写名称和内容，或删除空白条目。');
  }
  if (new Set(normalized.map((row) => row.id)).size !== normalized.length) {
    throw new Error('偏好条目重复，请重新载入后编辑。');
  }
  const previous = new Map(profile.entries.map((row) => [row.id, row]));
  return {
    base_revision: profile.revision, enabled,
    upsert: normalized.filter((row) => {
      const old = previous.get(row.id);
      return !old || old.label !== row.label || old.value !== row.value;
    }),
    remove_ids: profile.entries.filter((row) => !normalized.some((next) => next.id === row.id)).map((row) => row.id),
  };
}

interface PreferencePanelProps {
  profile: PreferenceProfile | null;
  loadError: string;
  busy: boolean;
  onRefresh: () => Promise<PreferenceProfile>;
  onChanged: (profile: PreferenceProfile) => void;
  onMutating: (pending: boolean) => void;
  onClose: () => void;
}

export function PreferencePanel({ profile, loadError, busy, onRefresh, onChanged, onMutating, onClose }: PreferencePanelProps) {
  const [base, setBase] = useState(profile);
  const [rows, setRows] = useState(() => rowsOf(profile));
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [clearConfirm, setClearConfirm] = useState(false);
  const locked = useRef(false);
  const closeButton = useRef<HTMLButtonElement>(null);
  const dirty = !!base && (enabled !== base.enabled || JSON.stringify(rows) !== JSON.stringify(rowsOf(base)));
  const disabled = busy || saving || reloading || !base;

  const load = (next: PreferenceProfile) => {
    setBase(next); setRows(rowsOf(next)); setEnabled(next.enabled);
    setConflict(false); setClearConfirm(false); setError('');
  };

  useEffect(() => {
    closeButton.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [onClose]);

  useEffect(() => {
    if (!profile || profile.revision === base?.revision) return;
    if (base && dirty) {
      setConflict(true);
      setError('已保存偏好有更新。你的编辑尚未保存，请载入最新版本后重新编辑。');
    } else {
      load(profile);
    }
  }, [profile, base, dirty]);

  const reload = async () => {
    setReloading(true); setNotice('');
    try { load(await onRefresh()); }
    catch (reason) { setError(preferenceError(reason)); }
    finally { setReloading(false); }
  };

  const mutate = async (clear = false) => {
    if (disabled || conflict || locked.current || !base) return;
    locked.current = true; setSaving(true); onMutating(true); setError(''); setNotice('');
    try {
      const update = clear ? null : buildPreferenceUpdate(base, rows, enabled);
      const next = await mutatePreferences(
        () => clear ? api.clearPreferences(base.revision) : api.updatePreferences(update!), onRefresh,
      );
      load(next); onChanged(next);
      setNotice(clear ? '已清空所有已保存偏好，使用开关保持不变。' : '偏好已保存，之后的规划会按使用开关读取。');
    } catch (reason) {
      setError(preferenceError(reason));
      if (isPreferenceConflict(reason)) setConflict(true);
    } finally {
      locked.current = false; setSaving(false); onMutating(false);
    }
  };

  const changeRow = (id: string, field: 'label' | 'value', value: string) => {
    setRows((previous) => previous.map((row) => row.id === id ? { ...row, [field]: value } : row));
    setNotice('');
  };

  return <aside className="preference-panel" aria-label="出行偏好">
    <header className="preference-header"><div><span>为之后的旅程保留</span><h2>出行偏好</h2></div>
      <button ref={closeButton} className="icon-button" type="button" aria-label="关闭出行偏好" onClick={onClose}>×</button></header>
    <div className="preference-body">
      <p className="preference-help">保存常用出发地、旅行节奏或任何长期偏好；实时票价、天气等信息仍需每次查询。</p>
      {!base && <p role={loadError ? 'alert' : 'status'}>{loadError || '正在载入偏好…'}</p>}
      {base && loadError && <p className="preference-error" role="alert">{loadError}</p>}
      {busy && <p className="preference-notice" role="status">有会话正在执行，结束后可以保存、清空或切换偏好。</p>}
      <form onSubmit={(event) => { event.preventDefault(); void mutate(); }}>
        <label className="preference-toggle"><input type="checkbox" checked={enabled} disabled={disabled}
          onChange={(event) => { setEnabled(event.target.checked); setNotice(''); }} />
          <span><strong>使用已保存偏好</strong><small>作为以后规划的参考，本次明确要求优先。</small></span></label>
        <p className="preference-help">修改条目或开关后，点击「保存偏好」生效。</p>
        {base && rows.length === 0 && <p className="preference-empty">还没有偏好条目。你可以添加任何想长期保留的习惯或限制。</p>}
        <div className="preference-entries">{rows.map((row, index) => <fieldset className="preference-row" key={row.id} disabled={disabled}>
          <legend className="sr-only">偏好条目 {index + 1}</legend>
          <label htmlFor={`preference-label-${row.id}`}>偏好名称</label>
          <input id={`preference-label-${row.id}`} value={row.label} placeholder="例如：常用出发地 / 旅行节奏"
            required onChange={(event) => changeRow(row.id, 'label', event.target.value)} />
          <label htmlFor={`preference-value-${row.id}`}>偏好内容</label>
          <textarea id={`preference-value-${row.id}`} value={row.value} rows={2} placeholder="写下自己的偏好，不限于示例"
            required onChange={(event) => changeRow(row.id, 'value', event.target.value)} />
          <button type="button" className="preference-remove" aria-label={`删除偏好${row.label || `条目 ${index + 1}`}`}
            onClick={() => { setRows((previous) => previous.filter((entry) => entry.id !== row.id)); setNotice(''); }}>删除这一条</button>
        </fieldset>)}</div>
        <button className="preference-secondary" type="button" disabled={disabled} onClick={() => {
          setRows((previous) => [...previous, createPreferenceInput()]); setNotice('');
        }}>＋ 添加偏好</button>
        {error && <p className="preference-error" role="alert">{error}</p>}
        {notice && <p className="preference-success" role="status">{notice}</p>}
        {conflict && <p className="preference-help">载入最新版本会替换这里尚未保存的编辑，请重新检查后再保存。</p>}
        <div className="preference-actions"><button className="preference-primary" type="submit" disabled={disabled || !dirty || conflict}>
          {saving ? '正在保存…' : '保存偏好'}</button>
          <button className="preference-secondary" type="button" disabled={saving || reloading} onClick={() => void reload()}>
            {reloading ? '正在载入…' : '载入最新版本'}</button></div>
      </form>
      <div className="preference-clear">
        {clearConfirm ? <><p>清空所有已保存条目？使用开关会保留。</p><div className="preference-actions">
          <button className="preference-danger" type="button" disabled={disabled || conflict} onClick={() => void mutate(true)}>确认清空</button>
          <button className="preference-secondary" type="button" disabled={saving} onClick={() => setClearConfirm(false)}>保留偏好</button></div></>
          : <button type="button" className="preference-remove" disabled={disabled || !base?.entries.length || conflict} onClick={() => setClearConfirm(true)}>清空所有偏好</button>}
      </div>
    </div>
  </aside>;
}
