<template>
  <section class="page" data-module="locator_ledger">
    <header class="page-head">
      <div>
        <h2>定位策略执行台账</h2>
        <p class="page-desc">
          批量执行已启用定位规则并留存回退证据：每条记录只保留一个最终定位，
          失败记录可核对首次错误、最终采用的选择器和回退路径。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="build-dataset" type="button"
                :disabled="building || !selectedRuleIds.length" @click="buildDataset">
          {{ building ? '构建中…' : `构建执行记录数据集（已选 ${selectedRuleIds.length} 条规则）` }}
        </button>
        <button class="btn" data-testid="execute-dataset" type="button"
                :disabled="executing || !records.length" @click="executeDataset">
          {{ executing ? '执行中…' : '批量执行并生成回执' }}
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">台账记录</span>
        <strong class="stat-value">{{ records.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待执行</span>
        <strong class="stat-value">{{ countByStatus('待执行') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">执行成功</span>
        <strong class="stat-value">{{ countByStatus('执行成功') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">失败/异常</span>
        <strong class="stat-value">{{ abnormalCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">回退恢复</span>
        <strong class="stat-value">{{ fallbackRecoveredCount }}</strong>
      </article>
    </div>

    <section class="ledger-panel">
      <h3>已启用定位规则（批量选择后构建数据集）</h3>
      <p v-if="!enabledRules.length" class="empty-state">
        暂无已启用定位规则，请先在「元素定位」模块启用规则。
      </p>
      <label v-for="rule in enabledRules" :key="rule.id" class="rule-item"
             :data-testid="`rule-${rule.id}`">
        <input v-model="selectedRuleIds" type="checkbox" :value="Number(rule.id)" />
        <span class="rule-name">{{ rule.规则名称 }}</span>
        <span class="rule-expr">{{ rule.选择器类型 }}:{{ rule.定位表达式 }}</span>
        <span class="rule-fallback">回退：{{ rule.回退方案 || '无' }}</span>
      </label>
      <div class="panel-actions">
        <button class="link" type="button" data-testid="select-all-rules" @click="toggleAllRules">
          {{ selectedRuleIds.length === enabledRules.length ? '取消全选' : '全选规则' }}
        </button>
        <span v-if="datasetMessage" class="muted-text">{{ datasetMessage }}</span>
      </div>
    </section>

    <section class="ledger-panel">
      <div class="panel-head">
        <h3>定位策略执行记录</h3>
        <div class="panel-tools">
          <label class="version-picker">
            <span>数据集版本</span>
            <select v-model="activeVersion" data-testid="version-select" @change="reloadRecords">
              <option value="">全部版本</option>
              <option v-for="ds in datasets" :key="ds.version" :value="ds.version">
                {{ ds.version }}（新增 {{ ds.created }}，去重 {{ ds.skipped_duplicates }}）
              </option>
            </select>
          </label>
          <button class="btn ghost" type="button" data-testid="reconcile" @click="runReconcile">
            核对记录与回执
          </button>
        </div>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>记录编号</th>
            <th>元素</th>
            <th>最终策略</th>
            <th>最终选择器</th>
            <th>首次错误</th>
            <th>执行会话</th>
            <th>回执</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in records" :key="row.record_id">
            <tr :data-testid="`ledger-${row.record_id}`" :data-status="row.status"
                :class="{ 'row-abnormal': row.abnormal }">
              <td>{{ row.record_id }}</td>
              <td>{{ row.element }}</td>
              <td>
                <span class="strategy-tag" :class="strategyClass(row.final_strategy)">
                  {{ row.final_strategy }}
                </span>
              </td>
              <td>
                <code v-if="row.final_selector">
                  {{ row.final_selector.type }}:{{ row.final_selector.expression }}
                </code>
                <span v-else>—</span>
                <span v-if="row.fallback_candidates.length" class="candidate-tip"
                      :title="candidateTip(row)">
                  +{{ row.fallback_candidates.length }} 回退候选
                </span>
              </td>
              <td class="error-cell">{{ row.first_error || '—' }}</td>
              <td>{{ row.session_id || '—' }}</td>
              <td>{{ row.receipt_id || '—' }}</td>
              <td>{{ row.status }}</td>
              <td class="row-actions">
                <button class="link" type="button"
                        :data-testid="`expand-${row.record_id}`"
                        @click="toggleExpand(row.record_id)">
                  {{ expanded.has(row.record_id) ? '收起路径' : '回退路径' }}
                </button>
                <button v-if="row.abnormal || row.status === '执行失败'" class="link"
                        type="button" :data-testid="`retest-${row.record_id}`"
                        :disabled="retesting.has(row.record_id)"
                        @click="retest(row.record_id)">
                  {{ retesting.has(row.record_id) ? '重测中…' : '重新测试' }}
                </button>
              </td>
            </tr>
            <tr v-if="expanded.has(row.record_id)" class="fallback-row">
              <td colspan="9">
                <ol class="fallback-list">
                  <li v-for="(step, i) in row.fallback_path" :key="i"
                      :class="step.outcome === '命中' ? 'step-hit' : 'step-miss'">
                    {{ step.source }} — {{ step.selector_type }}:{{ step.expression }}
                    → {{ step.outcome }}<span v-if="step.error">（{{ step.error }}）</span>
                  </li>
                  <li v-if="!row.fallback_path.length" class="muted-text">
                    尚未执行，批量执行后在此展示主/回退尝试轨迹。
                  </li>
                </ol>
              </td>
            </tr>
          </template>
          <tr v-if="!records.length">
            <td colspan="9" class="empty-state">
              暂无台账记录，请先批量选择规则并构建执行记录数据集
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="reconcileReport" class="ledger-panel" data-testid="reconcile-panel">
      <div class="panel-head">
        <h3>核对结果：业务记录 vs 结果明细</h3>
        <span class="reconcile-badge" :class="reconcileReport.consistent ? 'ok' : 'bad'">
          {{ reconcileReport.consistent ? '记录与明细一致' : '存在差异，请处理' }}
        </span>
      </div>
      <p class="muted-text">
        台账记录 {{ reconcileReport.记录总数 }} 条，有效回执 {{ reconcileReport.有效回执数 }} 条。
      </p>
      <div class="reconcile-grid">
        <div class="reconcile-cell">
          <h4>残留（{{ reconcileReport.残留.length }}）</h4>
          <p v-if="!reconcileReport.残留.length" class="muted-text">无已办结但缺回执的记录</p>
          <p v-for="item in reconcileReport.残留" :key="item.record_id">
            {{ item.record_id }} · {{ item.rule_name }}（{{ item.status }}）
          </p>
        </div>
        <div class="reconcile-cell">
          <h4>丢失（{{ reconcileReport.丢失.length }}）</h4>
          <p v-if="!reconcileReport.丢失.length" class="muted-text">无指向缺失记录的回执</p>
          <p v-for="item in reconcileReport.丢失" :key="item.receipt_id">
            {{ item.receipt_id }} → 记录 {{ item.record_id }} 不存在
          </p>
        </div>
        <div class="reconcile-cell">
          <h4>错位（{{ reconcileReport.错位.length }}）</h4>
          <p v-if="!reconcileReport.错位.length" class="muted-text">回执与记录的最终选择器一致</p>
          <p v-for="item in reconcileReport.错位" :key="item.receipt_id">
            {{ item.record_id }}：记录={{ item.record_final }} ≠ 回执={{ item.receipt_final }}
          </p>
        </div>
        <div class="reconcile-cell">
          <h4>重复（{{ reconcileReport.重复.length }}）</h4>
          <p v-if="!reconcileReport.重复.length" class="muted-text">每条记录仅一条有效回执</p>
          <p v-for="item in reconcileReport.重复" :key="item.record_id">
            {{ item.record_id }}：{{ item.receipts.join('、') }}
          </p>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ records.length }} 条定位策略执行记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildLedgerDataset,
  executeLedgerDataset,
  listEntries,
  loadLedgerDatasets,
  loadLedgerReconcile,
  loadLedgerRecords,
  retestLedgerRecords,
} from '@/api/backend-service'
import type { EntryRow, LedgerDataset, LedgerRecord, ReconcileReport } from '@/data/types'

const enabledRules = ref<EntryRow[]>([])
const selectedRuleIds = ref<number[]>([])
const datasets = ref<LedgerDataset[]>([])
const records = ref<LedgerRecord[]>([])
const activeVersion = ref('')
const reconcileReport = ref<ReconcileReport | null>(null)
const expanded = ref<Set<string>>(new Set())
const retesting = ref<Set<string>>(new Set())
const building = ref(false)
const executing = ref(false)
const datasetMessage = ref('')
const errorMessage = ref('')

const abnormalCount = computed(
  () => records.value.filter((r) => r.abnormal || r.status === '执行失败').length,
)
const fallbackRecoveredCount = computed(
  () => records.value.filter((r) => r.final_strategy === '回退选择器').length,
)

function countByStatus(status: string): number {
  return records.value.filter((r) => r.status === status).length
}

function strategyClass(strategy: string): string {
  if (strategy === '主选择器') return 'primary'
  if (strategy === '回退选择器') return 'fallback'
  return 'none'
}

function candidateTip(row: LedgerRecord): string {
  const lines = row.fallback_candidates.map(
    (c) => `${c.selector_type}:${c.expression}`,
  )
  return `主选择器已命中，回退仅作候选（不执行）：\n${lines.join('\n')}`
}

function toggleExpand(recordId: string) {
  const next = new Set(expanded.value)
  if (next.has(recordId)) next.delete(recordId)
  else next.add(recordId)
  expanded.value = next
}

function toggleAllRules() {
  if (selectedRuleIds.value.length === enabledRules.value.length) {
    selectedRuleIds.value = []
  } else {
    selectedRuleIds.value = enabledRules.value.map((r) => Number(r.id))
  }
}

async function buildDataset() {
  building.value = true
  errorMessage.value = ''
  datasetMessage.value = ''
  try {
    const ds = await buildLedgerDataset(selectedRuleIds.value)
    datasetMessage.value =
      `版本 ${ds.version} 已生成：新增 ${ds.created} 条，重复导入跳过 ${ds.skipped_duplicates} 条（只认第一条）`
    selectedRuleIds.value = []
    await Promise.all([reloadDatasets(), reloadRecords()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据集构建失败'
  } finally {
    building.value = false
  }
}

async function executeDataset() {
  executing.value = true
  errorMessage.value = ''
  try {
    const result = await executeLedgerDataset(activeVersion.value || undefined)
    if (result.message && !result.total) {
      datasetMessage.value = result.message
    } else {
      datasetMessage.value =
        `会话 ${result.session_id}：执行 ${result.total} 条，成功 ${result.executed}，失败 ${result.failed}，回执 ${result.receipts.length} 条`
    }
    await Promise.all([reloadRecords(), runReconcile()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量执行失败'
  } finally {
    executing.value = false
  }
}

async function retest(recordId: string) {
  retesting.value = new Set([...retesting.value, recordId])
  errorMessage.value = ''
  try {
    const result = await retestLedgerRecords([recordId])
    const item = result.results[0]
    datasetMessage.value = item?.receipt_id
      ? `记录 ${recordId} 重测完成，新回执 ${item.receipt_id}`
      : `记录 ${recordId} 重测失败：${item?.message ?? '未知原因'}`
    await Promise.all([reloadRecords(), runReconcile()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新测试失败'
  } finally {
    const next = new Set(retesting.value)
    next.delete(recordId)
    retesting.value = next
  }
}

async function runReconcile() {
  try {
    reconcileReport.value = await loadLedgerReconcile()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function reloadRules() {
  const payload = await listEntries('element_selector', { status: '已启用' })
  enabledRules.value = payload.items
}

async function reloadDatasets() {
  const payload = await loadLedgerDatasets()
  datasets.value = payload.datasets
}

async function reloadRecords() {
  const payload = await loadLedgerRecords(activeVersion.value || undefined)
  records.value = payload.records
}

onMounted(async () => {
  errorMessage.value = ''
  try {
    await Promise.all([reloadRules(), reloadDatasets(), reloadRecords()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '台账数据读取失败'
  }
})
</script>

<style scoped>
.ledger-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.ledger-panel h3 { margin: 0 0 10px; font-size: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.panel-head h3 { margin: 0; }
.panel-tools { display: flex; gap: 10px; align-items: center; }
.panel-actions { display: flex; gap: 12px; align-items: center; margin-top: 8px; }
.rule-item {
  display: flex; gap: 10px; align-items: center; padding: 6px 4px;
  border-bottom: 1px solid #f0f2f5; font-size: 13px; cursor: pointer;
}
.rule-name { font-weight: 600; }
.rule-expr { color: var(--muted); font-family: monospace; }
.rule-fallback { color: var(--muted); }
.version-picker { display: flex; gap: 6px; align-items: center; font-size: 12px; color: var(--muted); }
.version-picker select { padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px; }
.strategy-tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.strategy-tag.primary { background: #e7f0fe; color: #1f6feb; }
.strategy-tag.fallback { background: #fef3e2; color: #b45309; }
.strategy-tag.none { background: #fee2e2; color: #b42318; }
.candidate-tip { margin-left: 6px; color: var(--muted); font-size: 12px; cursor: help; white-space: pre-line; }
.error-cell { color: #b42318; max-width: 220px; }
.row-abnormal td { background: #fff7f7; }
.fallback-row td { background: #fafbfc; }
.fallback-list { margin: 0; padding-left: 20px; font-size: 12px; }
.fallback-list li { margin: 2px 0; }
.step-hit { color: #15803d; }
.step-miss { color: #b42318; }
.muted-text { color: var(--muted); font-size: 12px; }
.reconcile-badge { padding: 4px 10px; border-radius: 10px; font-size: 12px; }
.reconcile-badge.ok { background: #dcfce7; color: #15803d; }
.reconcile-badge.bad { background: #fee2e2; color: #b42318; }
.reconcile-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.reconcile-cell { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font-size: 12px; }
.reconcile-cell h4 { margin: 0 0 6px; font-size: 13px; }
.reconcile-cell p { margin: 4px 0; }
code { font-size: 12px; }
</style>
