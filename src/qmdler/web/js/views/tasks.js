// SPDX-License-Identifier: GPL-3.0-or-later
// 任务面板：进度、倒计时、暂停/恢复/取消、汇总报告。

import { api } from '../api.js';
import { store, toast, formatDuration } from '../store.js';

const { reactive, computed, onMounted } = Vue;

export const TasksView = {
  name: 'TasksView',
  setup() {
    const state = reactive({ loading: false });

    async function refresh() {
      state.loading = true;
      try {
        store.tasks = await api.listTasks();
        if (store.activeTaskId) {
          store.items = await api.listItems(store.activeTaskId);
        }
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.loading = false;
      }
    }

    async function select(task) {
      store.activeTaskId = task.id;
      store.items = await api.listItems(task.id);
      store.report = null;
    }

    async function act(action) {
      if (!store.activeTaskId) return;
      try {
        await action(store.activeTaskId);
      } catch (error) {
        toast(error.message, 'error');
      }
    }

    async function loadReport() {
      if (!store.activeTaskId) return;
      store.report = await api.report(store.activeTaskId);
    }

    const activeTask = computed(() => store.tasks.find((task) => task.id === store.activeTaskId) || null);
    const counts = computed(() => store.engine.counts || {});
    const csvUrl = computed(() => (store.activeTaskId ? api.reportCsvUrl(store.activeTaskId) : '#'));

    onMounted(refresh);

    return { state, store, api, refresh, select, act, loadReport, activeTask, counts, csvUrl, formatDuration };
  },
  template: `
    <div>
      <h3 class="pane__title">任务</h3>

      <div class="card">
        <div class="row">
          <span class="dot" :class="store.engine.running ? 'dot--ok' : ''"></span>
          <strong>{{ store.engine.running ? (store.engine.task_name || '运行中') : '空闲' }}</strong>
          <span class="spacer"></span>
          <span class="badge badge--muted">{{ store.engine.phase }}</span>
        </div>

        <div v-if="store.engine.item_title" class="muted" style="margin-top:6px">
          当前：{{ store.engine.item_title }}
        </div>

        <div v-if="store.engine.waiting" style="margin-top:8px">
          <div class="row">
            <span class="muted">下一首倒计时 {{ formatDuration(store.engine.countdown) }}</span>
            <span class="spacer"></span>
            <button class="btn--sm" @click="act(api.skipWait)">立即下载下一首</button>
          </div>
          <div class="progress" style="margin-top:4px">
            <div class="progress__bar" :style="{width: (100 - (store.engine.countdown / (store.engine.countdown_total||1) * 100)).toFixed(1)+'%'}"></div>
          </div>
        </div>

        <div v-if="store.engine.pause_reason" class="badge badge--warn" style="margin-top:8px;white-space:normal">
          {{ store.engine.pause_reason }}
        </div>

        <div class="row" style="margin-top:10px">
          <span class="badge badge--ok">成功 {{ counts.success || 0 }}</span>
          <span class="badge badge--danger">失败 {{ counts.failed || 0 }}</span>
          <span class="badge badge--muted">跳过 {{ counts.skipped || 0 }}</span>
          <span class="badge badge--warn">受限 {{ counts.unavailable || 0 }}</span>
          <span class="badge badge--trial">试听 {{ counts.trial || 0 }}</span>
        </div>

        <div class="row" style="margin-top:10px">
          <button class="btn--primary btn--sm" @click="act(api.startTask)" :disabled="!store.activeTaskId || store.engine.running">开始 / 继续</button>
          <button class="btn--sm" @click="act(api.pauseTask)" :disabled="!store.engine.running">暂停</button>
          <button class="btn--sm btn--danger" @click="act(api.cancelTask)" :disabled="!store.activeTaskId">取消</button>
          <button class="btn--sm" @click="act(api.retryFailed).then(refresh)" :disabled="!store.activeTaskId">只重试失败项</button>
        </div>
      </div>

      <div class="card">
        <div class="row">
          <strong>任务列表</strong>
          <span class="spacer"></span>
          <button class="btn--sm" @click="refresh">刷新</button>
        </div>
        <ul style="list-style:none;padding:0;margin:8px 0 0">
          <li v-for="task in store.tasks" :key="task.id">
            <button class="btn--sm" style="width:100%;justify-content:space-between;margin-bottom:6px"
                    :class="{'btn--primary': task.id===store.activeTaskId}" @click="select(task)">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ task.name }}</span>
              <span class="faint">{{ task.status }} · {{ task.total_items }}</span>
            </button>
          </li>
        </ul>
        <p v-if="!store.tasks.length" class="muted">还没有任务。</p>
      </div>

      <div class="card">
        <div class="row">
          <strong>汇总报告</strong>
          <span class="spacer"></span>
          <button class="btn--sm" @click="loadReport" :disabled="!store.activeTaskId">生成</button>
          <a v-if="store.activeTaskId" class="btn btn--sm" :href="csvUrl" download>导出 CSV</a>
        </div>
        <div v-if="store.report" style="margin-top:8px">
          <p class="muted">{{ store.report.summary }}</p>

          <details v-if="store.report.trials.length" open>
            <summary>试听 / 降级（{{ store.report.trials.length }}）—— 单独计数，不混进成功</summary>
            <ul class="mono">
              <li v-for="row in store.report.trials" :key="'t'+row.id">{{ row.title }} — {{ row.reason }}</li>
            </ul>
          </details>

          <!-- 成功但四层校验没跑全：文件是完整的，但试听检测少了一层。
               不点出来，报告就等于在说「全部通过四层校验」。 -->
          <details v-if="store.report.incomplete_verification.length" open>
            <summary>
              校验层不完整（{{ store.report.incomplete_verification.length }}）——
              计入成功，但四层校验没跑全（完整通过 {{ store.report.fully_verified }} 首）
            </summary>
            <ul class="mono">
              <li v-for="row in store.report.incomplete_verification" :key="'v'+row.id">
                {{ row.title }} — {{ row.actual_quality }}：容器读不出时长，第 4 层未执行
              </li>
            </ul>
          </details>

          <details v-if="store.report.failures.length">
            <summary>失败（{{ store.report.failures.length }}）</summary>
            <ul class="mono">
              <li v-for="row in store.report.failures" :key="'f'+row.id">{{ row.title }} — {{ row.reason }}</li>
            </ul>
          </details>

          <details v-if="store.report.unavailable.length">
            <summary>受限 / 无版权（{{ store.report.unavailable.length }}）</summary>
            <ul class="mono">
              <li v-for="row in store.report.unavailable" :key="'u'+row.id">{{ row.title }} — {{ row.reason }}</li>
            </ul>
          </details>
        </div>
      </div>

      <div class="card">
        <strong>日志</strong>
        <div class="log" style="margin-top:8px">
          <div v-for="(line, index) in store.log.slice(-200)" :key="index"
               class="log__line" :class="'log__line--' + line.level">
            {{ line.message }}
          </div>
          <p v-if="!store.log.length" class="faint">暂无日志</p>
        </div>
      </div>
    </div>
  `,
};
