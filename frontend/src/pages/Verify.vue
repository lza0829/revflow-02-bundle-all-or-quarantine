<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  rows.value = await getJson('/api/verify-reports')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runVerify() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/verify-reports')
    msg.value = r.consistent
      ? `互证一致：流水净额 ${(r.ledger_net_cents / 100).toFixed(2)} = 余额合计；成功捆 ${r.detail.累计成功捆数}，失败捆 ${r.detail.累计失败捆数}`
      : `互证不一致：流水 ${(r.ledger_net_cents / 100).toFixed(2)} vs 余额 ${(r.balance_sum_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>净额互证</h1>
    <p class="lead">核对「已归并流水净额」与「账户余额合计」是否一致；报告同时按捆解释成功捆、失败捆与成员笔数，与捆扎记录、流水、隔离、余额同源。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runVerify">跑净额互证</button>
    </div>
    <p v-if="msg" :class="msg.includes('不一致') ? 'msg' : 'ok'">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel" v-if="rows.length && rows[0].detail">
      <h2>最近报告 · 按捆解释</h2>
      <div class="stats">
        <div class="stat"><div class="k">本轮成功捆</div><div class="v">{{ rows[0].detail.本轮成功捆数 }}</div></div>
        <div class="stat"><div class="k">本轮失败捆</div><div class="v">{{ rows[0].detail.本轮失败捆数 }}</div></div>
        <div class="stat"><div class="k">本轮捆内成员</div><div class="v">{{ rows[0].detail.本轮捆内成员笔数 }}</div></div>
        <div class="stat"><div class="k">累计成功捆</div><div class="v">{{ rows[0].detail.累计成功捆数 }}</div></div>
        <div class="stat"><div class="k">累计失败捆</div><div class="v">{{ rows[0].detail.累计失败捆数 }}</div></div>
        <div class="stat"><div class="k">待归捆</div><div class="v">{{ rows[0].detail.待归捆数 }}</div></div>
      </div>
    </div>

    <div class="panel">
      <h2>互证报告</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>归并#</th><th>流水净额</th><th>余额合计</th><th>隔离数</th>
            <th>成/败捆</th><th>成员笔数</th><th>一致</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.settle_run_id }}</td>
            <td>{{ yuan(r.ledger_net_cents) }}</td>
            <td>{{ yuan(r.balance_sum_cents) }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>
              <template v-if="r.detail">
                {{ r.detail.本轮成功捆数 }} / {{ r.detail.本轮失败捆数 }}
              </template>
              <span v-else>—</span>
            </td>
            <td>{{ r.detail ? r.detail.本轮捆内成员笔数 : '—' }}</td>
            <td>
              <span class="tag" :class="r.consistent ? 'on' : 'bad'">
                {{ r.consistent ? '是' : '否' }}
              </span>
            </td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9">尚无报告</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
