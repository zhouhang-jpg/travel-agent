import { expect, test, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { APIError } from './api';
import { buildPreferenceUpdate, createPreferenceInput, mutatePreferences, preferenceError, PreferencePanel } from './PreferencePanel';
import { PreferenceProposalCard } from './PreferenceProposalCards';
import type { PreferenceProfile, PreferenceProposal } from './types';

const profile: PreferenceProfile = {
  owner_id: 'local-user', revision: 4, enabled: true, updated_at: null,
  entries: [
    { id: 'pace', label: '旅行节奏', value: '轻松一些', updated_at: '2026-10-11T08:00:00Z', source: { kind: 'manual' } },
    { id: 'origin', label: '出发地', value: '上海', updated_at: '2026-10-11T08:00:00Z', source: { kind: 'conversation', quote: '平时从上海出发' } },
  ],
};

const proposal: PreferenceProposal = {
  id: 'proposal-1', conversation_id: 'chat', base_revision: 4, status: 'pending',
  patch: { upsert: [{ id: 'pace', label: '旅行节奏', value: '一天只安排两个主要活动' }], remove_ids: ['origin'] },
  user_reference: { message_id: 'user-1', quote: '以后都一天只安排两个主要活动，不再固定上海出发' },
};

test('new preference IDs satisfy the backend contract even for digit-leading UUIDs', () => {
  const row = createPreferenceInput('01234567-89ab-4cde-8fab-0123456789ab');
  expect(row.id).toBe('pref_01234567-89ab-4cde-8fab-0123456789ab');
  expect(row.id).toMatch(/^[a-z][a-z0-9_-]*$/);
  expect(row.label).toBe('');
  expect(row.value).toBe('');
});

test('editing sends only explicit differences and preserves unchanged provenance on the server', () => {
  const update = buildPreferenceUpdate(profile, [
    { id: 'pace', label: ' 旅行节奏 ', value: ' 轻松一些 ' },
    { id: 'food', label: ' 饮食习惯 ', value: ' 不吃辣 ' },
  ], false);
  expect(update).toEqual({ base_revision: 4, enabled: false,
    upsert: [{ id: 'food', label: '饮食习惯', value: '不吃辣' }], remove_ids: ['origin'] });
  expect(profile.entries[1].source.kind).toBe('conversation');
});

test('free preference entries still require readable labels and values', () => {
  expect(() => buildPreferenceUpdate(profile, [{ id: 'a', label: ' ', value: '任意偏好' }], true)).toThrow('名称和内容');
  expect(() => buildPreferenceUpdate(profile, [{ id: 'a', label: '新偏好', value: ' ' }], true)).toThrow('名称和内容');
  expect(() => buildPreferenceUpdate(profile, [
    { id: 'a', label: '节奏', value: '轻松' }, { id: 'a', label: '住宿', value: '安静' },
  ], true)).toThrow('重复');
});

test('revision and stale-proposal conflicts refresh but never reapply unconfirmed changes', async () => {
  for (const code of ['preference_revision_conflict', 'preference_proposal_stale', 'preference_proposal_closed']) {
    const error = new APIError(409, '已变化', code);
    const operation = vi.fn().mockRejectedValue(error);
    const refresh = vi.fn().mockResolvedValue(profile);
    await expect(mutatePreferences(operation, refresh)).rejects.toBe(error);
    expect(operation).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(preferenceError(error)).toContain('重新确认');
  }
});

test('a failed refresh keeps the original conflict; running state is not treated as stale editing', async () => {
  const original = new APIError(409, '版本已变化', 'preference_revision_conflict');
  await expect(mutatePreferences(async () => { throw original; }, async () => { throw new TypeError('offline'); })).rejects.toBe(original);
  const refresh = vi.fn();
  const busy = new APIError(409, '执行中', 'preferences_busy');
  await expect(mutatePreferences(async () => { throw busy; }, refresh)).rejects.toBe(busy);
  expect(refresh).not.toHaveBeenCalled();
  expect(preferenceError(busy)).toContain('结束后');
  const unknown = new APIError(409, '要删除的偏好不存在，请检查条目。', 'unknown_preference_targets');
  await expect(mutatePreferences(async () => { throw unknown; }, refresh)).rejects.toBe(unknown);
  expect(refresh).not.toHaveBeenCalled();
  expect(preferenceError(unknown)).toBe(unknown.message);
});

test('saved preferences are discoverable, editable as arbitrary entries, and locked while running', () => {
  const html = renderToStaticMarkup(<PreferencePanel profile={profile} loadError="" busy
    onRefresh={async () => profile} onChanged={() => undefined} onMutating={() => undefined} onClose={() => undefined} />);
  expect(html).toContain('aria-label="出行偏好"');
  expect(html).toContain('关闭出行偏好');
  expect(html).toContain('使用已保存偏好');
  expect(html).toContain('旅行节奏');
  expect(html).toContain('轻松一些');
  expect(html).toContain('清空所有偏好');
  expect(html).toContain('实时票价、天气');
  expect(html).toContain('结束后可以保存');
  expect(html).toMatch(/<fieldset[^>]*disabled/);
  expect(html).toMatch(/<input[^>]*type="checkbox"[^>]*disabled/);
  expect(html).not.toContain('登录');
});

test('pending cards show the actual quoted user text and proposed changes with explicit confirmation', () => {
  const html = renderToStaticMarkup(<PreferenceProposalCard proposal={proposal} profile={profile}
    disabled={false} applyDisabled={false} dismissDisabled={false} pending={false} onApply={() => undefined} onDismiss={() => undefined} />);
  expect(html).toContain(proposal.user_reference.quote);
  expect(html).toContain('修改 · 旅行节奏');
  expect(html).toContain('之前：旅行节奏 · 轻松一些');
  expect(html).toContain('一天只安排两个主要活动');
  expect(html).toContain('删除 · 出发地');
  expect(html).toContain('保存为长期偏好');
  expect(html).toContain('忽略这份建议');
  expect(html).toContain('聊天记录按普通会话保留');
});

test('other running conversations block apply but still allow ignoring this idle conversation suggestion', () => {
  const html = renderToStaticMarkup(<PreferenceProposalCard proposal={proposal} profile={profile}
    disabled={false} applyDisabled dismissDisabled={false} pending={false} onApply={() => undefined} onDismiss={() => undefined} />);
  expect(html).toMatch(/<button[^>]*disabled[^>]*>保存为长期偏好/);
  expect(html).toMatch(/<button type="button" class="preference-secondary">忽略这份建议/);
});

test('stale suggestions cannot save against the latest profile revision and render external text safely', () => {
  const html = renderToStaticMarkup(<PreferenceProposalCard proposal={{ ...proposal,
    user_reference: { message_id: 'user-1', quote: '<script>saveEverything()</script>' } }}
    profile={{ ...profile, revision: 5 }} disabled={false} applyDisabled={false} dismissDisabled={false} pending={false}
    onApply={() => undefined} onDismiss={() => undefined} />);
  expect(html).toContain('基于旧版偏好');
  expect(html).toMatch(/<button[^>]*disabled[^>]*>保存为长期偏好/);
  expect(html).not.toContain('<script>');
  expect(html).toContain('&lt;script&gt;');
});
