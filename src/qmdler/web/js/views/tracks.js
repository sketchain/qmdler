// SPDX-License-Identifier: GPL-3.0-or-later
// 曲目列表：宽屏用表格，手机用卡片（不做横向滚动的宽表格）。
//
// 两条都是实测逼出来的，5075 首的歌单上量过：
//
// 1. **只渲染当前这一种布局。** 原先表格和卡片是同时挂在 DOM 里、靠 CSS
//    藏掉另一种 —— 5075 条就是 10150 个节点，白白多一倍。改成用
//    matchMedia 在 JS 侧判断，只渲染一种。
// 2. **窗口化渲染。** 全量渲染实测首屏 4.1s、清空过滤 3.2s，明显掉帧。
//    只渲染视口内 + 前后各 8 行，上下用占位撑出滚动条高度。
//
// 窗口化要求行高一致，所以过长的内容用省略号截断（完整内容进 title 属性），
// 这也顺带让长列表看起来更整齐。
//
// 行高**在运行时量**，不写死：表格单元格把 CSS 的 height 当成最小高度，
// 实际行高还受字号、padding、徽标撑高影响 —— 实测按 44px 算、实际渲染 61px，
// 偏移就会越滚越歪。下面的常量只是首帧还没量到时的兜底。

import { api } from '../api.js';
import { store, toast, formatBytes, formatDuration, STATUS_META } from '../store.js';

const { reactive, computed, ref, onMounted, onBeforeUnmount } = Vue;

/** 表格行高的兜底值，真实值在运行时量。 */
const ROW_H = 44;
/** 卡片高的兜底值，真实值在运行时量。 */
const CARD_H = 104;
/** 视口上下各多渲染几行，滚动时不会看到空白。 */
const OVERSCAN = 8;

export const TracksView = {
  name: 'TracksView',
  setup() {
    const state = reactive({ keyword: '', creating: false, taskName: '', saveRoot: '' });

    // ---- 布局与窗口化 ----
    const scroller = ref(null);
    const scrollTop = ref(0);
    const viewportH = ref(600);
    const isNarrow = ref(false);
    let mq = null;
    let ro = null;

    function onScroll(event) {
      scrollTop.value = event.target.scrollTop;
      if (!measuredRowH.value) syncViewport();
    }

    // 实测行高。量不到（首帧、空列表）就退回常量。
    const measuredRowH = ref(0);

    function syncViewport() {
      const el = scroller.value;
      if (!el) return;
      viewportH.value = el.clientHeight || 600;
      const sample = el.querySelector('tbody tr:not(.vpad)') || el.querySelector('.track-card');
      if (sample && sample.offsetHeight > 0) measuredRowH.value = sample.offsetHeight;
    }

    onMounted(() => {
      mq = window.matchMedia('(max-width: 767px)');
      isNarrow.value = mq.matches;
      mq.addEventListener('change', (event) => { isNarrow.value = event.matches; });
      syncViewport();
      if (window.ResizeObserver && scroller.value) {
        ro = new ResizeObserver(syncViewport);
        ro.observe(scroller.value);
      }
    });

    onBeforeUnmount(() => { if (ro) ro.disconnect(); });

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

    const rowHeight = computed(() => measuredRowH.value || (isNarrow.value ? CARD_H : ROW_H));

    // 行渲染出来之后再量一次，量到的值会反过来修正窗口大小。
    Vue.watch(
      () => [rows.value.length, isNarrow.value],
      () => Vue.nextTick(syncViewport),
    );

    const window_ = computed(() => {
      const total = rows.value.length;
      const h = rowHeight.value;
      const start = Math.max(0, Math.floor(scrollTop.value / h) - OVERSCAN);
      const visible = Math.ceil(viewportH.value / h) + OVERSCAN * 2;
      const end = Math.min(total, start + visible);
      return { start, end, padTop: start * h, padBottom: Math.max(0, (total - end) * h) };
    });

    const visibleRows = computed(() => rows.value.slice(window_.value.start, window_.value.end));

    return {
      state, store, rows, isTaskMode, toggle, selectAll, invert, createTask,
      badgeFor, warnings, bestQuality, selectedCount, formatBytes, formatDuration,
      scroller, onScroll, isNarrow, visibleRows, window: window_,
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

      <!-- 截断必须常驻可见。一闪而过的 toast 太弱：用户会拿着 2000 首
           当成 5075 首的完整歌单去建任务，那比报错更糟。 -->
      <p v-if="!isTaskMode && store.sourceResult && store.sourceResult.truncated"
         class="banner banner--warn">
        ⚠ 只拉到 {{ store.sourceResult.count }} 首，该来源共
        <strong>{{ store.sourceResult.total || '未知' }}</strong> 首 ——
        已达本次拉取上限，列表<strong>不完整</strong>。要全部拉取请调大「拉取上限」后重拉。
      </p>

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

      <!-- 只渲染当前这一种布局，并且只渲染视口内的行。 -->
      <div class="vlist" ref="scroller" @scroll.passive="onScroll" v-if="rows.length">
        <!-- 宽屏：表格 -->
        <table class="tracks-table" v-if="!isNarrow">
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
            <tr v-if="window.padTop" class="vpad" :style="{height: window.padTop + 'px'}"><td colspan="7"></td></tr>
            <tr v-for="row in visibleRows" :key="row.songmid + '-' + (row.id || '')">
              <td>
                <label class="check"><input type="checkbox" :checked="row.selected" @change="toggle(row)" /></label>
              </td>
              <td class="cell--title" :title="row.title">
                {{ row.title }}
                <span v-if="row.degrade_note" class="badge badge--warn">{{ row.degrade_note }}</span>
              </td>
              <td :title="row.singers">{{ row.singers }}</td>
              <td :title="row.album">{{ row.album }}</td>
              <td class="cell--num">{{ formatDuration(row.interval) }}</td>
              <td>{{ bestQuality(row) }}</td>
              <td :title="row.error_message || ''">
                <span class="badge" :class="badgeFor(row).cls">{{ badgeFor(row).label }}</span>
                <span v-for="warning in warnings(row)" :key="warning" class="badge badge--warn">{{ warning }}</span>
                <div v-if="row.status==='downloading'" class="progress" style="margin-top:4px">
                  <div class="progress__bar" :style="{width: ((row.progress||0)*100).toFixed(1)+'%'}"></div>
                </div>
                <span v-if="row.error_message" class="faint">{{ row.error_message }}</span>
              </td>
            </tr>
            <tr v-if="window.padBottom" class="vpad" :style="{height: window.padBottom + 'px'}"><td colspan="7"></td></tr>
          </tbody>
        </table>

        <!-- 手机：卡片列表 -->
        <div class="tracks-cards" v-else>
          <div v-if="window.padTop" :style="{height: window.padTop + 'px'}"></div>
          <div class="track-card" v-for="row in visibleRows" :key="'c-'+row.songmid+'-'+(row.id||'')">
            <label class="check"><input type="checkbox" :checked="row.selected" @change="toggle(row)" /></label>
            <div class="track-card__body">
              <div class="track-card__title" :title="row.title">{{ row.title }}</div>
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
          <div v-if="window.padBottom" :style="{height: window.padBottom + 'px'}"></div>
        </div>
      </div>
    </div>
  `,
};
