// 应用入口。
//
// Vue 3 全局构建（vue.global.prod.js）+ 组件用 JS 对象 + template 字符串编写。
// 不用 in-DOM 模板：HTML 解析大小写不敏感、自闭合标签会出错。
// 不引入 vue-router 与 Pinia，reactive() + 标签页切换足够。
// 全部依赖已 vendored 进包，不引用任何 CDN。

import { api } from './api.js';
import { connect } from './ws.js';
import { store, applyEvent, toast } from './store.js';
import { LoginView } from './views/login.js';
import { SourcesView } from './views/sources.js';
import { TracksView } from './views/tracks.js';
import { TasksView } from './views/tasks.js';
import { SettingsView } from './views/settings.js';

const { createApp, computed, onMounted } = Vue;

const App = {
  components: { LoginView, SourcesView, TracksView, TasksView, SettingsView },
  setup() {
    const tabs = [
      { key: 'sources', label: '来源', icon: '☰' },
      { key: 'tracks', label: '曲目', icon: '♪' },
      { key: 'tasks', label: '任务', icon: '⇩' },
      { key: 'settings', label: '设置', icon: '⚙' },
    ];

    const authLabel = computed(() => {
      if (!store.auth.logged_in) return '未登录';
      const profile = store.auth.profile || {};
      return profile.nickname || `musicid ${store.auth.musicid}`;
    });

    onMounted(async () => {
      connect({
        onOpen: () => {
          store.connected = true;
        },
        onClose: () => {
          store.connected = false;
        },
        onEvent: applyEvent,
      });

      try {
        const snapshot = await api.snapshot();
        store.auth = snapshot.auth;
        store.engine = snapshot.engine;
        store.settings = snapshot.settings;
        store.tasks = await api.listTasks();
        if (store.auth.logged_in) api.profile().catch(() => {});
      } catch (error) {
        toast(`初始化失败：${error.message}`, 'error');
      }

      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(() => {});
      }
    });

    return { store, tabs, authLabel };
  },
  template: `
    <div class="shell">
      <header class="topbar">
        <div class="topbar__brand">qmdler</div>
        <div class="topbar__status">
          <span class="dot" :class="store.connected ? 'dot--ok' : 'dot--danger'"></span>
          <span>{{ store.connected ? '已连接' : '连接中断，正在重连…' }}</span>
          <span>·</span>
          <span>{{ authLabel }}</span>
          <span v-if="store.auth.needs_login" class="badge badge--danger">凭证失效，请重新登录</span>
          <span v-if="store.engine.running" class="badge badge--info">
            {{ store.engine.item_title || '运行中' }}
          </span>
        </div>
        <div class="topbar__actions">
          <button class="btn--sm" :class="{'btn--primary': store.tab==='settings'}"
                  @click="store.tab = store.tab==='settings' ? 'tracks' : 'settings'">
            {{ store.tab==='settings' ? '返回曲目' : '设置' }}
          </button>
        </div>
      </header>

      <div class="panes">
        <aside class="pane pane--left" :class="{'is-active': store.tab==='sources'}">
          <login-view />
          <hr style="margin:16px 0;border:none;border-top:1px solid var(--border)" />
          <sources-view />
        </aside>

        <main class="pane pane--center" :class="{'is-active': store.tab==='tracks' || store.tab==='settings'}">
          <settings-view v-if="store.tab==='settings'" />
          <tracks-view v-else />
        </main>

        <aside class="pane pane--right" :class="{'is-active': store.tab==='tasks'}">
          <tasks-view />
        </aside>
      </div>

      <nav class="tabbar">
        <button v-for="tab in tabs" :key="tab.key" :class="{'is-active': store.tab===tab.key}" @click="store.tab=tab.key">
          <span class="tabbar__icon">{{ tab.icon }}</span>
          <span>{{ tab.label }}</span>
        </button>
      </nav>

      <div class="toast-stack">
        <div v-for="entry in store.toasts" :key="entry.id" class="toast" :class="'toast--' + entry.level">
          {{ entry.message }}
        </div>
      </div>
    </div>
  `,
};

createApp(App).mount('#app');
