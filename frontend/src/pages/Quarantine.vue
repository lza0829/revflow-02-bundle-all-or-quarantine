<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')
const bundleFilter = ref('')

async function load() {
  const key = bundleFilter.value.trim()
  rows.value = await getJson(key ? `/api/quarantine?bundle_key=${encodeURIComponent(key)}` : '/api/quarantine')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function applyFilter() {
  err.value = ''
  load().catch((e) => {
    err.value = String(e.message || e)
  })
}

function clearFilter() {
  bundleFilter.value = ''
  applyFilter()
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>未平隔离</h1>
    <p class="lead">冲正无原单、账户不一致、超额或整捆连带失败等未能配平的流水；捆内隔离项共享同一捆扎键。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>按捆扎键过滤</h2>
      <div class="filter-row">
        <input v-model="bundleFilter" placeholder="输入捆扎键，如 BUNDLE-BAD" @keyup.enter="applyFilter" />
        <button class="btn" @click="applyFilter">过滤</button>
        <button class="btn secondary" @click="clearFilter">清除</button>
      </div>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr><th>流水键</th><th>捆扎键</th><th>账户</th><th>原因</th><th>金额（元）</th><th>归并#</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="q in rows" :key="q.id">
            <td>{{ q.entry_key }}</td>
            <td>
              <span v-if="q.bundle_key" class="tag warn">{{ q.bundle_key }}</span>
              <span v-else class="muted">散笔</span>
            </td>
            <td>{{ q.account_code }}</td>
            <td><span class="tag warn">{{ q.reason }}</span></td>
            <td>{{ yuan(q.amount_cents) }}</td>
            <td>{{ q.settle_run_id }}</td>
            <td>{{ q.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7">暂无隔离项（先跑归并）</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.filter-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.filter-row input {
  min-width: 280px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  font: inherit;
  font-family: var(--mono);
  font-size: 13px;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
</style>
