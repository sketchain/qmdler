// 登录：五种方式并列可选。

import { api } from '../api.js';
import { store, toast } from '../store.js';

const { reactive, computed, onUnmounted, watch } = Vue;

const QR_STATUS_TEXT = {
  waiting: '等待扫描',
  scanned: '已扫描，请在手机上确认',
  confirmed: '已确认',
  done: '登录成功',
  refused: '已在手机上拒绝登录',
  timeout: '二维码已过期',
  error: '出错了',
};

export const LoginView = {
  name: 'LoginView',
  setup() {
    const state = reactive({
      method: '',
      qr: null,
      phone: { number: '', countryCode: 86, sessionId: '', status: '', message: '', captchaUrl: '', code: '' },
      credential: { musicid: '', musickey: '', refresh_key: '', refresh_token: '', encrypt_uin: '', path: '' },
      busy: false,
    });

    // 扫码状态由 WS 推送，这里只跟踪当前会话。
    watch(
      () => store.lastLoginEvent,
      (event) => {
        if (!event) return;
        if (event.kind === 'qrcode' && state.qr && event.session_id === state.qr.session_id) {
          Object.assign(state.qr, event);
        }
        if (event.kind === 'phone' && event.session_id === state.phone.sessionId) {
          state.phone.status = event.status;
          state.phone.message = event.message;
          state.phone.captchaUrl = event.captcha_url || '';
        }
      },
    );

    async function startQr(type) {
      state.method = type;
      state.busy = true;
      try {
        if (state.qr) await api.cancelQrcode(state.qr.session_id).catch(() => {});
        state.qr = await api.startQrcode(type);
      } catch (error) {
        toast(`获取二维码失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function refreshQr() {
      if (!state.qr) return;
      state.busy = true;
      try {
        state.qr = await api.refreshQrcode(state.qr.session_id);
      } catch (error) {
        toast(`刷新二维码失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function sendCode() {
      state.busy = true;
      try {
        const result = await api.sendPhoneCode(Number(state.phone.number), state.phone.countryCode);
        state.phone.sessionId = result.session_id;
        state.phone.status = result.status;
        state.phone.message = result.message;
        state.phone.captchaUrl = result.captcha_url || '';
      } catch (error) {
        toast(`发送验证码失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function verifyCode() {
      state.busy = true;
      try {
        const result = await api.verifyPhoneCode(state.phone.sessionId, state.phone.code);
        state.phone.status = result.status;
        state.phone.message = result.message;
        if (result.status === 'done') toast('登录成功');
      } catch (error) {
        toast(`验证失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function importCredential() {
      state.busy = true;
      try {
        const payload = {
          musicid: Number(state.credential.musicid),
          musickey: state.credential.musickey.trim(),
        };
        ['refresh_key', 'refresh_token', 'encrypt_uin'].forEach((key) => {
          if (state.credential[key]) payload[key] = state.credential[key].trim();
        });
        await api.importCredential(payload);
        toast('凭证已导入');
      } catch (error) {
        toast(`导入失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function importFile() {
      state.busy = true;
      try {
        await api.importCredentialFile(state.credential.path.trim());
        toast('凭证文件已导入');
      } catch (error) {
        toast(`导入失败：${error.message}`, 'error');
      } finally {
        state.busy = false;
      }
    }

    async function logout() {
      await api.logout();
      state.qr = null;
      toast('已登出');
    }

    async function refreshCredential() {
      try {
        await api.refreshCredential();
        toast('凭证已刷新');
      } catch (error) {
        toast(`刷新失败：${error.message}`, 'error');
      }
    }

    const qrText = computed(() => (state.qr ? QR_STATUS_TEXT[state.qr.status] || state.qr.message : ''));
    const qrImage = computed(() =>
      state.qr && state.qr.session_id ? `/api/auth/qrcode/${state.qr.session_id}/image` : '',
    );
    const vip = computed(() => (store.auth.profile && store.auth.profile.vip) || {});

    onUnmounted(() => {
      if (state.qr) api.cancelQrcode(state.qr.session_id).catch(() => {});
    });

    return { state, store, qrText, qrImage, vip, startQr, refreshQr, sendCode, verifyCode, importCredential, importFile, logout, refreshCredential };
  },
  template: `
    <div>
      <div v-if="store.auth.logged_in" class="card">
        <div class="row">
          <strong>{{ store.auth.profile.nickname || ('musicid ' + store.auth.musicid) }}</strong>
          <span v-if="vip.svip" class="badge badge--ok">超级会员</span>
          <span v-else-if="vip.huge_vip" class="badge badge--ok">豪华绿钻</span>
          <span v-else-if="vip.vip" class="badge badge--info">绿钻</span>
          <span v-else class="badge badge--muted">普通用户</span>
          <span v-if="vip.level" class="badge badge--muted">Lv.{{ vip.level }}</span>
        </div>
        <p class="muted" style="margin:8px 0 0">
          会员等级不是万能的：单曲付费、该音质文件本就不存在、地区版权限制、设备受限，
          这几种情况与会员等级无关。
        </p>
        <p class="faint" v-if="!store.auth.has_encrypt_uin">
          ⚠ 当前凭证缺少 encrypt_uin，「我喜欢」与「收藏的歌单」不可用。
        </p>
        <p class="faint" v-if="store.auth.expiry_known && store.auth.expires_at">
          凭证过期时间：{{ new Date(store.auth.expires_at * 1000).toLocaleString() }}（后台会自动提前刷新）
        </p>
        <p class="faint" v-else>凭证过期时间未知（手动导入的凭证没有时间戳），依赖服务端校验。</p>
        <div class="row" style="margin-top:8px">
          <button class="btn--sm" @click="refreshCredential">手动刷新凭证</button>
          <button class="btn--sm btn--danger" @click="logout">登出</button>
        </div>
      </div>

      <div v-else>
        <h3 class="pane__title">选择登录方式</h3>
        <div class="login-methods">
          <button :class="{'btn--primary': state.method==='qq'}" @click="startQr('qq')">QQ 扫码</button>
          <button :class="{'btn--primary': state.method==='wx'}" @click="startQr('wx')">微信扫码</button>
          <button :class="{'btn--primary': state.method==='mobile'}" @click="startQr('mobile')">APP 扫码</button>
          <button :class="{'btn--primary': state.method==='phone'}" @click="state.method='phone'">手机验证码</button>
          <button :class="{'btn--primary': state.method==='import'}" @click="state.method='import'">导入凭证</button>
        </div>

        <div v-if="['qq','wx','mobile'].includes(state.method)" class="card" style="margin-top:12px">
          <div v-if="state.qr">
            <img v-if="qrImage" class="qr" :src="qrImage" alt="登录二维码（可长按保存）" />
            <p class="row" style="margin-top:8px">
              <span class="badge" :class="state.qr.status==='done' ? 'badge--ok' : (state.qr.status==='timeout' || state.qr.status==='refused' || state.qr.status==='error' ? 'badge--danger' : 'badge--info')">
                {{ qrText }}
              </span>
              <button class="btn--sm" @click="refreshQr" :disabled="state.busy">刷新二维码</button>
            </p>
            <p class="faint">
              {{ state.method === 'mobile' ? 'APP 扫码走 MQTT 长连接，状态是服务端实时推送的。' : '状态每 1.5 秒轮询一次。' }}
              手机上可长按二维码保存。
            </p>
          </div>
          <p v-else class="muted">点击上方按钮获取二维码。</p>
        </div>

        <div v-if="state.method==='phone'" class="card" style="margin-top:12px">
          <div class="field">
            <label>国家代码</label>
            <input type="number" v-model.number="state.phone.countryCode" />
          </div>
          <div class="field">
            <label>手机号</label>
            <input type="text" inputmode="numeric" v-model="state.phone.number" placeholder="13000000000" />
          </div>
          <button class="btn--primary" @click="sendCode" :disabled="state.busy || !state.phone.number">发送验证码</button>

          <p v-if="state.phone.message" class="muted" style="margin-top:8px">{{ state.phone.message }}</p>
          <p v-if="state.phone.status==='captcha'" class="muted">
            需要人机验证：<a :href="state.phone.captchaUrl" target="_blank" rel="noopener">点此完成验证</a>，完成后重新发送验证码。
          </p>
          <p v-if="state.phone.status==='frequency'" class="muted">触发频率限制，请稍后再试。</p>

          <div v-if="state.phone.sessionId" class="field" style="margin-top:12px">
            <label>短信验证码</label>
            <input type="text" inputmode="numeric" v-model="state.phone.code" placeholder="6 位验证码" />
          </div>
          <button v-if="state.phone.sessionId" class="btn--primary" @click="verifyCode" :disabled="state.busy || !state.phone.code">
            登录
          </button>
        </div>

        <div v-if="state.method==='import'" class="card" style="margin-top:12px">
          <p class="faint">
            只填 musicid + musickey 也能用，但那样凭证里没有 encrypt_uin，
            「我喜欢」「收藏的歌单」和账号信息会不可用；有 refresh_key / refresh_token 才能自动刷新。
          </p>
          <div class="field"><label>musicid</label><input type="text" inputmode="numeric" v-model="state.credential.musicid" /></div>
          <div class="field"><label>musickey</label><input type="text" v-model="state.credential.musickey" placeholder="Q_H_L_… 或 W_X_…" /></div>
          <div class="field"><label>refresh_key（可选）</label><input type="text" v-model="state.credential.refresh_key" /></div>
          <div class="field"><label>refresh_token（可选）</label><input type="text" v-model="state.credential.refresh_token" /></div>
          <div class="field"><label>encrypt_uin（可选）</label><input type="text" v-model="state.credential.encrypt_uin" /></div>
          <button class="btn--primary" @click="importCredential" :disabled="state.busy || !state.credential.musicid || !state.credential.musickey">
            导入
          </button>
          <hr style="margin:16px 0;border:none;border-top:1px solid var(--border)" />
          <div class="field"><label>或从 JSON 文件导入（服务端本地路径）</label><input type="text" v-model="state.credential.path" placeholder="/path/to/credential.json" /></div>
          <button @click="importFile" :disabled="state.busy || !state.credential.path">读取文件</button>
        </div>
      </div>
    </div>
  `,
};
