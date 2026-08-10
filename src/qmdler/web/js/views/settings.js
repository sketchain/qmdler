// SPDX-License-Identifier: GPL-3.0-or-later
// 设置：音质链、间隔、模板（含实时预览）、歌词、封面、tag、保存位置、预设。

import { api } from '../api.js';
import { store, toast, formatBytes } from '../store.js';
import { QualityChain } from '../components/quality-chain.js';

const { reactive, computed, onMounted, watch } = Vue;

export const SettingsView = {
  name: 'SettingsView',
  components: { QualityChain },
  setup() {
    const state = reactive({
      section: 'quality',
      chain: [],
      preview: null,
      pathCheck: null,
      presets: {},
      activePreset: 'default',
      newPresetName: '',
      saving: false,
    });

    // 文本框必须绑本地草稿，不能直接绑 store.settings。
    //
    // 每次 patch() 成功都会把 store.settings 整个换掉，触发重渲染；而 Vue 给 input
    // 打 value 时是拿绑定值跟 **DOM 当前值** 比对（不是跟旧 prop 比），所以只要
    // 重渲染发生在用户正在输入的那个框上，就会把已经敲进去的字改写回服务端的旧值。
    // 值被改回原样后浏览器认为「没变过」，change 事件不再触发，于是这一次修改
    // 既没提示也没保存，凭空消失。
    //
    // 实测（连续改「目录模板 → 文件名模板 → 多歌手连接符」三个框）：
    //   7310 f1 input "{曲序}-{歌名}({songmid})"   ← 输入进去了
    //   7314 f1 blur  "{歌名} - {歌手}"            ← 4ms 后被弹回，没有 change
    // 三次修改只发出两个 PATCH，中间那个字段静默丢失。
    // v-model 绑 store 也是一样的结果 —— 症结在「重渲染改写 DOM」，不在绑定方式。
    const draft = reactive({ dir_template: '', file_template: '', singer_separator: '', save_root: '' });

    function syncDraft() {
      if (!store.settings) return;
      draft.dir_template = store.settings.naming.dir_template;
      draft.file_template = store.settings.naming.file_template;
      draft.singer_separator = store.settings.naming.singer_separator;
      draft.save_root = store.settings.paths.save_root;
    }

    const config = computed(() => store.settings);

    async function loadMeta() {
      const [qualities, templates, presets] = await Promise.all([
        api.qualities(), api.templates(), api.presets(),
      ]);
      store.qualities = qualities.items;
      store.qualityNotes = qualities.notes;
      store.templates = templates;
      state.presets = presets.presets;
      state.activePreset = presets.active;
      if (store.settings) state.chain = [...store.settings.quality.chain];
    }

    async function patch(path, value) {
      const patchBody = {};
      let cursor = patchBody;
      const parts = path.split('.');
      parts.forEach((key, index) => {
        if (index === parts.length - 1) cursor[key] = value;
        else {
          cursor[key] = {};
          cursor = cursor[key];
        }
      });
      state.saving = true;
      try {
        store.settings = await api.patchConfig(patchBody);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        state.saving = false;
      }
    }

    async function saveChain() {
      await patch('quality.chain', state.chain);
      toast('音质优先级链已保存');
    }

    async function refreshPreview() {
      if (!store.settings) return;
      try {
        state.preview = await api.previewTemplate({
          dir_template: draft.dir_template,
          file_template: draft.file_template,
          singer_separator: draft.singer_separator,
          quality: state.chain[0] || 'FLAC',
          root: draft.save_root,
          song: store.sourceItems.length ? store.sourceItems[0] : null,
          playlist_name: store.sourceResult ? store.sourceResult.name : '示例歌单',
        });
      } catch (error) {
        state.preview = null;
      }
    }

    async function checkPath(create = false) {
      if (!store.settings) return;
      state.pathCheck = await api.checkPath(draft.save_root, create);
    }

    function applyCombo(combo) {
      draft.dir_template = combo.dir;
      draft.file_template = combo.file;
      patch('naming', { dir_template: combo.dir, file_template: combo.file });
      refreshPreview();
    }

    async function activatePreset(name) {
      store.settings = await api.activatePreset(name);
      state.activePreset = name;
      state.chain = [...store.settings.quality.chain];
      syncDraft();
      toast(`已切换到预设「${name}」`);
      refreshPreview();
    }

    async function removePreset(name) {
      if (!window.confirm(`删除预设「${name}」？当前生效的配置不受影响。`)) return;
      try {
        await api.deletePreset(name);
        const presets = await api.presets();
        state.presets = presets.presets;
        toast(`预设「${name}」已删除`);
      } catch (error) {
        toast(error.message, 'error');
      }
    }

    async function savePreset() {
      const name = state.newPresetName.trim();
      if (!name) return;
      // 后端会挡掉带 / \ : * ? " < > | 的名字，不接住的话是一个未处理的 Promise
      // rejection —— 预设没建成，界面上却什么都不显示。
      try {
        await api.savePreset(name, {
          quality: store.settings.quality,
          download: store.settings.download,
          naming: store.settings.naming,
          lyric: store.settings.lyric,
          cover: store.settings.cover,
        });
      } catch (error) {
        toast(`预设保存失败：${error.message}`, 'error');
        return;
      }
      const presets = await api.presets();
      state.presets = presets.presets;
      state.newPresetName = '';
      toast(`预设「${name}」已保存`);
    }

    // 看草稿而不是 store：用户每敲一个字预览就刷新，和改动前的手感一致。
    watch(
      () => [draft.dir_template, draft.file_template, draft.singer_separator].join('|'),
      () => refreshPreview(),
    );

    onMounted(async () => {
      if (!store.settings) store.settings = await api.getConfig();
      state.chain = [...store.settings.quality.chain];
      syncDraft();
      await loadMeta();
      await refreshPreview();
    });

    return {
      state, store, config, draft, patch, saveChain, refreshPreview, checkPath,
      applyCombo, activatePreset, savePreset, removePreset, formatBytes,
    };
  },
  template: `
    <div v-if="config">
      <h3 class="pane__title">设置</h3>
      <div class="row row--tight" style="margin-bottom:10px">
        <button class="btn--sm" :class="{'btn--primary': state.section==='quality'}" @click="state.section='quality'">音质</button>
        <button class="btn--sm" :class="{'btn--primary': state.section==='pace'}" @click="state.section='pace'">节奏</button>
        <button class="btn--sm" :class="{'btn--primary': state.section==='naming'}" @click="state.section='naming'">命名</button>
        <button class="btn--sm" :class="{'btn--primary': state.section==='extras'}" @click="state.section='extras'">歌词/封面/Tag</button>
        <button class="btn--sm" :class="{'btn--primary': state.section==='storage'}" @click="state.section='storage'">存储</button>
        <button class="btn--sm" :class="{'btn--primary': state.section==='presets'}" @click="state.section='presets'">预设</button>
      </div>

      <!-- 音质 -->
      <div v-if="state.section==='quality'" class="card">
        <strong>优先级链（拖拽排序）</strong>
        <quality-chain v-model="state.chain" :catalog="store.qualities" />
        <button class="btn--primary btn--sm" @click="saveChain" :disabled="state.saving">保存优先级链</button>

        <div class="field" style="margin-top:14px">
          <label>全部档位都拿不到时</label>
          <select :value="config.quality.on_all_unavailable" @change="patch('quality.on_all_unavailable', $event.target.value)">
            <option value="skip">跳过并记录</option>
            <option value="lowest">用最低可用档位兜底</option>
          </select>
        </div>

        <label class="row"><input type="checkbox" :checked="config.quality.reject_trial"
               @change="patch('quality.reject_trial', $event.target.checked)" /> 拒绝试听文件（检出即换下一档）</label>
        <label class="row"><input type="checkbox" :checked="config.quality.keep_trial_file"
               @change="patch('quality.keep_trial_file', $event.target.checked)" /> 检出试听后保留文件</label>

        <div class="field" style="margin-top:10px">
          <label>请求档位与返回前缀不符时</label>
          <select :value="config.quality.on_prefix_mismatch" @change="patch('quality.on_prefix_mismatch', $event.target.value)">
            <option value="degrade">记为降级</option>
            <option value="fail">判为失败</option>
          </select>
        </div>

        <p class="faint">{{ store.qualityNotes.vip }}</p>
        <p class="faint">{{ store.qualityNotes.encrypted }}</p>
        <p class="faint">{{ store.qualityNotes.acc96 }}</p>
      </div>

      <!-- 节奏 -->
      <div v-if="state.section==='pace'" class="card">
        <label class="row"><input type="checkbox" :checked="config.download.random_interval"
               @change="patch('download.random_interval', $event.target.checked)" /> 使用随机区间（更不容易触发风控）</label>

        <div class="field" v-if="!config.download.random_interval">
          <label>每首之间的间隔（秒）</label>
          <input type="number" :value="config.download.interval_seconds"
                 @change="patch('download.interval_seconds', Number($event.target.value))" />
        </div>
        <div class="row" v-else>
          <div class="field" style="flex:1">
            <label>下界（秒）</label>
            <input type="number" :value="config.download.interval_min_seconds"
                   @change="patch('download.interval_min_seconds', Number($event.target.value))" />
          </div>
          <div class="field" style="flex:1">
            <label>上界（秒）</label>
            <input type="number" :value="config.download.interval_max_seconds"
                   @change="patch('download.interval_max_seconds', Number($event.target.value))" />
          </div>
        </div>

        <div class="row">
          <div class="field" style="flex:1">
            <label>元数据限速下界（秒）</label>
            <input type="number" step="0.1" :value="config.download.metadata_delay_min"
                   @change="patch('download.metadata_delay_min', Number($event.target.value))" />
          </div>
          <div class="field" style="flex:1">
            <label>元数据限速上界（秒）</label>
            <input type="number" step="0.1" :value="config.download.metadata_delay_max"
                   @change="patch('download.metadata_delay_max', Number($event.target.value))" />
          </div>
        </div>

        <div class="field">
          <label>网络错误重试次数</label>
          <input type="number" :value="config.download.max_retries"
                 @change="patch('download.max_retries', Number($event.target.value))" />
        </div>
        <p class="faint">下载严格串行，一首一首来；元数据类请求走独立的轻量限速，不套用 3 分钟间隔。</p>
      </div>

      <!-- 命名 -->
      <div v-if="state.section==='naming'" class="card">
        <div class="field">
          <label>目录模板（其中的 / 就是目录层级）</label>
          <input type="text" v-model="draft.dir_template"
                 @change="patch('naming.dir_template', draft.dir_template)" />
        </div>
        <div class="field">
          <label>文件名模板</label>
          <input type="text" v-model="draft.file_template"
                 @change="patch('naming.file_template', draft.file_template)" />
        </div>
        <div class="field">
          <label>多歌手连接符</label>
          <input type="text" v-model="draft.singer_separator"
                 @change="patch('naming.singer_separator', draft.singer_separator)" />
        </div>

        <strong>预设</strong>
        <div class="row" style="margin:6px 0">
          <button v-for="combo in store.templates.combos" :key="combo.name" class="btn--sm" @click="applyCombo(combo)">
            {{ combo.name }}
          </button>
        </div>

        <strong>实时预览</strong>
        <p class="mono" v-if="state.preview">{{ state.preview.full }}</p>
        <p v-if="state.preview && state.preview.unknown_file_placeholders.length" class="badge badge--warn">
          未知占位符：{{ state.preview.unknown_file_placeholders.join('、') }}
        </p>

        <details style="margin-top:10px">
          <summary>可用占位符</summary>
          <ul class="faint">
            <li v-for="(desc, key) in store.templates.placeholders" :key="key">{{ '{' + key + '}' }} — {{ desc }}</li>
          </ul>
          <p class="faint">{{ store.templates.hint }}</p>
        </details>

        <div class="field" style="margin-top:10px">
          <label>文件名冲突时</label>
          <select :value="config.download.conflict_policy" @change="patch('download.conflict_policy', $event.target.value)">
            <option value="suffix">加 (1)(2) 后缀</option>
            <option value="skip">跳过</option>
            <option value="overwrite">覆盖</option>
          </select>
        </div>
      </div>

      <!-- 歌词 / 封面 / Tag -->
      <div v-if="state.section==='extras'">
        <div class="card">
          <strong>歌词</strong>
          <label class="row"><input type="checkbox" :checked="config.lyric.enabled" @change="patch('lyric.enabled', $event.target.checked)" /> 下载歌词</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.qrc" @change="patch('lyric.qrc', $event.target.checked)" /> 逐字歌词（QRC）</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.translation" @change="patch('lyric.translation', $event.target.checked)" /> 翻译</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.romaji" @change="patch('lyric.romaji', $event.target.checked)" /> 罗马音</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.singing_annotations" @change="patch('lyric.singing_annotations', $event.target.checked)" /> 助唱标注</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.multi_style_translation" @change="patch('lyric.multi_style_translation', $event.target.checked)" /> 多风格翻译</label>
          <div class="field">
            <label>输出格式</label>
            <select :value="config.lyric.output_format" @change="patch('lyric.output_format', $event.target.value)">
              <option value="enhanced_lrc">增强 LRC（逐字 A2）</option>
              <option value="line_lrc">标准逐行 LRC</option>
              <option value="raw_qrc">原样保存 .qrc</option>
            </select>
          </div>
          <div class="field">
            <label>双语处理</label>
            <select :value="config.lyric.bilingual_mode" @change="patch('lyric.bilingual_mode', $event.target.value)">
              <option value="separate_file">译文单独存 .zh.lrc</option>
              <option value="two_lines">同时间戳双行</option>
              <option value="inline">一行内 原文 (译文)</option>
            </select>
          </div>
          <label class="row"><input type="checkbox" :checked="config.lyric.save_as_file" @change="patch('lyric.save_as_file', $event.target.checked)" /> 存为独立 .lrc 文件</label>
          <label class="row"><input type="checkbox" :checked="config.lyric.embed_in_tag" @change="patch('lyric.embed_in_tag', $event.target.checked)" /> 内嵌到 tag</label>
        </div>

        <div class="card">
          <strong>封面</strong>
          <label class="row"><input type="checkbox" :checked="config.cover.enabled" @change="patch('cover.enabled', $event.target.checked)" /> 下载封面</label>
          <div class="field">
            <label>嵌入尺寸</label>
            <select :value="config.cover.embed_size" @change="patch('cover.embed_size', Number($event.target.value))">
              <option v-for="size in [150,300,500,800,1200,1500]" :key="size" :value="size">{{ size }}px</option>
            </select>
          </div>
          <div class="field">
            <label>单独保存尺寸</label>
            <select :value="config.cover.save_size" @change="patch('cover.save_size', Number($event.target.value))">
              <option v-for="size in [150,300,500,800,1200,1500]" :key="size" :value="size">{{ size }}px</option>
            </select>
          </div>
          <label class="row"><input type="checkbox" :checked="config.cover.embed" @change="patch('cover.embed', $event.target.checked)" /> 嵌入音频</label>
          <label class="row"><input type="checkbox" :checked="config.cover.save_file" @change="patch('cover.save_file', $event.target.checked)" /> 单独保存图片</label>
          <div class="field">
            <label>图片文件名</label>
            <input type="text" :value="config.cover.file_name" @change="patch('cover.file_name', $event.target.value)" />
          </div>
          <p class="faint">尺寸是固定六档；高尺寸不一定存在，404 会自动降到下一档。</p>
        </div>

        <div class="card">
          <strong>Tag 字段</strong>
          <label class="row" v-for="key in ['title','artist','album','album_artist','composer','lyricist','year','track_number','disc_number','genre','cover','lyrics','replaygain']" :key="key">
            <input type="checkbox" :checked="config.tag[key]" @change="patch('tag.' + key, $event.target.checked)" /> {{ key }}
          </label>
          <p class="faint">
            {{ '{作曲}{作词}' }} 需要额外请求 song.get_producer（按歌），
            {{ '{流派}' }} 走 album.get_detail（按专辑缓存，整张专辑只请求一次）。
            .nac 是非标准容器，mutagen 不支持，会跳过 tag 写入并标注。
          </p>
        </div>
      </div>

      <!-- 存储 -->
      <div v-if="state.section==='storage'" class="card">
        <div class="field">
          <label>下载根目录</label>
          <input type="text" v-model="draft.save_root" @change="patch('paths.save_root', draft.save_root)" />
        </div>
        <div class="row">
          <button class="btn--sm" @click="checkPath(false)">校验</button>
          <button class="btn--sm" @click="checkPath(true)">创建并校验</button>
        </div>
        <div v-if="state.pathCheck" style="margin-top:8px">
          <span class="badge" :class="state.pathCheck.ok ? 'badge--ok' : 'badge--danger'">
            {{ state.pathCheck.ok ? '可用' : '不可用' }}
          </span>
          <span class="muted">剩余 {{ formatBytes(state.pathCheck.free_bytes) }} / 共 {{ formatBytes(state.pathCheck.total_bytes) }}</span>
          <p class="faint" v-if="state.pathCheck.message">{{ state.pathCheck.message }}</p>
        </div>
      </div>

      <!-- 预设 -->
      <div v-if="state.section==='presets'" class="card">
        <p class="muted">当前预设：<strong>{{ state.activePreset }}</strong></p>
        <div class="row">
          <button class="btn--sm" @click="activatePreset('default')">default</button>
          <!-- 以前只能建不能删：DELETE 端点一直存在，但界面上没有任何入口，
               打错一个名字就永远留在列表里。 -->
          <span v-for="(_, name) in state.presets" :key="name" class="row row--tight">
            <button class="btn--sm" :class="{'btn--primary': name===state.activePreset}"
                    @click="activatePreset(name)">{{ name }}</button>
            <button class="btn--sm btn--danger" :title="'删除预设「' + name + '」'"
                    @click="removePreset(name)">✕</button>
          </span>
        </div>
        <p class="faint">
          切换预设会把预设里的值<strong>一次性写进配置文件</strong>；切回 default 只是改回标签，
          不会还原之前被覆盖的设置。
        </p>
        <div class="field" style="margin-top:12px">
          <label>把当前设置存为新预设</label>
          <input type="text" v-model="state.newPresetName" placeholder="预设名称" />
        </div>
        <button class="btn--sm" @click="savePreset" :disabled="!state.newPresetName">保存预设</button>
      </div>
    </div>
  `,
};
