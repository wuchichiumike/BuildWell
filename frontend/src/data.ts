export type PageId =
  | 'overview'
  | 'inventory'
  | 'shortage'
  | 'procurement'
  | 'trace'
  | 'assistant'
  | 'daily'
  | 'anomalies'

export type Role = 'PROJECT_OWNER' | 'PROCUREMENT' | 'SITE_ADMIN' | 'SUPPLIER' | 'INSPECTOR' | 'AUDITOR'

export type StockStatus = '正常' | '偏低' | '隔离'

export interface StockItem {
  id: string
  materialId?: string
  sku: string
  material: string
  category: '施工物料' | 'PPE' | '急救物资'
  specification: string
  warehouse: string
  warehouseId?: string
  batch: string
  batchId?: string
  available: string
  onHand: string
  reserved: string
  quarantined: string
  safetyStock?: string
  unit: string
  unitCode?: string
  status: StockStatus
  updatedAt: string
}

export interface ShortageItem {
  id: string
  material: string
  area: string
  risk: '高' | '中' | '低'
  riskDate: string
  available: string
  safetyStock: string
  shortage: string
  orderQty: string
  unit: string
  unitCode?: string
  calcId: string
  source: string
}

export interface PurchaseDraft {
  id: string
  requestNo: string
  material: string
  quantity: string
  unit: string
  unitCode?: string
  neededBy: string
  warehouse: string
  status: '待确认' | '已提交' | '已批准' | '已驳回' | '已取消' | '已过期'
  createdAt: string
  risk: '低' | '中' | '高'
  calcId: string
  reason: string
  confirmationToken?: string
  payloadHash?: string
  draftId?: string
  requestId?: string
  rawStatus?: string
  statusCode?: string
}

export interface TraceEvent {
  id: string
  type: 'SHIPMENT' | 'DELIVERY' | 'INSPECTION' | 'HANDOVER'
  label: string
  status: '已确认' | '已提交' | '待上链' | '重试中' | '链上失败'
  time: string
  actor: string
  reference: string
  txId?: string
  hash: string
  detail: string
  tone: 'blue' | 'green' | 'orange' | 'red'
}

export interface Todo {
  id: string
  title: string
  owner: string
  due: string
  priority: '高' | '中' | '低'
  status: '待处理' | '进行中' | '已完成'
}

export interface Anomaly {
  id: string
  title: string
  material: string
  area: string
  level: '高' | '中' | '低'
  status: '待确认' | '已知悉' | '已解决'
  possibleCause: string
  time: string
  sourceRef?: string
  payload?: Record<string, unknown>
}

export interface AiMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  timestamp: string
  facts?: string[]
  calculations?: string[]
  hypotheses?: string[]
  warnings?: string[]
  sourceRefs?: string[]
  actions?: string[]
}

export const roleLabels: Record<Role, string> = {
  PROJECT_OWNER: '项目负责人',
  PROCUREMENT: '采购人员',
  SITE_ADMIN: '现场管理员',
  SUPPLIER: '供应商',
  INSPECTOR: '验收监理',
  AUDITOR: '审计人员',
}

