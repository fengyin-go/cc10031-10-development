/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 定位策略执行台账（处理项癸）：每次元素解析只保留一个最终定位。 */
export type LedgerSelector = {
  type: string
  expression: string
  source: string
}

export type LedgerFallbackStep = {
  source: string
  selector_type: string
  expression: string
  outcome: string
  error: string
}

export type LedgerRecord = {
  record_id: string
  dataset_version: string
  rule_id: number
  rule_name: string
  element: string
  bot_ref: string
  primary_selector: { type: string; expression: string }
  primary_outcome: string
  first_error: string
  fallback_path: LedgerFallbackStep[]
  fallback_candidates: { selector_type: string; expression: string }[]
  final_selector: LedgerSelector | null
  final_strategy: string
  session_id: string | null
  receipt_id: string | null
  status: string
  pending: boolean
  abnormal: boolean
  updated_at: string
}

export type LedgerDataset = {
  version: string
  created_at: string
  created: number
  skipped_duplicates: number
  session_msgs_consumed: number
  rule_scope: number[]
}

export type LedgerReceipt = {
  receipt_id: string
  record_id: string
  dataset_version: string
  element: string
  final_selector: LedgerSelector | null
  final_strategy: string
  outcome: string
  first_error: string
  fallback_path: LedgerFallbackStep[]
  session_id: string
  duration_ms: number
  superseded: boolean
  created_at: string
}

export type ReconcileReport = {
  记录总数: number
  有效回执数: number
  残留: { record_id: string; rule_name: string; status: string }[]
  丢失: { receipt_id: string; record_id: string }[]
  错位: { record_id: string; record_final: string; receipt_id: string; receipt_final: string }[]
  重复: { record_id: string; receipts: string[] }[]
  consistent: boolean
}
