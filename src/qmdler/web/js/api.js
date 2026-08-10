// SPDX-License-Identifier: GPL-3.0-or-later
// REST 客户端。WebUI 与 TUI 调用的是同一组端点。

const BASE = '/api';

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(BASE + path, options);
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_) {
      payload = text;
    }
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : response.statusText;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  return payload;
}

export const api = {
  health: () => request('GET', '/health'),
  snapshot: () => request('GET', '/snapshot'),

  // 登录
  authState: () => request('GET', '/auth/state'),
  profile: (refresh = false) => request('GET', `/auth/profile?refresh=${refresh}`),
  startQrcode: (loginType) => request('POST', '/auth/qrcode', { login_type: loginType }),
  qrcodeState: (id) => request('GET', `/auth/qrcode/${id}`),
  refreshQrcode: (id) => request('POST', `/auth/qrcode/${id}/refresh`),
  cancelQrcode: (id) => request('DELETE', `/auth/qrcode/${id}`),
  sendPhoneCode: (phone, countryCode) =>
    request('POST', '/auth/phone/code', { phone, country_code: countryCode }),
  verifyPhoneCode: (sessionId, code) =>
    request('POST', '/auth/phone/verify', { session_id: sessionId, code }),
  importCredential: (payload) => request('POST', '/auth/credential', payload),
  importCredentialFile: (path) => request('POST', '/auth/credential/file', { path }),
  refreshCredential: () => request('POST', '/auth/refresh'),
  logout: () => request('POST', '/auth/logout'),

  // 来源
  resolveLink: (text) => request('POST', '/sources/resolve', { text }),
  createdSonglists: () => request('GET', '/sources/created'),
  favSonglists: () => request('GET', '/sources/fav-songlists'),
  fetchSource: (payload) => request('POST', '/sources/fetch', payload),

  // 任务
  listTasks: () => request('GET', '/tasks'),
  createTask: (payload) => request('POST', '/tasks', payload),
  getTask: (id) => request('GET', `/tasks/${id}`),
  listItems: (id, params = '') => request('GET', `/tasks/${id}/items${params}`),
  setSelection: (id, itemIds, selected) =>
    request('POST', `/tasks/${id}/selection`, { item_ids: itemIds, selected }),
  invertSelection: (id) => request('POST', `/tasks/${id}/selection/invert`),
  filterSelection: (id, keyword, selectMatched) =>
    request('POST', `/tasks/${id}/selection/filter`, { keyword, select_matched: selectMatched }),
  startTask: (id) => request('POST', `/tasks/${id}/start`),
  pauseTask: (id) => request('POST', `/tasks/${id}/pause`),
  cancelTask: (id) => request('POST', `/tasks/${id}/cancel`),
  skipWait: (id) => request('POST', `/tasks/${id}/skip-wait`),
  retryFailed: (id) => request('POST', `/tasks/${id}/retry-failed`),
  report: (id) => request('GET', `/tasks/${id}/report`),
  deleteTask: (id) => request('DELETE', `/tasks/${id}`),
  reportCsvUrl: (id) => `${BASE}/tasks/${id}/report.csv`,

  // 配置
  getConfig: () => request('GET', '/config'),
  patchConfig: (patch) => request('PATCH', '/config', { patch }),
  presets: () => request('GET', '/config/presets'),
  activatePreset: (name) => request('POST', '/config/presets/activate', { name }),
  savePreset: (name, overrides) => request('POST', '/config/presets', { name, overrides }),
  qualities: () => request('GET', '/config/qualities'),
  templates: () => request('GET', '/config/templates'),
  previewTemplate: (payload) => request('POST', '/config/templates/preview', payload),
  checkPath: (path, create = false) => request('POST', '/config/path/check', { path, create }),
};
