// SPDX-License-Identifier: GPL-3.0-or-later
// 音质优先级链：拖拽排序生成，不是单选。
//
// HTML5 drag-and-drop 在移动浏览器上基本不可用，所以用 vendored 的 SortableJS
// （基于 Pointer Events）。同时保留上下箭头按钮，作为无障碍与兜底交互。

const { onMounted, onBeforeUnmount, ref, watch } = Vue;

export const QualityChain = {
  name: 'QualityChain',
  props: {
    modelValue: { type: Array, required: true },
    catalog: { type: Array, required: true },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const listRef = ref(null);
    let sortable = null;

    function labelOf(code) {
      const found = props.catalog.find((entry) => entry.code === code);
      return found ? found.label : code;
    }

    // 档位级别的告警（目前只有 NAC）。要在**选之前**就看见，不是下完才发现。
    function caveatOf(code) {
      const found = props.catalog.find((entry) => entry.code === code);
      return found ? (found.caveat || '') : '';
    }

    function move(index, delta) {
      const next = [...props.modelValue];
      const target = index + delta;
      if (target < 0 || target >= next.length) return;
      [next[index], next[target]] = [next[target], next[index]];
      emit('update:modelValue', next);
    }

    function remove(index) {
      const next = [...props.modelValue];
      next.splice(index, 1);
      if (next.length) emit('update:modelValue', next);
    }

    function add(code) {
      if (!code || props.modelValue.includes(code)) return;
      emit('update:modelValue', [...props.modelValue, code]);
    }

    function mountSortable() {
      if (!listRef.value || typeof Sortable === 'undefined') return;
      if (sortable) sortable.destroy();
      sortable = Sortable.create(listRef.value, {
        handle: '.chain__handle',
        animation: 150,
        // 手机上长按 200ms 才开始拖，避免和页面滚动打架。
        delay: 200,
        delayOnTouchOnly: true,
        touchStartThreshold: 5,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        onEnd(event) {
          const next = [...props.modelValue];
          const [moved] = next.splice(event.oldIndex, 1);
          next.splice(event.newIndex, 0, moved);
          emit('update:modelValue', next);
        },
      });
    }

    onMounted(mountSortable);
    watch(() => props.modelValue.length, () => setTimeout(mountSortable, 0));
    onBeforeUnmount(() => {
      if (sortable) sortable.destroy();
    });

    return { listRef, labelOf, caveatOf, move, remove, add };
  },
  template: `
    <div>
      <ul class="chain" ref="listRef">
        <li v-for="(code, index) in modelValue" :key="code">
          <span class="chain__handle" title="拖动排序">⠿</span>
          <span class="chain__rank">{{ index + 1 }}</span>
          <span class="chain__label">
            {{ labelOf(code) }}
            <span v-if="caveatOf(code)" class="chain__caveat" :title="caveatOf(code)">⚠ {{ caveatOf(code) }}</span>
          </span>
          <button class="btn--sm" @click="move(index, -1)" :disabled="index===0" aria-label="上移">↑</button>
          <button class="btn--sm" @click="move(index, 1)" :disabled="index===modelValue.length-1" aria-label="下移">↓</button>
          <button class="btn--sm btn--danger" @click="remove(index)" :disabled="modelValue.length<=1" aria-label="移除">✕</button>
        </li>
      </ul>
      <div class="row" style="margin-top:8px">
        <select @change="add($event.target.value); $event.target.value=''" style="flex:1 1 auto">
          <option value="">＋ 添加档位…</option>
          <option v-for="entry in catalog.filter(e => !modelValue.includes(e.code))" :key="entry.code" :value="entry.code">
            {{ entry.label }}（{{ entry.extension }}）{{ entry.caveat ? ' ⚠ ' + entry.caveat : '' }}
          </option>
        </select>
      </div>
      <p class="faint">按顺序尝试，取第一个真正拿得到的档位；拿到比首选低的档位会标注「已降级」。</p>
    </div>
  `,
};