export const stockItems: StockItem[] = [
  {
    id: 'stock-001',
    sku: 'MAT-BLK-001',
    material: '蒸压加气砌块',
    category: '施工物料',
    specification: '600×240×200mm',
    warehouse: '主仓 · WH-MAIN',
    batch: 'B-202608-019',
    available: '1280',
    onHand: '1600',
    reserved: '280',
    quarantined: '40',
    unit: '块',
    status: '偏低',
    updatedAt: '08-27 09:42',
  },
  {
    id: 'stock-002',
    sku: 'MAT-CEM-002',
    material: 'P.O 42.5 水泥',
    category: '施工物料',
    specification: '50kg/袋',
    warehouse: '主仓 · WH-MAIN',
    batch: 'B-202608-014',
    available: '86.500',
    onHand: '96.500',
    reserved: '10.000',
    quarantined: '0.000',
    unit: 't',
    status: '正常',
    updatedAt: '08-27 08:15',
  },
  {
    id: 'stock-003',
    sku: 'MAT-REB-003',
    material: 'HRB400E 螺纹钢',
    category: '施工物料',
    specification: 'Φ20 · 9m',
    warehouse: '钢筋加工区 · WH-REBAR',
    batch: 'B-202607-009',
    available: '32.800',
    onHand: '36.800',
    reserved: '4.000',
    quarantined: '0.000',
    unit: 't',
    status: '正常',
    updatedAt: '08-26 18:23',
  },
  {
    id: 'stock-004',
    sku: 'PPE-GLV-007',
    material: '防护手套',
    category: 'PPE',
    specification: '耐磨 · L码',
    warehouse: '主仓 · WH-MAIN',
    batch: 'PPE-2026-044',
    available: '46',
    onHand: '64',
    reserved: '18',
    quarantined: '0',
    unit: '双',
    status: '偏低',
    updatedAt: '08-27 07:50',
  },
  {
    id: 'stock-005',
    sku: 'PPE-HEL-005',
    material: '安全帽',
    category: 'PPE',
    specification: 'ABS · 黄色',
    warehouse: '主仓 · WH-MAIN',
    batch: 'PPE-2026-031',
    available: '118',
    onHand: '120',
    reserved: '2',
    quarantined: '0',
    unit: '顶',
    status: '正常',
    updatedAt: '08-26 16:04',
  },
  {
    id: 'stock-006',
    sku: 'MAT-ADH-021',
    material: '砌筑专用砂浆',
    category: '施工物料',
    specification: 'M7.5 · 25kg/袋',
    warehouse: '3号楼临时仓 · WH-B3',
    batch: 'B-202608-022',
    available: '12.000',
    onHand: '20.000',
    reserved: '0.000',
    quarantined: '8.000',
    unit: 't',
    status: '隔离',
    updatedAt: '08-26 14:16',
  },
]

export const shortageItems: ShortageItem[] = [
  {
    id: 'short-001',
    material: '蒸压加气砌块',
    area: '3号楼 · 砌体施工',
    risk: '高',
    riskDate: '2026-09-01',
    available: '1,280',
    safetyStock: '1,500',
    shortage: '2,460',
    orderQty: '2,500',
    unit: '块',
    calcId: 'calc-20260827-01',
    source: '计划 v4 · 14 日消耗快照',
  },
  {
    id: 'short-002',
    material: '防护手套',
    area: '全场 · 木工班组',
    risk: '中',
    riskDate: '2026-09-04',
    available: '46',
    safetyStock: '60',
    shortage: '54',
    orderQty: '100',
    unit: '双',
    calcId: 'calc-20260827-02',
    source: 'PPE 标准 · 14 日覆盖检查',
  },
  {
    id: 'short-003',
    material: '砌筑专用砂浆',
    area: '3号楼 · 砌体施工',
    risk: '低',
    riskDate: '2026-09-14',
    available: '12.000',
    safetyStock: '10.000',
    shortage: '4.000',
    orderQty: '5.000',
    unit: 't',
    calcId: 'calc-20260827-03',
    source: '计划 v4 · 在途已计入',
  },
]

export const purchaseDrafts: PurchaseDraft[] = [
  {
    id: 'draft-001',
    requestNo: 'MR-20260827-0007',
    material: '蒸压加气砌块',
    quantity: '2,500',
    unit: '块',
    neededBy: '2026-09-01',
    warehouse: '3号楼临时仓',
    status: '待确认',
    createdAt: '今天 09:48',
    risk: '高',
    calcId: 'calc-20260827-01',
    reason: '恢复至安全库存，覆盖 3 号楼砌体计划',
  },
  {
    id: 'draft-002',
    requestNo: 'MR-20260826-0005',
    material: '防护手套',
    quantity: '100',
    unit: '双',
    neededBy: '2026-09-04',
    warehouse: '主仓',
    status: '已提交',
    createdAt: '昨天 16:22',
    risk: '中',
    calcId: 'calc-20260827-02',
    reason: '木工班组未来 14 天 PPE 覆盖不足',
  },
  {
    id: 'draft-003',
    requestNo: 'MR-20260825-0003',
    material: '砌筑专用砂浆',
    quantity: '5.000',
    unit: 't',
    neededBy: '2026-09-14',
    warehouse: '3号楼临时仓',
    status: '已过期',
    createdAt: '08-25 14:07',
    risk: '低',
    calcId: 'calc-20260825-03',
    reason: '现场计划调整，草稿未在有效期内确认',
  },
]

