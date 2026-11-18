import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  LedgerDataset,
  LedgerRecord,
  OverviewResult,
  PageResult,
  ReconcileReport,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨模块数据流：获取全平台领域快照 */
export async function loadNodeStats(module?: string): Promise<Record<string, unknown>> {
  const query = module ? `?module=${module}` : ''
  const response = await request(`/api/nodestats${query}`)
  if (!response.ok) throw new Error('节点统计读取失败')
  return await response.json()
}

/** 跨模块数据流：获取指定 topic 的通道消息 */
export async function loadChannelSnapshot(topic: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/channel/${topic}`)
  if (!response.ok) throw new Error('通道快照读取失败')
  return await response.json()
}

/** 跨模块数据流：查询脏数据隔离区 */
export async function loadDirtyVault(source?: string): Promise<Record<string, unknown>> {
  const query = source ? `?source=${source}` : ''
  const response = await request(`/api/dirty-vault${query}`)
  if (!response.ok) throw new Error('脏数据查询失败')
  return await response.json()
}

/** 跨模块数据流：查询采集记录 */
export async function loadCollectRecords(batchRef?: string): Promise<Record<string, unknown>> {
  const query = batchRef ? `?batch_ref=${batchRef}` : ''
  const response = await request(`/api/collect-records${query}`)
  if (!response.ok) throw new Error('采集记录查询失败')
  return await response.json()
}

/** 跨模块数据流：执行RPA全生命周期管道 */
export async function triggerPipeline(lifecycle: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/${lifecycle}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('管道执行失败')
  return await response.json()
}

/** 跨模块数据流：获取失败恢复状态 */
export async function loadRecoveryStats(): Promise<Record<string, unknown>> {
  const response = await request('/api/recovery')
  if (!response.ok) throw new Error('恢复状态查询失败')
  return await response.json()
}

/* ------------------------------------------------------------------ */
/* 定位策略执行台账（处理项癸）                                          */
/* ------------------------------------------------------------------ */

/** 构建定位策略执行记录数据集；重复导入只认第一条记录 */
export async function buildLedgerDataset(ruleIds: number[]): Promise<LedgerDataset> {
  const response = await request('/api/locator-ledger/build', {
    method: 'POST',
    body: JSON.stringify({ rule_ids: ruleIds.length ? ruleIds : null }),
  })
  if (!response.ok) throw new Error('台账数据集构建失败')
  const payload = await response.json()
  return payload.dataset as LedgerDataset
}

/** 台账数据集版本列表 */
export async function loadLedgerDatasets(): Promise<{
  count: number
  datasets: LedgerDataset[]
  latest_version: string | null
}> {
  const response = await request('/api/locator-ledger/datasets')
  if (!response.ok) throw new Error('台账版本读取失败')
  return await response.json()
}

/** 台账记录：每条失败记录都能看到最终采用的选择器和回退路径 */
export async function loadLedgerRecords(
  version?: string,
  status?: string,
): Promise<{ count: number; records: LedgerRecord[] }> {
  const query = new URLSearchParams()
  if (version) query.set('version', version)
  if (status) query.set('status', status)
  const response = await request(`/api/locator-ledger/records?${query}`)
  if (!response.ok) throw new Error('台账记录读取失败')
  return await response.json()
}

/** 运行时会话按版本数据集批量执行，为每个元素生成回执 */
export async function executeLedgerDataset(version?: string): Promise<{
  version?: string
  session_id: string | null
  executed: number
  failed: number
  total?: number
  receipts: { receipt_id: string }[]
  message?: string
}> {
  const response = await request('/api/locator-ledger/execute', {
    method: 'POST',
    body: JSON.stringify({ version: version ?? null }),
  })
  if (!response.ok) throw new Error('批量执行失败')
  const payload = await response.json()
  return payload.result
}

/** 异常元素重新测试，生成新的执行回执 */
export async function retestLedgerRecords(recordIds: string[]): Promise<{
  retested: number
  results: { record_id: string; ok: boolean; receipt_id?: string; message?: string }[]
}> {
  const response = await request('/api/locator-ledger/retest', {
    method: 'POST',
    body: JSON.stringify({ record_ids: recordIds }),
  })
  if (!response.ok) throw new Error('重新测试失败')
  const payload = await response.json()
  return payload.result
}

/** 核对台账记录与回执明细：残留 / 丢失 / 错位 / 重复 */
export async function loadLedgerReconcile(): Promise<ReconcileReport> {
  const response = await request('/api/locator-ledger/reconcile')
  if (!response.ok) throw new Error('核对结果读取失败')
  const payload = await response.json()
  return payload.reconcile as ReconcileReport
}
