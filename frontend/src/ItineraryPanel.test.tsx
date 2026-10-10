import { expect, test } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { changeText, ItineraryPanel } from './ItineraryPanel';
import type { ItineraryVersion } from './types';

test('unknown cost never displays as zero; booked stays remain user claims', () => {
  const version: ItineraryVersion = {
    id: 'v1', revision: 1, base_version_id: null, created_at: '', change_reason: '保留固定住宿',
    document: { title: '测试行程', party: { travelers: 2, rooms: 1 }, items: [],
      lodging: [{ id: 'hotel', title: '酒店A', check_in: '2026-10-12', check_out: '2026-10-13', protection: { state: 'user_reported_booked' } }],
      costs: [{ id: 'train', title: '返程费用', total: null, kind: 'unknown', tax_basis: 'unknown', fee_basis: 'unknown', basis: null }],
      requirements: [], source_catalog: {} },
    review: { status: 'unknown', known_cost_totals: [], unknown_cost_ids: ['train'], pending: [], checks: [], evidence: {} },
    diff: { changes: [], cost_deltas: [] },
  };
  const html = renderToStaticMarkup(<ItineraryPanel conversationId="chat" current={version} />);
  expect(html).toContain('费用未知，未按零计');
  expect(html).toContain('你告知已预订 · 未核验');
  expect(html).not.toContain('CNY 0');
});

test('version differences describe user-visible time and price changes', () => {
  expect(changeText('items', { title: '室内展览', start: '2026-10-13T14:00:00+08:00', end: '2026-10-13T16:00:00+08:00' })).toContain('14:00');
  expect(changeText('costs', { title: '门票', total: { amount: '50', currency: 'CNY' } })).toBe('门票 · CNY 50');
  expect(changeText('costs', { title: '返程', total: null })).toBe('返程 · 费用未知');
});
