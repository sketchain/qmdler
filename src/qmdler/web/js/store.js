// SPDX-License-Identifier: GPL-3.0-or-later
// 全局状态。不引入 Pinia，reactive() 足够。

const { reactive } = Vue;

export const store = reactive({
  connected: false,
  tab: 'sources', // sources | tracks | tasks | settings（窄屏底部标签栏用）
  auth: { logged_in: false, needs_login: false, profile: {}, expires_at: null, expiry_known: false },
  engine: { running: false, phase: 'idle', counts: {}, waiting: false, countdown: 0, countdown_total: 0 },
  settings: null,
  qualities: [],
  qualityNotes: {},
  templates: { placeholders: {}, combos: [], hint: '' },

  // 来源
  createdSonglists: [],
  favSonglists: [],
  sourceResult: null,
  sourceItems: [],
  selectedMids: new Set(),
  filterKeyword: '',

  // 任务
  tasks: [],
  activeTaskId: '',
  items: [],
  report: null,

  log: [],
  toasts: [],
});

let toastId = 0;

export function toast(message, level = 'info') {
  const id = ++toastId;
  store.toasts.push({ id, message, level });
  setTimeout(() => {
    const index = store.toasts.findIndex((entry) => entry.id === id);
    if (index >= 0) store.toasts.splice(index, 1);
  }, level === 'error' ? 9000 : 4500);
}

export function pushLog(line) {
  store.log.push(line);
  if (store.log.length > 400) store.log.splice(0, store.log.length - 400);
}

export function applyEvent(frame) {
  const { kind, payload } = frame;

  if (kind === 'snapshot') {
    if (payload.auth) store.auth = payload.auth;
    if (payload.engine) store.engine = payload.engine;
    if (payload.settings) store.settings = payload.settings;
    return;
  }
  if (kind === 'auth_state') {
    store.auth = payload;
    return;
  }
  if (kind === 'config_changed') {
    store.settings = payload;
    return;
  }
  if (kind === 'task_state') {
    if (payload.engine) store.engine = payload.engine;
    if (payload.status && frame.task_id) {
      const task = store.tasks.find((entry) => entry.id === frame.task_id);
      if (task) task.status = payload.status;
    }
    return;
  }
  if (kind === 'countdown') {
    store.engine.waiting = true;
    store.engine.countdown = payload.remaining;
    store.engine.countdown_total = payload.total;
    return;
  }
  if (kind === 'item_state') {
    const index = store.items.findIndex((entry) => entry.id === payload.id);
    if (index >= 0) store.items.splice(index, 1, payload);
    return;
  }
  if (kind === 'progress') {
    const item = store.items.find((entry) => entry.id === frame.item_id);
    if (item) {
      item.bytes_downloaded = payload.written;
      if (payload.total) item.bytes_expected = payload.total;
      item.progress = payload.total ? payload.written / payload.total : 0;
    }
    return;
  }
  if (kind === 'log') {
    pushLog({ ts: frame.ts, level: frame.level, message: payload.message });
    if (frame.level === 'error') toast(payload.message, 'error');
    return;
  }
  if (kind === 'report') {
    toast('任务结束，汇总报告已生成', 'info');
    return;
  }
  if (kind === 'login_event') {
    store.lastLoginEvent = payload;
  }
}

export function formatBytes(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function formatDuration(seconds) {
  if (!seconds || seconds < 0) return '—';
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, '0')}`;
}

export const STATUS_META = {
  pending: { label: '待下载', cls: 'badge--muted' },
  downloading: { label: '下载中', cls: 'badge--info' },
  success: { label: '成功', cls: 'badge--ok' },
  failed: { label: '失败', cls: 'badge--danger' },
  skipped: { label: '已跳过', cls: 'badge--muted' },
  unavailable: { label: '受限', cls: 'badge--warn' },
  trial: { label: '试听片段', cls: 'badge--trial' },
};
