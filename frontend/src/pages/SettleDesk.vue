<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const bundles = ref([])
const reversals = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const busyKey = ref('')

// 捆扎声明表单
const bundleKey = ref('')
const selectedKeys = ref([])
const manualKeys = ref('')

async function load() {
  const [r, b, e] = await Promise.all([
    getJson('/api/settle-runs'),
    getJson('/api/reversal-bundles'),
    getJson('/api/entries?kind=冲正'),
  ])
  runs.value = r
  bundles.value = b
  reversals.value = e
}

const pendingReversals = computed(() =>
  reversals.value.filter((e) => e.status === '待归并' && !e.bundle_key)
)

const pendingBundles = computed(() =>
  bundles.value.filter((b) => b.status === '待归捆')
)

function toggleKey(key) {
  const i = selectedKeys.value.indexOf(key)
  if (i >= 0) selectedKeys.value.splice(i, 1)
  else selectedKeys.value.push(key)
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

async function declareBundle() {
  err.value = ''
  msg.value = ''
  const keys = new Set([
    ...selectedKeys.value,
    ...manualKeys.value.split(/[\s,，、]+/).map((s) => s.trim()).filter(Boolean),
  ])
  if (!bundleKey.value.trim()) {
    err.value = '请填写捆扎键'
    return
  }
  if (!keys.size) {
    err.value = '请勾选或填写至少一笔成员冲正'
    return
  }
  busy.value = true
  try {
    const b = await postJson('/api/reversal-bundles', {
      bundle_key: bundleKey.value.trim(),
      entry_keys: [...keys],
    })
    msg.value = `捆扎 ${b.bundle_key} 已声明，成员 ${b.member_count} 笔，状态：${b.status}`
    bundleKey.value = ''
    selectedKeys.value = []
    manualKeys.value = ''
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function settleBundle(key) {
  err.value = ''
  msg.value = ''
  busyKey.value = key
  try {
    const r = await postJson(`/api/reversal-bundles/${encodeURIComponent(key)}/settle`)
    if (r.bundle_fail_count) {
      msg.value = `捆 ${key} 整捆失败：${r.quarantine_count} 笔成员全部进未平隔离，正式余额未改`
    } else {
      msg.value = `捆 ${key} 整捆归并成功：${r.settled_count} 笔已归并并改余额`
    }
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyKey.value = ''
  }
}

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${yuan(r.net_cents)}，隔离 ${r.quarantine_count}，成功捆 ${r.bundle_success_count}，失败捆 ${r.bundle_fail_count}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function statusTag(status) {
  if (status === '整捆已归并') return 'on'
  if (status === '整捆已隔离') return 'bad'
  return 'warn'
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">
      一批冲正可声明同一捆扎键：同捆全部可解析原单且各自不超额，才整捆进入已归并并改余额；
      任一腿不满足则整捆进未平隔离，正式余额分毫不动。
    </p>
    <p class="note-strip">
      成员状态、余额、隔离、捆状态在同一事务内提交（失败闭）；捆扎集合落库于独立捆扎表，与散笔隔离。
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel">
      <h2>声明冲正捆扎</h2>
      <div class="form-grid">
        <label class="field">
          <span>捆扎键</span>
          <input v-model="bundleKey" placeholder="如 BUNDLE-20261117-01" />
        </label>
      </div>
      <p class="field-hint">勾选待归并散笔冲正（已入捆的不重复列出），或在下方手工补录冲正键：</p>
      <table class="pick-table">
        <thead>
          <tr><th style="width:36px"></th><th>冲正键</th><th>账户</th><th>金额（元）</th><th>原单键</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in pendingReversals" :key="e.id">
            <td>
              <input
                type="checkbox"
                :checked="selectedKeys.includes(e.entry_key)"
                @change="toggleKey(e.entry_key)"
              />
            </td>
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.target_entry_key || '—' }}</td>
          </tr>
          <tr v-if="!pendingReversals.length">
            <td colspan="5">暂无可入捆的待归并散笔冲正</td>
          </tr>
        </tbody>
      </table>
      <label class="field" style="margin-top:10px">
        <span>手工补录冲正键（逗号/空格分隔，可选）</span>
        <input v-model="manualKeys" placeholder="REV-xxx, REV-yyy" />
      </label>
      <div class="row-actions" style="margin-top:12px">
        <button class="btn" :disabled="busy" @click="declareBundle">声明捆扎</button>
      </div>
    </div>

    <div class="panel">
      <h2>捆扎状态（按捆归并）</h2>
      <table>
        <thead>
          <tr>
            <th>捆扎键</th><th>状态</th><th>成员笔数</th><th>成员冲正键</th>
            <th>失败原因</th><th>归并#</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in bundles" :key="b.id">
            <td>{{ b.bundle_key }}</td>
            <td><span class="tag" :class="statusTag(b.status)">{{ b.status }}</span></td>
            <td>{{ b.member_count }}</td>
            <td class="mono-cell">
              <span v-for="m in b.members" :key="m.entry_key" class="chip">{{ m.entry_key }}</span>
            </td>
            <td>{{ b.fail_reason || '—' }}</td>
            <td>{{ b.settle_run_id || '—' }}</td>
            <td>
              <button
                v-if="b.status === '待归捆'"
                class="btn secondary"
                :disabled="busyKey === b.bundle_key"
                @click="settleBundle(b.bundle_key)"
              >按捆归并</button>
              <span v-else class="muted">已处理</span>
            </td>
          </tr>
          <tr v-if="!bundles.length"><td colspan="7">尚无捆扎</td></tr>
        </tbody>
      </table>
      <p v-if="pendingBundles.length" class="field-hint">
        待归捆 {{ pendingBundles.length }} 捆；也可用下方全量归并一次性处理全部散笔与待归捆。
      </p>
    </div>

    <div class="panel">
      <h2>全量净额归并（散笔 + 待归捆）</h2>
      <div class="row-actions">
        <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th><th>范围</th><th>捆扎键</th><th>状态</th><th>已归并</th>
            <th>隔离</th><th>成捆/败捆</th><th>成员笔数</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.scope }}</td>
            <td>{{ r.bundle_key || '—' }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ r.bundle_success_count }} / {{ r.bundle_fail_count }}</td>
            <td>{{ r.bundle_member_count }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="10">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.form-grid {
  display: grid;
  grid-template-columns: minmax(260px, 420px);
  gap: 12px;
  margin-bottom: 12px;
}
.field {
  display: grid;
  gap: 6px;
  font-size: 13px;
  color: var(--muted);
}
.field input {
  padding: 9px 10px;
  border: 1px solid var(--line);
  font: inherit;
  font-family: var(--mono);
  font-size: 13px;
}
.field-hint {
  margin: 4px 0 8px;
  font-size: 12.5px;
  color: var(--muted);
}
.pick-table {
  max-height: 260px;
  overflow-y: auto;
}
.mono-cell {
  font-family: var(--mono);
  font-size: 12px;
}
.chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 2px 6px;
  background: #eef3f9;
  border: 1px solid var(--line);
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
</style>
