import {
  anomalies,
  initialMessages,
  purchaseDrafts,
  shortageItems,
  stockItems,
  traceEvents,
  type AiMessage,
  type Anomaly,
  type PurchaseDraft,
  type ShortageItem,
  type StockItem,
  type Todo,
  type TraceEvent,
} from './data'
import { formatDateTime, formatUnit, localeForApi } from './i18n'

export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'
export const isMockMode = import.meta.env.VITE_USE_MOCK === 'true'

export const demoUsers = {
  PROJECT_OWNER: 'owner@example.test',
  PROCUREMENT: 'procurement@example.test',
  SITE_ADMIN: 'site@example.test',
  SUPPLIER: 'supplier@example.test',
  INSPECTOR: 'inspector@example.test',
  AUDITOR: 'auditor@example.test',
} as const

let activeDemoUser: string = demoUsers.PROJECT_OWNER

export function setDemoUser(username: string) {
  activeDemoUser = username
}

const wait = (ms = 180) => new Promise((resolve) => setTimeout(resolve, ms))

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function readError(response: Response) {
  try {
    const body = await response.json() as { error?: { message?: string; code?: string } }
    return body.error?.message || `${response.status} ${response.statusText}`
  } catch {
    return `${response.status} ${response.statusText}`
  }
}

function requestHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return { Accept: 'application/json', 'X-User-Id': activeDemoUser, 'X-Locale': localeForApi(), ...extra }
}

async function getWithFallback<T>(path: string, fallback: () => T): Promise<T> {
  if (isMockMode) {
    await wait()
    return fallback()
  }
  const response = await fetch(`${API_BASE}${path}`, { headers: requestHeaders() })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = (await response.json()) as { data?: T }
  return body.data ?? (body as T)
}

async function postWithFallback<T>(path: string, payload: unknown, fallback: () => T): Promise<T> {
  if (isMockMode) {
    await wait(240)
    return fallback()
  }

  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      ...requestHeaders({ 'Content-Type': 'application/json' }),
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = (await response.json()) as { data?: T }
  return body.data ?? (body as T)
}

async function postRequest<T>(path: string, payload: unknown, idempotencyKey?: string): Promise<T> {
  if (isMockMode) throw new ApiError('显式演示数据模式不执行业务写入')
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      ...requestHeaders({ 'Content-Type': 'application/json' }),
      'Idempotency-Key': idempotencyKey ?? crypto.randomUUID(),
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = await response.json() as { data?: T }
  return body.data ?? (body as T)
}

