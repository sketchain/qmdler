// SPDX-License-Identifier: GPL-3.0-or-later
// 歌单来源：八种来源分标签页。

import { api } from '../api.js';
import { store, toast } from '../store.js';

const { reactive, computed, watch, onMounted } = Vue;

export const SourcesView = {
  name: 'SourcesView',
  setup() {
    const state = reactive({
      tab: 'created',
      input: '',
      // 默认值从后端读（store.settings.limits），这里只是它到位前的占位。
      limit: 0,
      loading: false,
    });

    async function loadCreated() {
      state.loading = true;
      try {
        store.createdSonglists = await api.createdSonglists();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.loading = false;
      }
    }

    async function loadFav() {
      state.loading = true;
      try {
        store.favSonglists = await api.favSonglists();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.loading = false;
      }
    }

    async function fetchSource(sourceType, identifier, name) {
      state.loading = true;
      try {
        const result = await api.fetchSource({
          source_type: sourceType,
          identifier: String(identifier || ''),
          limit: state.limit,
          name: name || '',
        });
        store.sourceResult = result;
        store.sourceItems = result.items;
        store.selectedMids = new Set(result.items.map((item) => item.songmid));
        store.tab = 'tracks';
        toast(`已拉取 ${result.count} 首${result.truncated ? '（已达上限，可调大后重拉）' : ''}`);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.loading = false;
      }
    }

    async function fetchFromInput() {
      const text = state.input.trim();
      if (!text) return;
      try {
        const resolved = await api.resolveLink(text);
        if (resolved.source_type === 'manual' && resolved.mids) {
          await fetchSource('manual', resolved.mids.join('\n'), '手动列表');
        } else {
          await fetchSource(resolved.source_type, resolved.identifier, '');
        }
      } catch (error) {
        toast(error.message, 'error');
      }
    }

    onMounted(() => {
      if (store.auth.logged_in) loadCreated();
    });

    // 上限只有后端一个来源，前端不写死。
    const fetchMax = computed(() => (store.settings?.limits?.fetch_max) || 10000);
    watch(
      () => store.settings?.limits?.fetch_default,
      (value) => { if (value && !state.limit) state.limit = value; },
      { immediate: true },
    );

    return { state, store, loadCreated, loadFav, fetchSource, fetchFromInput, fetchMax };
  },
  template: `
    <div>
      <h3 class="pane__title">歌单来源</h3>
      <div class="row row--tight" style="margin-bottom:10px">
        <button class="btn--sm" :class="{'btn--primary': state.tab==='created'}" @click="state.tab='created'; loadCreated()">自建</button>
        <button class="btn--sm" :class="{'btn--primary': state.tab==='fav'}" @click="state.tab='fav'; loadFav()">收藏</button>
        <button class="btn--sm" :class="{'btn--primary': state.tab==='link'}" @click="state.tab='link'">链接/ID</button>
        <button class="btn--sm" :class="{'btn--primary': state.tab==='search'}" @click="state.tab='search'">搜索</button>
        <button class="btn--sm" :class="{'btn--primary': state.tab==='manual'}" @click="state.tab='manual'">mid 列表</button>
      </div>

      <div class="field">
        <label>拉取上限（分页会自动翻完，但不会无限拉）</label>
        <input type="number" v-model.number="state.limit" min="1" :max="fetchMax" />
      </div>

      <button class="btn--sm" style="width:100%;margin-bottom:10px" @click="fetchSource('fav_song','', '我喜欢')" :disabled="!store.auth.logged_in">
        ♥ 我喜欢
      </button>

      <div v-if="state.tab==='created'">
        <p v-if="!store.auth.logged_in" class="muted">需要先登录。</p>
        <p v-else-if="!store.createdSonglists.length" class="muted">{{ state.loading ? '加载中…' : '没有自建歌单' }}</p>
        <ul style="list-style:none;padding:0;margin:0">
          <li v-for="list in store.createdSonglists" :key="list.id">
            <button class="btn--sm" style="width:100%;justify-content:space-between;margin-bottom:6px"
                    @click="fetchSource('songlist', list.id, list.title)">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ list.title }}</span>
              <span class="faint">{{ list.songnum }}</span>
            </button>
          </li>
        </ul>
      </div>

      <div v-if="state.tab==='fav'">
        <p v-if="!store.favSonglists.length" class="muted">{{ state.loading ? '加载中…' : '点击「收藏」加载' }}</p>
        <ul style="list-style:none;padding:0;margin:0">
          <li v-for="list in store.favSonglists" :key="list.id">
            <button class="btn--sm" style="width:100%;justify-content:space-between;margin-bottom:6px"
                    @click="fetchSource('songlist', list.id, list.title)">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ list.title }}</span>
              <span class="faint">{{ list.songnum }}</span>
            </button>
          </li>
        </ul>
      </div>

      <div v-if="state.tab==='link'">
        <div class="field">
          <label>歌单 / 专辑 / 歌手 分享链接或 ID</label>
          <input type="text" v-model="state.input" placeholder="https://y.qq.com/n/ryqq/playlist/7392570702" />
        </div>
        <button class="btn--primary" style="width:100%" @click="fetchFromInput" :disabled="state.loading">解析并拉取</button>
      </div>

      <div v-if="state.tab==='search'">
        <div class="field">
          <label>搜索关键词</label>
          <input type="search" v-model="state.input" placeholder="歌名 / 歌手" />
        </div>
        <button class="btn--primary" style="width:100%" @click="fetchSource('search', state.input, '')" :disabled="state.loading || !state.input">
          搜索
        </button>
      </div>

      <div v-if="state.tab==='manual'">
        <div class="field">
          <label>每行一个 songmid（也接受分享链接）</label>
          <textarea v-model="state.input" placeholder="003w2xz20QlUZt&#10;004Z8Ihr0JIu5s"></textarea>
        </div>
        <button class="btn--primary" style="width:100%" @click="fetchSource('manual', state.input, '手动列表')" :disabled="state.loading || !state.input">
          查询详情并载入
        </button>
        <p class="faint">这是唯一必须逐首调 query_song 的来源（其它来源的列表接口已经返回完整信息）。</p>
      </div>
    </div>
  `,
};