export const traceEvents: TraceEvent[] = [
  {
    id: 'trace-001',
    type: 'SHIPMENT',
    label: '供应商发货',
    status: '已确认',
    time: '2026-08-26 10:16',
    actor: '供应商 · 海州建材',
    reference: 'SH-20260826-003 · 砌块 1,600 块',
    txId: 'mocktx_trace-001',
    hash: 'a84ce1…c901',
    detail: '发货单及质检报告已登记，批次 B-202608-019。',
    tone: 'blue',
  },
  {
    id: 'trace-002',
    type: 'DELIVERY',
    label: '现场到货',
    status: '已确认',
    time: '2026-08-26 15:42',
    actor: '现场管理员 · 李工',
    reference: 'DL-20260826-002 · 到货 1,600 块',
    txId: 'mocktx_trace-002',
    hash: 'd0ae12…8f30',
    detail: '到货数量与发货数量一致，外观抽检通过。',
    tone: 'green',
  },
  {
    id: 'trace-003',
    type: 'INSPECTION',
    label: '质量验收',
    status: '已确认',
    time: '2026-08-27 08:26',
    actor: '验收监理 · 王工',
    reference: 'IN-20260827-001 · 通过 1,560 块',
    txId: 'mocktx_trace-003',
    hash: 'f53b77…20ad',
    detail: '40 块包装破损，已隔离；通过数量生成入库流水。',
    tone: 'orange',
  },
  {
    id: 'trace-004',
    type: 'HANDOVER',
    label: '班组领用交接',
    status: '待上链',
    time: '2026-08-27 09:12',
    actor: '3号楼砌体班组',
    reference: 'HO-20260827-001 · 领用 320 块',
    hash: '待生成',
    detail: '来源仓库已确认，等待目标班组确认后提交模拟链。',
    tone: 'orange',
  },
]

export const todos: Todo[] = [
  { id: 'todo-001', title: '确认砌块采购草稿并提交审批', owner: '项目负责人', due: '今天 17:00', priority: '高', status: '待处理' },
  { id: 'todo-002', title: '复核砂浆批次隔离原因', owner: '现场管理员', due: '明天 10:00', priority: '中', status: '进行中' },
  { id: 'todo-003', title: '补录 8 月 26 日木工班组领用单', owner: '现场管理员', due: '已逾期 1 天', priority: '高', status: '待处理' },
  { id: 'todo-004', title: '上传砌块验收单原件', owner: '验收监理', due: '已完成', priority: '低', status: '已完成' },
]

export const anomalies: Anomaly[] = [
  { id: 'anomaly-001', title: '木工班组领用量较计划高 32%', material: '防护手套', area: '3号楼 · 木工班组', level: '高', status: '待确认', possibleCause: '计划版本变更后消耗快照未同步', time: '今天 08:42' },
  { id: 'anomaly-002', title: '同物料 24 小时内重复领用 4 次', material: '蒸压加气砌块', area: '3号楼 · 砌体班组', level: '中', status: '已知悉', possibleCause: '分批领用或现场记录延迟，建议核对领料单', time: '昨天 17:16' },
  { id: 'anomaly-003', title: '计划版本 v3 已超过同步窗口', material: '砌筑专用砂浆', area: '3号楼 · 砌体施工', level: '低', status: '已解决', possibleCause: '计划已更新至 v4', time: '08-25 11:05' },
]

export const initialMessages: AiMessage[] = [
  {
    id: 'ai-001',
    role: 'assistant',
    text: '早上好，我是筑福链现场助手。可以帮你查库存、算缺料、查看追溯，或把计算结果整理成采购草稿。当前数据来自受控业务工具。',
    timestamp: '09:30',
    facts: ['当前项目：P001 · 海之子示范项目', '可用库存与批次数据已同步至 08-27 09:42'],
    sourceRefs: ['stock snapshot · 09:42'],
    actions: ['查询 3 号楼本周缺料', '查看待审批采购申请', '追溯砌块批次'],
  },
]

export const chartData = [68, 56, 72, 63, 81, 74, 89, 76, 92, 85, 96, 88, 78, 84]