async function patchRequest<T>(path: string, payload: unknown): Promise<T> {
  if (isMockMode) throw new ApiError('显式演示数据模式不执行业务写入')
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'PATCH',
    headers: { ...requestHeaders({ 'Content-Type': 'application/json' }), 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = await response.json() as { data?: T }
  return body.data ?? (body as T)
}

async function deleteRequest<T>(path: string, payload: unknown = {}): Promise<T> {
  if (isMockMode) throw new ApiError('显式演示数据模式不执行业务写入')
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'DELETE',
    headers: {
      ...requestHeaders({ 'Content-Type': 'application/json' }),
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = await response.json() as { data?: T }
  return body.data ?? (body as T)
}

type ApiEnvelope = { data?: unknown }
type ApiRecord = Record<string, unknown>

function records(value: unknown): ApiRecord[] {
  if (Array.isArray(value)) return value.filter((item): item is ApiRecord => Boolean(item) && typeof item === 'object')
  if (value && typeof value === 'object' && Array.isArray((value as ApiRecord).items)) return records((value as ApiRecord).items)
  return []
}

function text(value: unknown, fallback = ''): string {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

function displayDate(value: unknown, fallback: string): string {
  const raw = text(value, fallback)
  if (!raw || raw === fallback) return raw
  return formatDateTime(raw)
}

function displayUnit(unit: string) {
  return formatUnit(unit)
}

function stockStatus(item: ApiRecord): StockItem['status'] {
  const available = Number(text(item.available))
  const safety = Number(text(item.safety_stock))
  const quarantined = Number(text(item.qty_quarantined))
  if (quarantined > 0 && available <= 0) return '隔离'
  return available < safety ? '偏低' : '正常'
}

function adaptStock(value: unknown): StockItem[] {
  return records(value).map((item) => ({
    id: text(item.id),
    materialId: text(item.material_id),
    sku: text(item.sku),
    material: text(item.name, '未命名物料'),
    category: text(item.category) === 'PPE' ? 'PPE' : text(item.category) === 'FIRST_AID' ? '急救物资' : '施工物料',
    specification: text(item.specification, '—'),
    warehouse: `${text(item.warehouse_name, '仓库')} · ${text(item.warehouse_code)}`,
    warehouseId: text(item.warehouse_id) || undefined,
    batch: text(item.batch_no, text(item.batch_id, '无批次')),
    batchId: text(item.batch_id) || undefined,
    available: text(item.available, '0'),
    onHand: text(item.qty_on_hand, '0'),
    reserved: text(item.qty_reserved, '0'),
    quarantined: text(item.qty_quarantined, '0'),
    safetyStock: text(item.safety_stock, '0'),
    unit: displayUnit(text(item.unit, 'piece')),
    status: stockStatus(item),
    updatedAt: displayDate(item.updated_at, '读取时刻'),
  }))
}

function adaptShortage(value: unknown): ShortageItem[] {
  const root = value && typeof value === 'object' ? value as ApiRecord : {}
  return records(root.items).filter((item) => Number(text(item.shortage_qty)) > 0).map((item, index) => {
    const shortage = text(item.shortage_qty, '0')
    const riskCode = text(item.risk_level, index === 0 ? 'HIGH' : index === 1 ? 'MEDIUM' : 'LOW')
    const risk: ShortageItem['risk'] = riskCode === 'HIGH' ? '高' : riskCode === 'LOW' ? '低' : '中'
    return {
      id: text(item.material_id),
      material: text(item.material_name, text(item.material_id).slice(0, 8)),
      area: text(item.area_name, text(item.area_id, '项目范围')),
      risk,
      riskDate: text(item.shortage_date, '待确认'),
      available: text(item.available_now, '0'),
      safetyStock: text(item.safety_stock, '0'),
      shortage,
      orderQty: text(item.recommended_order_qty, shortage),
      unit: displayUnit(text(item.unit, 'piece')),
      unitCode: text(item.unit, 'piece'),
      calcId: text(root.calculation_id, 'shortage-v1'),
      source: Array.isArray(item.source_refs) && item.source_refs.length ? item.source_refs.map(String).join(' · ') : 'stock_balances · material_plans · materials',
    }
  })
}

function adaptTrace(value: unknown): TraceEvent[] {
  return records(value && typeof value === 'object' ? (value as ApiRecord).events : value).map((item) => {
    const type = text(item.event_type, 'INSPECTION') as TraceEvent['type']
    const state = text(item.status)
    return {
      id: text(item.id), type,
      label: ({ SHIPMENT: '供应商发货', DELIVERY: '现场到货', INSPECTION: '质量验收', HANDOVER: '物料交接' } as Record<string, string>)[type] ?? type,
      status: state === 'CONFIRMED' ? '已确认' : state === 'SUBMITTED' ? '已提交' : state === 'FAILED' ? '链上失败' : state === 'RETRYING' ? '重试中' : '待上链',
      time: displayDate(item.occurred_at, '刚刚'),
      actor: text(item.actor_name, '已授权项目成员'), reference: text(item.business_reference, text(item.business_document_id)), txId: text(item.tx_id) || undefined,
      hash: text(item.payload_hash, '待生成').slice(0, 12), detail: text(item.detail, '链上仅保存经规范化后的事件摘要和文件哈希。'),
      tone: type === 'SHIPMENT' ? 'blue' : type === 'DELIVERY' ? 'green' : type === 'INSPECTION' ? 'orange' : 'orange',
    }
  })
}

function adaptAnomalies(value: unknown): Anomaly[] {
  return records(value).map((item) => {
    const payload = item.payload && typeof item.payload === 'object' ? item.payload as ApiRecord : {}
    const severity = text(item.severity, 'MEDIUM')
    const status = text(item.status, 'OPEN')
    const types = Array.isArray(payload.types) ? payload.types.map(String) : [text(item.type, 'ANOMALY')]
    const title = types.includes('PLAN_VARIANCE')
      ? `计划偏差：实际 ${text(payload.actual_net, '—')} 高于计划 ${text(payload.planned_qty, '—')}`
      : types.includes('PROCESS_REPEAT')
        ? `24 小时内重复领用 ${text(payload.repeat_count_24h, '多次')}`
        : text(payload.title, text(item.type, '消耗异常'))
    return {
      id: text(item.id), title,
      material: text(item.material_name, text(item.material_id, '待复核物料')), area: text(payload.area, '项目范围'),
      level: severity === 'HIGH' || severity === 'CRITICAL' ? '高' : severity === 'LOW' ? '低' : '中',
      status: status === 'RESOLVED' ? '已解决' : status === 'ACKNOWLEDGED' ? '已知悉' : '待确认',
      possibleCause: text((payload.possible_causes as string[] | undefined)?.[0], '需要核对计划和原始流水'),
      time: text(payload.date, text(item.created_at, '未知时间')),
      sourceRef: text(item.dedupe_key, text(item.id)),
      payload,
    }
  })
}

function adaptPurchaseRequests(value: unknown): PurchaseDraft[] {
  return records(value).flatMap((request) => {
    const items = records(request.items)
    return items.map((item, index) => ({
      id: `${text(request.id)}:${text(item.id, String(index))}`,
      requestId: text(request.id),
      requestNo: text(request.request_no),
      material: text(item.material_name, text(item.specification_snapshot, text(item.material_id, '未命名物料'))),
      quantity: text(item.qty, '0'),
      unit: displayUnit(text(item.unit, 'piece')),
      unitCode: text(item.unit, 'piece'),
      neededBy: text(request.needed_by, '待确认'),
      warehouse: text(item.warehouse_name, text(item.warehouse_code, text(item.warehouse_id, '项目仓库'))),
      status: text(request.status) === 'PENDING_APPROVAL' ? '已提交' : text(request.status) === 'DRAFT' ? '待确认' : text(request.status) === 'APPROVED' ? '已批准' : text(request.status) === 'REJECTED' ? '已驳回' : '已取消',
      rawStatus: text(request.status),
      createdAt: displayDate(request.created_at, '已创建'),
      risk: '中',
      calcId: text(item.shortage_calc_id, '人工申请'),
      reason: text(item.reason, text(request.justification, '采购申请')),
    }))
  })
}

function adaptDraftResponse(value: unknown): PurchaseDraft {
  const root = value && typeof value === 'object' ? value as ApiRecord : {}
  const payload = root.payload && typeof root.payload === 'object' ? root.payload as ApiRecord : {}
  const first = records(payload.items)[0] ?? {}
  return {
    id: text(root.draft_id, text(root.id)),
    draftId: text(root.draft_id, text(root.id)),
    requestId: text(root.request_id),
    requestNo: '待确认草稿',
    material: text(first.material_name, text(first.material_id, '未命名物料')),
    quantity: text(first.qty, '0'),
    unit: displayUnit(text(first.unit, 'piece')),
    unitCode: text(first.unit, 'piece'),
    neededBy: text(payload.needed_by, '待确认'),
    warehouse: text(first.warehouse_name, text(first.warehouse_code, text(first.warehouse_id, '项目仓库'))),
    status: '待确认',
    createdAt: '刚刚',
    risk: '中',
    calcId: text(payload.calculation_id, 'shortage-v1'),
    reason: text(first.reason, '由确定性缺料计算生成'),
    confirmationToken: text(root.confirmation_token) || undefined,
    payloadHash: text(root.payload_hash) || undefined,
  }
}

function adaptDraftStatus(value: unknown): PurchaseDraft['status'] {
  switch (text(value)) {
    case 'PENDING_CONFIRMATION': return '待确认'
    case 'REJECTED': return '已驳回'
    case 'EXPIRED': return '已过期'
    case 'FAILED': return '已取消'
    case 'CONFIRMED':
    case 'EXECUTED': return '已提交'
    default: return '已提交'
  }
}

function adaptBatches(value: unknown): BatchOption[] {
  return records(value).map((batch) => ({
    id: text(batch.id),
    batchNo: text(batch.batch_no, text(batch.id)),
    material: text(batch.material_name, text(batch.sku, '未命名物料')),
    unit: displayUnit(text(batch.unit, 'piece')),
    available: text(batch.available, '0'),
    status: text(batch.status, 'EXPECTED'),
  }))
}

function adaptTodos(value: unknown): Todo[] {
  return records(value).map((todo) => ({
    id: text(todo.id),
    title: text(todo.title, '未命名待办'),
    owner: ({ PROJECT_OWNER: '项目负责人', SITE_ADMIN: '现场管理员', INSPECTOR: '验收监理', PROCUREMENT: '采购人员', SUPPLIER: '供应商' } as Record<string, string>)[text(todo.owner_role)] ?? text(todo.owner_role),
    due: displayDate(todo.due_at, '待安排'),
    priority: text(todo.priority) === 'HIGH' ? '高' : text(todo.priority) === 'LOW' ? '低' : '中',
    status: text(todo.status) === 'DONE' || text(todo.status) === 'CLOSED' ? '已完成' : text(todo.status) === 'IN_PROGRESS' ? '进行中' : '待处理',
  }))
}

function adaptBusinessRecord(value: unknown): BusinessRecord {
  const root = value && typeof value === 'object' ? value as ApiRecord : {}
  return {
    event: root.event && typeof root.event === 'object' ? root.event as ApiRecord : {},
    recordType: text(root.record_type),
    record: root.record && typeof root.record === 'object' ? root.record as ApiRecord : {},
    items: records(root.items),
    auditLogs: records(root.audit_logs),
    sourceRefs: Array.isArray(root.source_refs) ? root.source_refs.map(String) : [],
  }
}

function adaptStockMoves(value: unknown): StockMoveView[] {
  return records(value).map((move) => ({
    id: text(move.id),
    moveNo: text(move.move_no, text(move.id)),
    moveType: text(move.move_type),
    materialName: text(move.material_name, text(move.sku, '未命名物料')),
    materialId: text(move.material_id),
    qty: text(move.qty, '0'),
    unit: displayUnit(text(move.unit, 'piece')),
    occurredAt: displayDate(move.occurred_at, '—'),
    reason: text(move.reason, '—'),
    fromWarehouse: text(move.from_warehouse_code) || undefined,
    toWarehouse: text(move.to_warehouse_code) || undefined,
  }))
}

function adaptConsumption(value: unknown): ConsumptionView[] {
  return records(value).map((row) => ({
    date: text(row.date),
    materialName: text(row.material_name, text(row.sku, '未命名物料')),
    materialId: text(row.material_id),
    qty: text(row.net_consumed_qty, '0'),
    unit: displayUnit(text(row.unit, 'piece')),
  }))
}

function adaptPurchaseOrders(value: unknown): PurchaseOrderView[] {
  return records(value).map((order) => ({
    id: text(order.id),
    poNo: text(order.po_no, text(order.id)),
    supplier: text(order.supplier_name, text(order.supplier_code, '未命名供应商')),
    status: text(order.status, 'DRAFT'),
    expectedDate: text(order.expected_date) || undefined,
    items: records(order.items).map((item) => ({
      materialName: text(item.material_name, text(item.sku, '未命名物料')),
      qtyOrdered: text(item.qty_ordered, '0'),
      qtyShipped: text(item.qty_shipped, '0'),
      qtyReceived: text(item.qty_received, '0'),
      unit: displayUnit(text(item.unit, 'piece')),
    })),
  }))
}

function adaptDocuments(value: unknown): DocumentView[] {
  return records(value).map((doc) => ({
    id: text(doc.id), documentType: text(doc.document_type), mime: text(doc.mime),
    sizeBytes: Number(doc.size_bytes ?? 0), sha256: text(doc.sha256), version: Number(doc.version ?? 1), uploadedAt: text(doc.uploaded_at),
  }))
}

function adaptShortageMeta(value: unknown): ShortageMeta {
  const root = value && typeof value === 'object' ? value as ApiRecord : {}
  const curveByMaterial: Record<string, Array<Record<string, unknown>>> = {}
  records(root.items).forEach((item) => {
    curveByMaterial[text(item.material_id)] = records(item.daily_curve)
  })
  return {
    calculationId: text(root.calculation_id), calculationVersion: text(root.calculation_version, 'shortage-v1'),
    inputSnapshotHash: text(root.input_snapshot_hash), confidence: Number(root.confidence ?? 0),
    missingData: Array.isArray(root.missing_data) ? root.missing_data.map(String) : [],
    warnings: Array.isArray(root.warnings) ? root.warnings.map(String).filter(Boolean) : [], curveByMaterial,
  }
}

export interface DailyReportView {
  id: string
  reportDate: string
  version: number
  publishedAt?: string
  payload: ApiRecord
}

function adaptDailyReports(value: unknown): DailyReportView[] {
  return records(value).map((report) => ({
    id: text(report.id),
    reportDate: text(report.report_date),
    version: Number(report.version ?? 1),
    publishedAt: text(report.published_at) || undefined,
    payload: report.payload && typeof report.payload === 'object' ? report.payload as ApiRecord : {},
  }))
}

function adaptAi(value: unknown, fallbackText: string): AiMessage {
  const root = value && typeof value === 'object' ? value as ApiRecord : {}
  const claimText = (claims: unknown) => records(claims).map((claim) => text(claim.text)).filter(Boolean)
  return {
    id: text(root.run_id, `ai-${Date.now()}`), role: 'assistant', timestamp: '刚刚',
    text: text(root.text, fallbackText), facts: claimText(root.facts), calculations: claimText(root.calculations),
    hypotheses: claimText(root.hypotheses), warnings: Array.isArray(root.warnings) ? root.warnings.map(String) : [],
    sourceRefs: Array.isArray(root.source_refs) ? root.source_refs.map(String) : [],
    actions: Array.isArray(root.next_actions) ? root.next_actions.map(String) : [],
  }
}

export interface OverviewData {
  stock: StockItem[]
  shortage: ShortageItem[]
  drafts: PurchaseDraft[]
  traces: TraceEvent[]
  anomalies: Anomaly[]
  todos: Todo[]
  batches: BatchOption[]
  stockMoves: StockMoveView[]
  purchaseOrders: PurchaseOrderView[]
  documents: DocumentView[]
  shortageMeta: ShortageMeta
  consumption: ConsumptionView[]
}

export interface ShortageMeta {
  calculationId: string
  calculationVersion: string
  inputSnapshotHash: string
  confidence: number
  missingData: string[]
  warnings: string[]
  curveByMaterial: Record<string, Array<Record<string, unknown>>>
}

export interface StockMoveView {
  id: string
  moveNo: string
  moveType: string
  materialName: string
  materialId: string
  qty: string
  unit: string
  occurredAt: string
  reason: string
  fromWarehouse?: string
  toWarehouse?: string
}

export interface ConsumptionView {
  date: string
  materialName: string
  materialId: string
  qty: string
  unit: string
}

export interface PurchaseOrderView {
  id: string
  poNo: string
  supplier: string
  status: string
  expectedDate?: string
  items: Array<{ materialName: string; qtyOrdered: string; qtyShipped: string; qtyReceived: string; unit: string }>
}

export interface DocumentView {
  id: string
  documentType: string
  mime: string
  sizeBytes: number
  sha256: string
  version: number
  uploadedAt: string
}

export interface BatchOption {
  id: string
  batchNo: string
  material: string
  unit: string
  available: string
  status: string
}

export interface BusinessRecord {
  event: ApiRecord
  recordType: string
  record: ApiRecord
  items: ApiRecord[]
  auditLogs: ApiRecord[]
  sourceRefs: string[]
}

export interface EvidenceVerification {
  matches: boolean
  txExists: boolean
  txId?: string
  checkedAt?: string
}

async function verifyEvidence(eventId: string): Promise<EvidenceVerification> {
  const fallback = (): EvidenceVerification => ({
    matches: true,
    txExists: true,
    txId: eventId.startsWith('trace-') ? `mocktx_${eventId}` : undefined,
  })
  if (isMockMode) return fallback()
  const response = await fetch(`${API_BASE}/projects/P001/evidence/${encodeURIComponent(eventId)}/verify`, {
    headers: requestHeaders(),
  })
  if (!response.ok) throw new ApiError(await readError(response), response.status)
  const body = await response.json() as { data?: Record<string, unknown> }
  const data = body.data ?? {}
  return {
    matches: Boolean(data.matches),
    txExists: Boolean(data.tx_exists),
    txId: typeof data.tx_id === 'string' ? data.tx_id : undefined,
    checkedAt: typeof data.checked_at === 'string' ? data.checked_at : undefined,
  }
}

export const api = {
  health: async () => {
    const response = await fetch('/health', { headers: requestHeaders() })
    if (!response.ok) throw new ApiError(await readError(response), response.status)
    return await response.json() as Record<string, unknown>
  },
  getOverview: async (): Promise<OverviewData> => {
    const [rawStock, rawShortage, rawRequests, rawDrafts, rawBatches, rawAnomalies, rawTodos, rawMoves, rawOrders, rawDocuments, rawConsumption] = await Promise.all([
      getWithFallback('/projects/P001/stock?include_quarantined=true', () => ({ items: stockItems })),
      postWithFallback('/projects/P001/shortage:calculate', { date_from: '2026-08-27', date_to: '2026-09-14' }, () => ({ items: shortageItems })),
      getWithFallback('/projects/P001/purchase-requests', () => ({ items: purchaseDrafts })),
      getWithFallback('/projects/P001/drafts?draft_type=MATERIAL_REQUEST', () => ({ items: [] })),
      getWithFallback('/projects/P001/batches', () => ({ items: [] })),
      getWithFallback('/projects/P001/anomalies', () => anomalies),
      getWithFallback('/projects/P001/todos', () => ({ items: [] })),
      getWithFallback('/projects/P001/stock-moves', () => ({ items: [] })),
      getWithFallback('/projects/P001/purchase-orders', () => ({ items: [] })),
      getWithFallback('/projects/P001/documents', () => ({ items: [] })),
      getWithFallback('/projects/P001/consumption?date_from=2026-08-13&date_to=2026-08-27', () => ({ items: [] })),
    ])
    const batches = adaptBatches(rawBatches)
    const rawTrace = batches[0]
      ? await getWithFallback(`/projects/P001/batches/${encodeURIComponent(batches[0].id)}/trace`, () => ({ events: traceEvents }))
      : { events: [] }
    const mappedStock = adaptStock(rawStock)
    const mappedShortage = adaptShortage(rawShortage)
    const mappedTrace = adaptTrace(rawTrace)
    const mappedAnomalies = adaptAnomalies(rawAnomalies)
    return {
      stock: mappedStock,
      shortage: mappedShortage,
      // Once a material draft has been executed, the authoritative
      // purchase_request replaces it in this projection.  Keeping both cards
      // made the executed draft look actionable even though it is immutable.
      drafts: [...adaptPurchaseRequests(rawRequests), ...records(rawDrafts).filter((draft) => !['EXECUTED', 'CONFIRMED'].includes(text(draft.status))).map((draft) => ({
        id: text(draft.id), draftId: text(draft.id), requestNo: '待确认草稿', payloadHash: text(draft.payload_hash),
        requestId: text(draft.purchase_request_id) || undefined,
        material: text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.material_name, text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.material_id, '未命名物料')),
        quantity: text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.qty, '0'),
        unit: displayUnit(text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.unit, 'piece')), unitCode: text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.unit, 'piece'),
        neededBy: text((draft.payload as ApiRecord | undefined)?.needed_by, '待确认'), warehouse: text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.warehouse_name, text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.warehouse_code, text(records((draft.payload as ApiRecord | undefined)?.items)[0]?.warehouse_id, '项目仓库'))),
        status: adaptDraftStatus(draft.status), rawStatus: text(draft.status), createdAt: text(draft.expires_at, '已创建'), risk: '中', calcId: text((draft.payload as ApiRecord | undefined)?.calculation_id, 'shortage-v1'), reason: '待确认草稿',
      } as PurchaseDraft))],
      traces: mappedTrace,
      anomalies: mappedAnomalies,
      todos: adaptTodos(rawTodos),
      batches,
      stockMoves: adaptStockMoves(rawMoves),
      purchaseOrders: adaptPurchaseOrders(rawOrders),
      documents: adaptDocuments(rawDocuments),
      shortageMeta: adaptShortageMeta(rawShortage),
      consumption: adaptConsumption(rawConsumption),
    }
  },
  getStock: async (query = '') => {
    const result = await getWithFallback(`/projects/P001/stock${query ? `?material_id=${encodeURIComponent(query)}` : ''}`, () => ({ items: stockItems }))
    return adaptStock(result)
  },
  calculateShortageDetailed: async () => {
    const result = await postWithFallback('/projects/P001/shortage:calculate', { date_from: '2026-08-27', date_to: '2026-09-14' }, () => ({ items: shortageItems }))
    return { items: adaptShortage(result), meta: adaptShortageMeta(result) }
  },
  calculateShortage: async () => (await api.calculateShortageDetailed()).items,
  getBatches: async () => adaptBatches(await getWithFallback('/projects/P001/batches', () => ({ items: [] }))),
  getTodos: async () => adaptTodos(await getWithFallback('/projects/P001/todos', () => ({ items: [] }))),
  updateTodo: async (todoId: string, patch: Record<string, unknown>) => adaptTodos([await patchRequest<ApiRecord>(`/projects/P001/todos/${encodeURIComponent(todoId)}`, patch)])[0],
  getStockMoves: async () => adaptStockMoves(await getWithFallback('/projects/P001/stock-moves', () => ({ items: [] }))),
  getConsumption: async () => adaptConsumption(await getWithFallback('/projects/P001/consumption?date_from=2026-08-13&date_to=2026-08-27', () => ({ items: [] }))),
  getPurchaseOrders: async () => adaptPurchaseOrders(await getWithFallback('/projects/P001/purchase-orders', () => ({ items: [] }))),
  getPurchaseOrderDetail: async (orderId: string) => getWithFallback(`/projects/P001/purchase-orders/${encodeURIComponent(orderId)}`, () => ({})),
  getDrafts: async (draftType = 'MATERIAL_REQUEST') => records(await getWithFallback(`/projects/P001/drafts?draft_type=${encodeURIComponent(draftType)}`, () => ({ items: [] }))),
  createPurchaseDraft: async (shortageResult: unknown, selectedItems: unknown[], neededBy: string) => adaptDraftResponse(await postRequest<ApiRecord>('/projects/P001/purchase-requests:draft', { shortage_result: shortageResult, selected_items: selectedItems, needed_by: neededBy })),
  submitPurchaseDraft: async (draftId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/purchase-requests/${encodeURIComponent(draftId)}:confirm`, { confirmation_token: token }),
  getDraftConfirmationToken: async (draftId: string) => postRequest<ApiRecord>(`/projects/P001/drafts/${encodeURIComponent(draftId)}:confirmation-token`, {}),
  rejectDraft: async (draftId: string, reason: string) => postRequest<ApiRecord>(`/projects/P001/drafts/${encodeURIComponent(draftId)}:reject`, { reason }),
  getApprovalToken: async (requestId: string) => postRequest<ApiRecord>(`/projects/P001/purchase-requests/${encodeURIComponent(requestId)}:approval-token`, {}),
  approvePurchaseRequest: async (requestId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/purchase-requests/${encodeURIComponent(requestId)}:approve`, { confirmation_token: token }),
  rejectPurchaseRequest: async (requestId: string, reason: string) => postRequest<ApiRecord>(`/projects/P001/purchase-requests/${encodeURIComponent(requestId)}:reject`, { reason }),
  createStockMove: async (payload: Record<string, unknown>) => postRequest<ApiRecord>('/projects/P001/stock-moves', payload),
  createShipment: async (payload: Record<string, unknown>) => postRequest<ApiRecord>('/projects/P001/shipments', payload),
  createDelivery: async (payload: Record<string, unknown>) => postRequest<ApiRecord>('/projects/P001/deliveries', payload),
  confirmDelivery: async (deliveryId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/deliveries/${encodeURIComponent(deliveryId)}:confirm`, { confirmation_token: token }),
  createInspection: async (payload: Record<string, unknown>) => postRequest<ApiRecord>('/projects/P001/inspections', payload),
  getInspectionConfirmationToken: async (inspectionId: string) => postRequest<ApiRecord>(`/projects/P001/inspections/${encodeURIComponent(inspectionId)}:confirmation-token`, {}),
  confirmInspection: async (inspectionId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/inspections/${encodeURIComponent(inspectionId)}:confirm`, { confirmation_token: token }),
  getHandovers: async () => records(await getWithFallback('/projects/P001/handovers', () => ({ items: [] }))),
  getHandoverConfirmationToken: async (handoverId: string) => postRequest<ApiRecord>(`/projects/P001/handovers/${encodeURIComponent(handoverId)}:confirmation-token`, {}),
  confirmHandover: async (handoverId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/handovers/${encodeURIComponent(handoverId)}:confirm`, { confirmation_token: token }),
  getDailyReports: async (from: string, to: string) => adaptDailyReports(await getWithFallback(`/projects/P001/daily-reports?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, () => ({ items: [] }))),
  createDailyReportDraft: async (reportDate: string) => postRequest<ApiRecord>('/projects/P001/daily-reports:draft', { report_date: reportDate }),
  publishDailyReport: async (draftId: string, token: string) => postRequest<ApiRecord>(`/projects/P001/daily-reports/${encodeURIComponent(draftId)}:publish`, { confirmation_token: token }),
  scanAnomalies: async (from: string, to: string) => postRequest<ApiRecord>('/projects/P001/anomalies:scan', { date_from: from, date_to: to }),
  acknowledgeAnomaly: async (anomalyId: string, note: string) => postRequest<ApiRecord>(`/projects/P001/anomalies/${encodeURIComponent(anomalyId)}:ack`, { note }),
  resolveAnomaly: async (anomalyId: string, resolutionCode: string, note: string) => postRequest<ApiRecord>(`/projects/P001/anomalies/${encodeURIComponent(anomalyId)}:resolve`, { resolution_code: resolutionCode, note }),
  getAiRun: async (runId: string) => getWithFallback(`/projects/P001/ai/runs/${encodeURIComponent(runId)}`, () => ({})),
  getAiThreadMessages: async (threadId: string) => records(await getWithFallback(`/projects/P001/ai/threads/${encodeURIComponent(threadId)}/messages`, () => ({ items: initialMessages }))),
  clearAiThread: async (threadId: string) => deleteRequest<ApiRecord>(`/projects/P001/ai/threads/${encodeURIComponent(threadId)}/messages`),
  getTrace: async (batchId: string) => adaptTrace(await getWithFallback(`/projects/P001/batches/${encodeURIComponent(batchId)}/trace`, () => ({ events: traceEvents }))),
  getBusinessRecord: async (eventId: string) => adaptBusinessRecord(await getWithFallback(`/projects/P001/evidence/${encodeURIComponent(eventId)}/business-record`, () => ({ event: {}, record: {}, items: [], audit_logs: [] }))),
  getDocuments: async () => adaptDocuments(await getWithFallback('/projects/P001/documents', () => ({ items: [] }))),
  getDocumentDownloadUrl: async (documentId: string) => getWithFallback(`/projects/P001/documents/${encodeURIComponent(documentId)}/download-url?ttl_seconds=300`, () => ({})),
  downloadDocument: async (documentId: string) => {
    if (isMockMode) throw new ApiError('显式演示数据模式没有真实文件内容')
    const descriptor = await api.getDocumentDownloadUrl(documentId) as { url?: string }
    if (!descriptor.url) throw new ApiError('服务端没有返回文件下载链接')
    const downloadUrl = new URL(descriptor.url, globalThis.location?.origin ?? window.location.origin)
    const response = await fetch(downloadUrl.toString(), { headers: requestHeaders() })
    if (!response.ok) throw new ApiError(await readError(response), response.status)
    return response.blob()
  },
  getAnomalies: async () => adaptAnomalies(await getWithFallback('/projects/P001/anomalies', () => anomalies)),
  confirmDraft: async (_draftId: string) => {
    throw new ApiError('采购草稿确认尚未取得对应的一次性确认 token')
  },
  verifyEvidence,
  retryEvidence: async (eventId: string) => postRequest<ApiRecord>(`/projects/P001/evidence/${encodeURIComponent(eventId)}:retry`, {}),
  anchorEvidence: async (eventId: string) => {
    const token = await postWithFallback<{ confirmation_token: string }>(`/projects/P001/evidence/${encodeURIComponent(eventId)}:anchor-token`, {}, () => ({ confirmation_token: '' }))
    if (!token.confirmation_token) throw new ApiError('模拟模式未提供锚定 token')
    return postWithFallback(`/projects/P001/evidence/${encodeURIComponent(eventId)}:anchor`, { confirmation_token: token.confirmation_token }, () => ({ status: 'SUBMITTED' }))
  },
  sendAgentMessage: async (threadId: string, text: string): Promise<AiMessage> => {
    const response = await postWithFallback<unknown>(`/ai/threads/${threadId}/messages`, { text }, () => null)
    if (!response) throw new ApiError('AI 服务没有返回结果')
    return adaptAi(response, text)
  },
  initialMessages,
}
