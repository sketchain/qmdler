// 曲目列表：宽屏用表格，手机用卡片（不做横向滚动的宽表格）。

import { api } from '../api.js';
import { store, toast, formatBytes, formatDuration, STATUS_META } from '../store.js';

const { reactive, computed } = Vue;

export const TracksView = {
  name: 'TracksView',
  setup() {
    const state = reactive({ keyword: '', creating: false, taskName: '', saveRoot: '' });

    const rows = computed(() => {
      // 已有任务时展示任务条目，否则展示刚拉取的来源结果。
      if (store.activeTaskId && store.items.length) return store.items;
      const keyword = state.keyword.trim().toLowerCase();
      return store.sourceItems
        .filter((item) => {
          if (!keyword) return true;
          const hay = `${item.title} ${item.album_name} ${(item.singers || []).map((s) => s.name).join(' ')}`;
          return hay.toLowerCase().includes(keyword);
        })
        .map((item) => ({
          id: null,
          songmid: item.songmid,
          title: item.title,
          singers: (item.singers || []).map((s) => s.name).join('、'),
          album: item.album_name,
          interval: item.interval,
          status: 'pending',
          annotation: item.annotation,
          selected: store.selectedMids.has(item.songmid),
        }));
    });

    const isTaskMode = computed(() => Boolean(store.activeTaskId && store.items.length));

    function toggle(row) {
      if (isTaskMode.value) {
        const next = !row.selected;
        row.selected = next;
        api.setSelection(store.activeTaskId, [row.id], next).catch((error) => toast(error.message, 'error'));
        return;
      }
      if (store.selectedMids.has(row.songmid)) store.selectedMids.delete(row.songmid);
      else store.selectedMids.add(row.songmid);
    }

    function selectAll(value) {
      if (isTaskMode.value) {
        api.setSelection(store.activeTaskId, null, value)
          .then(() => store.items.forEach((item) => { item.selected = value; }))
          .catch((error) => toast(error.message, 'error'));
        return;
      }
      if (value) rows.value.forEach((row) => store.selectedMids.add(row.songmid));
      else store.selectedMids.clear();
    }

    function invert() {
      if (isTaskMode.value) {
        api.invertSelection(store.activeTaskId)
          .then(() => store.items.forEach((item) => { item.selected = !item.selected; }))
          .catch((error) => toast(error.message, 'error'));
        return;
      }
      rows.value.forEach((row) => {
        if (store.selectedMids.has(row.songmid)) store.selectedMids.delete(row.songmid);
        else store.selectedMids.add(row.songmid);
      });
    }

    async function createTask() {
      if (!store.sourceResult) return;
      state.creating = true;
      try {
        const task = await api.createTask({
          name: state.taskName || store.sourceResult.name,
          source_type: store.sourceResult.source_type,
          source_id: store.sourceResult.identifier,
          source_ref: store.sourceResult.identifier,
          save_root: state.saveRoot,
          quality_chain: store.settings ? store.settings.quality.chain : [],
          items: store.sourceItems,
          selected_mids: Array.from(store.selectedMids),
        });
        store.activeTaskId = task.id;
        store.items = await api.listItems(task.id);
        store.tasks = await api.listTasks();
        toast(`任务已创建：${task.name}`);
        store.tab = 'tasks';
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.creating = false;
      }
    }

    function badgeFor(row) {
      return STATUS_META[row.status] || STATUS_META.pending;
    }

    function annotationOf(row) {
      return row.annotation || (row.song && row.song.annotation) || null;
    }

    function warnings(row) {
      const annotation = annotationOf(row);
      if (!annotation) return [];
      return (annotation.plan && annotation.plan.warnings) || [];
    }

    function bestQuality(row) {
      const annotation = annotationOf(row);
      if (annotation) return annotation.best_available_label || '—';
      return row.actual_quality_label || row.requested_quality_label || '—';
    }

    const selectedCount = computed(() =>
      isTaskMode.value ? store.items.filter((item) => item.selected).length : store.selectedMids.size,
    );

    return {
      state, store, rows, isTaskMode, toggle, selectAll, invert, createTask,
      badgeFor, warnings, bestQuality, selectedCount, formatBytes, formatDuration,
    };
  },
  template: `
    <div>
      <div class="row" style="margin-bottom:10px">
        <h3 class="pane__title" style="margin:0">
          {{ isTaskMode ? '任务队列' : (store.sourceResult ? store.sourceResult.name : '曲目') }}
        </h3>
        <span class="spacer"></span>
        <span class="muted">已选 {{ selectedCount }} / {{ rows.length }}</span>
      </div>

      <div class="row" style="margin-bottom:10px">
        <input type="search" v-model="state.keyword" placeholder="按歌名 / 歌手 / 专辑 过滤" style="flex:1 1 200px" />
        <button class="btn--sm" @click="selectAll(true)">全选</button>
        <button class="btn--sm" @click="selectAll(false)">全不选</button>
        <button class="btn--sm" @click="invert">反选</button>
      </div>

      <div v-if="!isTaskMode && store.sourceResult" class="card">
        <div class="field">
          <label>任务名称</label>
          <input type="text" v-model="state.taskName" :placeholder="store.sourceResult.name" />
        </div>
        <div class="field">
          <label>保存位置（留空用全局根目录，可为不同歌单单独设置）</label>
          <input type="text" v-model="state.saveRoot" :placeholder="store.settings ? store.settings.paths.save_root : ''" />
        </div>
        <button class="btn--primary" @click="createTask" :disabled="state.creating || !selectedCount">
          创建下载任务（{{ selectedCount }} 首）
        </button>
      </div>

      <p v-if="!rows.length" class="muted">左侧选择一个来源后，曲目会显示在这里。</p>

      <!-- 宽屏：表格 -->
      <div class="table-wrap" v-if="rows.length">
        <table class="tracks-table">
          <thead>
            <tr>
              <th style="width:44px"></th>
              <th>歌名</th>
              <th>歌手</th>
              <th>专辑</th>
              <th class="cell--num">时长</th>
              <th>最高可用音质</th>
              <th>状态 / 提示</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.songmid + '-' + (row.id || '')">
              <td>
                <label class="check"><input type="checkbox" :checked="row.selected" @change="toggle(row)" /></label>
              </td>
              <td class="cell--title">
                {{ row.title }}
                <span v-if="row.degrade_note" class="badge badge--warn">{{ row.degrade_note }}</span>
              </td>
              <td>{{ row.singers }}</td>
              <td>{{ row.album }}</td>
              <td class="cell--num">{{ formatDuration(row.interval) }}</td>
              <td>{{ bestQuality(row) }}</td>
              <td>
                <span class="badge" :class="badgeFor(row).cls">{{ badgeFor(row).label }}</span>
                <span v-for="warning in warnings(row)" :key="warning" class="badge badge--warn">{{ warning }}</span>
                <div v-if="row.status==='downloading'" class="progress" style="margin-top:4px">
                  <div class="progress__bar" :style="{width: ((row.progress||0)*100).toFixed(1)+'%'}"></div>
                </div>
                <div v-if="row.error_message" class="faint">{{ row.error_message }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 手机：卡片列表 -->
      <div class="tracks-cards" v-if="rows.length">
        <div class="track-card" v-for="row in rows" :key="'c-'+row.songmid+'-'+(row.id||'')">
          <label class="check"><input type="checkbox" :checked="row.selected" @change="toggle(row)" /></label>
          <div class="track-card__body">
            <div class="track-card__title">{{ row.title }}</div>
            <div class="track-card__sub">{{ row.singers }} · {{ row.album }} · {{ formatDuration(row.interval) }}</div>
            <div class="track-card__tags">
              <span class="badge" :class="badgeFor(row).cls">{{ badgeFor(row).label }}</span>
              <span class="badge badge--muted">{{ bestQuality(row) }}</span>
              <span v-for="warning in warnings(row)" :key="warning" class="badge badge--warn">{{ warning }}</span>
            </div>
            <div v-if="row.status==='downloading'" class="progress" style="margin-top:6px">
              <div class="progress__bar" :style="{width: ((row.progress||0)*100).toFixed(1)+'%'}"></div>
            </div>
            <div v-if="row.error_message" class="faint">{{ row.error_message }}</div>
          </div>
        </div>
      </div>
    </div>
  `,
};
