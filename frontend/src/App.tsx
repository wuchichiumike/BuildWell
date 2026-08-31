import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties, type RefObject } from 'react'
import { api, demoUsers, isMockMode, setDemoUser, type BatchOption, type BusinessRecord, type ConsumptionView, type DailyReportView, type DocumentView, type PurchaseOrderView, type ShortageMeta, type StockMoveView } from './api'
import {
  anomalies as seedAnomalies,
  initialMessages,
  purchaseDrafts as seedDrafts,
  roleLabels,
  shortageItems as seedShortage,
  stockItems as seedStock,
  todos,
  traceEvents as seedTraces,
  type AiMessage,
  type Anomaly,
  type PageId,
  type PurchaseDraft,
  type Role,
  type ShortageItem,
  type StockItem,
  type Todo,
  type TraceEvent,
} from './data'
import { formatDateTime as formatLocalizedDateTime, formatNumber as formatLocalizedNumber, getActiveLocale, localeOptions, localizeText, setActiveLocale, subscribeLocale, type AppLocale } from './i18n'

const pageTitles: Record<PageId, { title: string; eyebrow: string }> = {
  overview: { title: '项目总览', eyebrow: '现场供应链' },
  inventory: { title: '库存台账', eyebrow: '库存与流水' },
  shortage: { title: '缺料预测', eyebrow: '计算与预警' },
  procurement: { title: '采购协同', eyebrow: '申请与交付' },
  trace: { title: '证据追溯', eyebrow: '批次与链上事件' },
  assistant: { title: 'AI 现场助手', eyebrow: '受控工具问答' },
  daily: { title: '现场日报', eyebrow: '记录与待办' },
  anomalies: { title: '异常中心', eyebrow: '偏差与复核' },
}

const navGroups: { label: string; items: { id: PageId; label: string; icon: string }[] }[] = [
  {
    label: '工作台',
    items: [
      { id: 'overview', label: '项目总览', icon: 'grid' },
      { id: 'inventory', label: '库存台账', icon: 'stack' },
      { id: 'shortage', label: '缺料预测', icon: 'trend' },
    ],
  },
  {
    label: '业务流转',
    items: [
      { id: 'procurement', label: '采购协同', icon: 'cart' },
      { id: 'trace', label: '证据追溯', icon: 'link' },
      { id: 'daily', label: '现场日报', icon: 'calendar' },
      { id: 'anomalies', label: '异常中心', icon: 'alert' },
    ],
  },
]

type LocalizedTextRecord = { source: string; rendered: string }
const localizedTextRecords = new WeakMap<Text, LocalizedTextRecord>()
const localizedAttributeRecords = new WeakMap<Element, Map<string, LocalizedTextRecord>>()

function projectLegacyCopy(root: HTMLElement, locale: AppLocale) {
  const localizeNode = (node: Text) => {
    const parent = node.parentElement
    if (!parent || parent.closest('code, pre, textarea, input, [data-i18n-skip]')) return
    const previous = localizedTextRecords.get(node)
    const source = previous && node.data === previous.rendered ? previous.source : node.data
    const rendered = localizeText(source, locale)
    localizedTextRecords.set(node, { source, rendered })
    if (node.data !== rendered) node.data = rendered
  }
  const localizeAttribute = (element: Element, attribute: string) => {
    const value = element.getAttribute(attribute)
    if (!value) return
    const entries = localizedAttributeRecords.get(element) ?? new Map<string, LocalizedTextRecord>()
    const previous = entries.get(attribute)
    const source = previous && value === previous.rendered ? previous.source : value
    const rendered = localizeText(source, locale)
    entries.set(attribute, { source, rendered })
    localizedAttributeRecords.set(element, entries)
    if (value !== rendered) element.setAttribute(attribute, rendered)
  }
  const apply = (node: Node = root) => {
    if (node.nodeType === Node.TEXT_NODE) localizeNode(node as Text)
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT)
    let current = walker.nextNode()
    while (current) {
      localizeNode(current as Text)
      current = walker.nextNode()
    }
    const elements = node instanceof Element
      ? [node, ...Array.from(node.querySelectorAll('[title], [placeholder], [aria-label]'))]
      : Array.from(root.querySelectorAll('[title], [placeholder], [aria-label]'))
    elements.forEach((element) => ['title', 'placeholder', 'aria-label'].forEach((attribute) => localizeAttribute(element, attribute)))
  }
  apply()
  return apply
}

function useLegacyCopyProjection(root: RefObject<HTMLElement | null>, locale: AppLocale) {
  useEffect(() => {
    const element = root.current
    if (!element) return
    const apply = projectLegacyCopy(element, locale)
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        if (record.type === 'characterData' || record.type === 'attributes') apply(record.target)
        else record.addedNodes.forEach((node) => apply(node))
      })
    })
    observer.observe(element, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['title', 'placeholder', 'aria-label'] })
    return () => observer.disconnect()
  }, [root, locale])
}

function Icon({ name }: { name: string }) {
  const glyphs: Record<string, string> = {
    grid: '▦',
    stack: '▤',
    trend: '↗',
    cart: '□',
    link: '⌘',
    calendar: '▣',
    alert: '!',
    bot: '✦',
    search: '⌕',
    plus: '+',
    arrow: '→',
    download: '↓',
    filter: '≡',
    refresh: '↻',
    check: '✓',
    clock: '◷',
    external: '↗',
    menu: '☰',
    close: '×',
    send: '↑',
    lock: '⌑',
    chevron: '›',
    document: '▧',
  }
  return <span className={`icon icon-${name}`} aria-hidden="true">{glyphs[name] ?? '·'}</span>
}

function formatNumber(value: string) {
  return formatLocalizedNumber(value, getActiveLocale(), { maximumFractionDigits: 3 })
}

function textValue(value: unknown, fallback = '') {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

function riskClass(risk: string) {
  return risk === '高' ? 'risk-high' : risk === '中' ? 'risk-medium' : 'risk-low'
}

function statusClass(status: string) {
  if (status.includes('确认') || status.includes('正常') || status.includes('完成') || status.includes('通过') || status.includes('已知悉')) return 'status-success'
  if (status.includes('隔离') || status.includes('失败') || status.includes('拒绝') || status.includes('逾期')) return 'status-danger'
  if (status.includes('待') || status.includes('进行') || status.includes('重试') || status.includes('偏低') || status.includes('提交')) return 'status-warning'
  return 'status-muted'
}

function StatusPill({ children, tone }: { children: ReactNode; tone?: string }) {
  const resolvedTone = tone ?? (typeof children === 'string' ? statusClass(children) : 'status-muted')
  return <span className={`status-pill ${resolvedTone}`}><span className="status-dot" />{children}</span>
}

function PageHeading({
  title,
  description,
  action,
  actionIcon = 'plus',
  onAction,
}: {
  title: string
  description: string
  action?: string
  actionIcon?: string
  onAction?: () => void
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="section-kicker">{title === '项目总览' ? `TODAY · ${formatLocalizedDateTime(new Date(), getActiveLocale(), { month: '2-digit', day: '2-digit', weekday: 'short' })}` : 'P001 · 海之子示范项目'}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      {action && <button className="button button-primary" onClick={onAction}><Icon name={actionIcon} />{action}</button>}
    </div>
  )
}

function MetricCard({ label, value, unit, trend, tone, onClick }: { label: string; value: string; unit?: string; trend?: string; tone: string; onClick?: () => void }) {
  return (
    <button className={`metric-card metric-${tone}`} onClick={onClick}>
      <div className="metric-label"><span className="metric-mark" />{label}<Icon name="chevron" /></div>
      <div className="metric-value">{value}<small>{unit}</small></div>
      {trend && <div className="metric-trend">{trend}</div>}
    </button>
  )
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state"><span className="empty-mark">—</span><span>{text}</span></div>
}

function formatDateTime(value: string) {
  return formatLocalizedDateTime(value, getActiveLocale(), {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

function projectToday() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const values = Object.fromEntries(parts.filter((part) => part.type !== 'literal').map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}

function projectDateDaysAgo(days: number) {
  const current = new Date(`${projectToday()}T00:00:00+08:00`)
  current.setDate(current.getDate() - days)
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(current)
  const values = Object.fromEntries(parts.filter((part) => part.type !== 'literal').map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}

function triggerJsonDownload(filename: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function triggerCsvDownload(filename: string, rows: Array<Record<string, string>>) {
  const headers = rows[0] ? Object.keys(rows[0]) : []
  const quote = (value: string) => `"${value.replace(/"/g, '""')}"`
  const csv = [headers.map(quote).join(','), ...rows.map((row) => headers.map((header) => quote(row[header] ?? '')).join(','))].join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function OverviewPage({
  stock,
  shortage,
  drafts,
  traces,
  todoList,
  anomalyList,
  onNavigate,
  onStockSelect,
  onTraceSelect,
  onToast,
  onCreateDailyReport,
  onRefreshShortage,
  stockMoves,
  consumption,
  onTodoUpdate,
}: {
  stock: StockItem[]
  shortage: ShortageItem[]
  drafts: PurchaseDraft[]
  traces: TraceEvent[]
  todoList: Todo[]
  anomalyList: Anomaly[]
  onNavigate: (page: PageId) => void
  onStockSelect: (materialId: string) => void
  onTraceSelect: (eventId: string) => void
  onToast: (message: string) => void
  onCreateDailyReport: () => void
  onRefreshShortage: () => void
  stockMoves: StockMoveView[]
  consumption: ConsumptionView[]
  onTodoUpdate: (todo: Todo) => void
}) {
  const openAnomalies = anomalyList.filter((item) => item.status !== '已解决').length
  const lowStock = stock.filter((item) => item.status === '偏低').length
  const completedMoves = stockMoves.filter((move) => move.moveType === 'ISSUE').length
  const chartMaterialId = consumption.find((row) => Number(row.qty) > 0)?.materialId
  const chartValues = consumption.filter((row) => !chartMaterialId || row.materialId === chartMaterialId).slice(-14).map((move) => Number(move.qty)).filter(Number.isFinite)
  const maxChartValue = Math.max(1, ...chartValues)
  return (
    <>
      <PageHeading title="项目总览" description="掌握物料流转、缺料风险和今日现场动作" action="生成今日日报" actionIcon="document" onAction={onCreateDailyReport} />

      <section className="metrics-grid" aria-label="项目关键指标">
        <MetricCard label="可用库存" value={String(stock.length)} unit="条余额" trend="按仓库、物料和批次分组" tone="teal" onClick={() => onNavigate('inventory')} />
        <MetricCard label="缺料风险" value={String(shortage.length)} unit="项" trend={`${lowStock} 项当前低于安全库存`} tone="orange" onClick={() => onNavigate('shortage')} />
        <MetricCard label="采购申请" value={String(drafts.length).padStart(2, '0')} unit="份" trend="来自数据库采购申请记录" tone="blue" onClick={() => onNavigate('procurement')} />
        <MetricCard label="开放异常" value={String(openAnomalies).padStart(2, '0')} unit="条" trend="由异常扫描与人工复核维护" tone="red" onClick={() => onNavigate('anomalies')} />
      </section>

      <div className="content-grid overview-grid">
        <section className="panel stock-panel">
          <div className="panel-header"><div><p className="panel-kicker">INVENTORY SNAPSHOT</p><h2>库存健康度</h2></div><button className="text-button" onClick={() => onNavigate('inventory')}>查看台账 <Icon name="arrow" /></button></div>
          <div className="inventory-health">
            <div className="health-ring"><div><strong>{Math.max(0, 100 - lowStock * 25)}</strong><span>库存健康指数</span></div></div>
            <div className="health-copy"><strong>{lowStock ? '存在低库存风险' : '库存处于安全范围'}</strong><p>{lowStock ? `当前有 ${lowStock} 项库存低于安全库存；缺口以确定性计算结果为准。` : '当前库存余额没有低于安全库存的物料。'}</p><button className="link-button" onClick={() => onNavigate('shortage')}>查看风险明细 <Icon name="arrow" /></button></div>
          </div>
          <div className="stock-list">
            {stock.slice(0, 4).map((item) => (
              <div className="stock-row stock-row-link" key={item.id} role="button" tabIndex={0} title="打开库存台账" onClick={() => onStockSelect(item.materialId ?? item.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onStockSelect(item.materialId ?? item.id) } }}>
                <div className="stock-name"><span className={`material-avatar ${item.category === 'PPE' ? 'avatar-safety' : 'avatar-material'}`}>{item.category === 'PPE' ? 'P' : 'M'}</span><div><strong>{item.material}</strong><small>{item.warehouse}</small></div></div>
                <div className="stock-qty"><strong>{formatNumber(item.available)}</strong><small>{item.unit} 可用</small></div>
                <StatusPill>{item.status}</StatusPill>
              </div>
            ))}
            {stock.length === 0 && <EmptyState text="暂无库存余额" />}
          </div>
        </section>

        <section className="panel shortage-panel">
          <div className="panel-header"><div><p className="panel-kicker">RISK WATCH</p><h2>缺料风险</h2></div><button className="icon-button" title="刷新缺料计算" onClick={onRefreshShortage}><Icon name="refresh" /></button></div>
          <div className="risk-summary"><div className="risk-summary-number">{shortage.length}<small>项风险</small></div><div className="risk-summary-copy"><span><i className="dot dot-red" />高风险 {shortage.filter((item) => item.risk === '高').length}</span><span><i className="dot dot-amber" />中风险 {shortage.filter((item) => item.risk === '中').length}</span><span><i className="dot dot-green" />低风险 {shortage.filter((item) => item.risk === '低').length}</span></div></div>
          <div className="risk-list">
            {shortage.map((item) => (
              <button className="risk-row" key={item.id} onClick={() => onNavigate('shortage')}>
                <span className={`risk-badge ${riskClass(item.risk)}`}>{item.risk}</span><div className="risk-info"><strong>{item.material}</strong><small>{item.area} · {item.riskDate} 预计触底</small></div><div className="risk-gap"><strong>-{formatNumber(item.shortage)}</strong><small>{item.unit}</small></div><Icon name="chevron" />
              </button>
            ))}
            {shortage.length === 0 && <EmptyState text="暂无缺料风险" />}
          </div>
        </section>
      </div>

      <div className="content-grid lower-grid">
        <section className="panel chart-panel">
          <div className="panel-header"><div><p className="panel-kicker">CONSUMPTION PACE</p><h2>{consumption.find((row) => row.materialId === chartMaterialId)?.materialName ?? '近 14 日物料消耗'}</h2></div><div className="chart-legend"><span><i className="legend-line legend-teal" />实际消耗</span></div></div>
          <div className="chart-area"><div className="chart-y"><span>{maxChartValue}</span><span>{Math.round(maxChartValue * .75)}</span><span>{Math.round(maxChartValue * .5)}</span><span>{Math.round(maxChartValue * .25)}</span><span>0</span></div><div className="chart-bars">{chartValues.length ? chartValues.map((value, index) => <div className="chart-column" key={index}><div className="chart-bar" style={{ height: `${Math.max(5, value / maxChartValue * 100)}%` }}><span>{index === chartValues.length - 1 ? `${value}` : ''}</span></div><small>{index === 0 ? '最早' : index === chartValues.length - 1 ? '最新' : ''}</small></div>) : <EmptyState text="暂无已登记的消耗流水" />}</div></div>
          <div className="chart-foot"><span><b>{completedMoves || consumption.filter((row) => Number(row.qty) > 0).length}</b> 条有效消耗记录</span><span className="positive-text">来源：consumption_snapshots</span></div>
        </section>

        <section className="panel todo-panel">
          <div className="panel-header"><div><p className="panel-kicker">TODAY'S ACTIONS</p><h2>待办事项</h2></div><button className="text-button" onClick={() => onNavigate('daily')}>全部待办 <Icon name="arrow" /></button></div>
          <div className="todo-list">{todoList.slice(0, 4).map((todo) => <div className="todo-row" key={todo.id}><button className={`todo-check ${todo.status === '已完成' ? 'checked' : ''}`} title="更新待办状态" onClick={() => onTodoUpdate(todo)}><Icon name="check" /></button><div className="todo-copy"><strong>{todo.title}</strong><small>{todo.owner} · {todo.due}</small></div><span className={`priority priority-${todo.priority === '高' ? 'high' : todo.priority === '中' ? 'medium' : 'low'}`}>{todo.priority}</span></div>)}{todoList.length === 0 && <EmptyState text="暂无待办记录" />}</div>
        </section>
      </div>

          <section className="panel trace-strip"><div className="panel-header"><div><p className="panel-kicker">LATEST EVIDENCE</p><h2>最新证据事件</h2></div><button className="text-button" onClick={() => onNavigate('trace')}>打开追溯 <Icon name="arrow" /></button></div><div className="trace-strip-list">{traces.slice(0, 4).map((event) => <button className="trace-mini" key={event.id} onClick={() => onTraceSelect(event.id)}><span className={`trace-mini-icon tone-${event.tone}`}><Icon name={event.type === 'SHIPMENT' ? 'cart' : event.type === 'DELIVERY' ? 'stack' : event.type === 'INSPECTION' ? 'check' : 'link'} /></span><div><strong>{event.label}</strong><small>{event.reference}</small></div><StatusPill>{event.status}</StatusPill><Icon name="chevron" /></button>)}{traces.length === 0 && <EmptyState text="暂无证据事件" />}</div></section>
    </>
  )
}


function InventoryPage({ stock, stockMoves, canWrite, onToast, onNavigate, onRefresh, focusMaterialId }: { stock: StockItem[]; stockMoves: StockMoveView[]; canWrite: boolean; onToast: (message: string) => void; onNavigate: (page: PageId) => void; onRefresh: () => Promise<void>; focusMaterialId?: string | null }) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('全部状态')
  const [selectedId, setSelectedId] = useState(stock[0]?.id ?? '')
  const [moveOpen, setMoveOpen] = useState(false)
  const selected = stock.find((item) => item.id === selectedId)
  const filtered = stock.filter((item) => (item.material.includes(query) || item.sku.toLowerCase().includes(query.toLowerCase())) && (status === '全部状态' || item.status === status))
  useEffect(() => { if (!selected && filtered[0]) setSelectedId(filtered[0].id) }, [filtered, selected])
  useEffect(() => {
    if (!focusMaterialId) return
    const match = stock.find((item) => item.materialId === focusMaterialId || item.id === focusMaterialId)
    if (match) setSelectedId(match.id)
  }, [focusMaterialId, stock])
  return <>
    <PageHeading title="库存台账" description="按仓库、批次和物料查看可用量；所有余额与流水由业务 API 维护" />
    <section className="toolbar"><div className="search-field"><Icon name="search" /><input placeholder="搜索物料名称或 SKU" value={query} onChange={(event) => setQuery(event.target.value)} /></div><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="全部状态">全部状态</option><option value="正常">正常</option><option value="偏低">偏低</option><option value="隔离">隔离</option></select><span className="toolbar-spacer" /><button className="icon-button" title="导出库存台账" onClick={() => { triggerCsvDownload('zhufulian-stock.csv', filtered.map((item) => ({ SKU: item.sku, 物料: item.material, 仓库: item.warehouse, 批次: item.batch, 可用量: `${item.available} ${item.unit}`, 预留: item.reserved, 隔离: item.quarantined, 状态: item.status }))); onToast('已导出当前筛选的库存台账') }}><Icon name="download" /></button></section>
    <div className="inventory-layout"><section className="panel table-panel"><div className="table-meta"><span>共 {filtered.length} 条库存余额</span><span className="muted">source: stock_balances</span></div><div className="table-scroll"><table><thead><tr><th>物料</th><th>仓库 / 批次</th><th>可用量</th><th>预留</th><th>安全库存</th><th>状态</th></tr></thead><tbody>{filtered.map((item) => <tr key={item.id} className={selectedId === item.id ? 'row-active' : ''} onClick={() => setSelectedId(item.id)}><td><div className="table-material"><span className={`material-avatar small ${item.category === 'PPE' ? 'avatar-safety' : 'avatar-material'}`}>{item.category === 'PPE' ? 'P' : 'M'}</span><div><strong>{item.material}</strong><small>{item.sku} · {item.specification}</small></div></div></td><td><strong>{item.warehouse}</strong><small>{item.batch}</small></td><td><strong className={item.status === '偏低' ? 'orange-text' : ''}>{formatNumber(item.available)} <small className="inline-unit">{item.unit}</small></strong><small>现有 {formatNumber(item.onHand)}</small></td><td>{formatNumber(item.reserved)} <small className="inline-unit">{item.unit}</small></td><td><span className="safety-bar"><i style={{ width: `${Math.min(100, Number(item.available) / Math.max(1, Number(item.safetyStock ?? item.onHand)) * 100)}%` }} /></span></td><td><StatusPill>{item.status}</StatusPill></td></tr>)}</tbody></table>{filtered.length === 0 && <EmptyState text="没有符合条件的库存余额" />}</div></section><aside className="panel detail-panel">{selected ? <><div className="detail-header"><div><p className="panel-kicker">MATERIAL DETAIL</p><h2>{selected.material}</h2><small>{selected.sku}</small></div><StatusPill>{selected.status}</StatusPill></div><div className="detail-hero"><div><span>可用量</span><strong>{formatNumber(selected.available)} <small>{selected.unit}</small></strong></div><div className="detail-ring"><span style={{ '--progress': `${Math.min(100, Number(selected.available) / Math.max(1, Number(selected.safetyStock ?? selected.onHand)) * 100)}%` } as CSSProperties}><b>{formatNumber(selected.available)}</b></span></div></div><dl className="detail-list"><div><dt>规格</dt><dd>{selected.specification}</dd></div><div><dt>仓库</dt><dd>{selected.warehouse}</dd></div><div><dt>批次</dt><dd>{selected.batch}</dd></div><div><dt>预留量</dt><dd>{formatNumber(selected.reserved)} {selected.unit}</dd></div><div><dt>隔离量</dt><dd>{formatNumber(selected.quarantined)} {selected.unit}</dd></div><div><dt>关联流水</dt><dd>{stockMoves.filter((move) => move.materialId === selected.materialId).length} 条</dd></div></dl><div className="detail-actions"><button className="button button-primary" disabled={!canWrite} title={canWrite ? '登记库存流水' : '当前角色没有库存写入权限'} onClick={() => setMoveOpen(true)}><Icon name="plus" />登记库存流水</button><button className="button button-quiet" onClick={() => onNavigate('shortage')}>查看缺料预测</button></div><div className="chain-note"><Icon name="lock" /><span>库存可用量 = 现有量 − 预留 − 隔离。<br />业务写入会记录审计和幂等键。</span></div></> : <EmptyState text="选择一项物料查看详情" />}</aside></div>
    <div className="inventory-ledger panel"><div className="panel-header"><div><p className="panel-kicker">IMMUTABLE LEDGER</p><h2>最近库存流水</h2></div><span className="muted">{stockMoves.length} 条记录</span></div>{stockMoves.length ? <div className="table-scroll"><table><thead><tr><th>流水号</th><th>物料</th><th>类型</th><th>数量</th><th>时间</th><th>原因</th></tr></thead><tbody>{stockMoves.slice(0, 20).map((move) => <tr key={move.id}><td className="mono">{move.moveNo}</td><td>{move.materialName}</td><td><StatusPill>{move.moveType}</StatusPill></td><td>{move.qty} {move.unit}</td><td>{move.occurredAt}</td><td>{move.reason}</td></tr>)}</tbody></table></div> : <EmptyState text="暂无库存流水；登记领用、退库或报废后会在此显示。" />}</div>
    {moveOpen && selected && <StockMoveModal item={selected} onClose={() => setMoveOpen(false)} onSaved={async () => { setMoveOpen(false); await onRefresh(); onToast('库存流水已写入数据库') }} onToast={onToast} />}
  </>
}

function StockMoveModal({ item, onClose, onSaved, onToast }: { item: StockItem; onClose: () => void; onSaved: () => Promise<void>; onToast: (message: string) => void }) {
  const [moveType, setMoveType] = useState<'ISSUE' | 'RETURN' | 'SCRAP'>('ISSUE')
  const [qty, setQty] = useState('')
  const [reason, setReason] = useState('')
  const [referenceId, setReferenceId] = useState('')
  const [saving, setSaving] = useState(false)
  const save = async () => {
    if (!qty || !reason.trim() || !item.warehouseId) return
    setSaving(true)
    try {
      await api.createStockMove({ project_id: 'P001', material_id: item.materialId, batch_id: item.batchId, move_type: moveType, qty, unit: item.unitCode ?? ({ 件: 'piece', 块: 'piece', 双: 'pair', 顶: 'piece' } as Record<string, string>)[item.unit] ?? item.unit, from_warehouse_id: moveType === 'ISSUE' || moveType === 'SCRAP' ? item.warehouseId : undefined, to_warehouse_id: moveType === 'RETURN' ? item.warehouseId : undefined, reference_type: moveType === 'RETURN' ? 'ISSUE' : undefined, reference_id: referenceId || undefined, reason })
      await onSaved()
    } catch (error) { onToast(error instanceof Error ? `库存流水失败：${error.message}` : '库存流水失败') } finally { setSaving(false) }
  }
  return <div className="modal-backdrop" role="presentation" onClick={() => !saving && onClose()}><section className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="stock-move-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name="stack" /></div><button className="icon-button" title="关闭库存流水表单" onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">STOCK MOVE</p><h2 id="stock-move-title">登记 {item.material} 流水</h2><p className="modal-copy">仓库：{item.warehouse} · 批次：{item.batch}。提交后会在同一事务中更新库存余额和审计日志。</p><label className="form-field"><span>流水类型</span><select value={moveType} onChange={(event) => setMoveType(event.target.value as typeof moveType)}><option value="ISSUE">ISSUE · 领用</option><option value="RETURN">RETURN · 退库</option><option value="SCRAP">SCRAP · 报废</option></select></label><label className="form-field"><span>数量（十进制字符串）</span><input inputMode="decimal" value={qty} onChange={(event) => setQty(event.target.value)} placeholder="例如 2" /></label>{moveType === 'RETURN' && <label className="form-field"><span>原领用流水 ID</span><input value={referenceId} onChange={(event) => setReferenceId(event.target.value)} placeholder="必须引用原 ISSUE" /></label>}<label className="form-field"><span>原因</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写业务原因" /></label><div className="modal-actions"><button className="button button-quiet" disabled={saving} onClick={onClose}>取消</button><button className="button button-primary" disabled={saving || !qty || !reason.trim() || !item.warehouseId} onClick={() => void save()}><Icon name="check" />{saving ? '提交中…' : '提交流水'}</button></div></section></div>
}

function sourceTarget(ref: string): PageId | null {
  const value = ref.toLowerCase()
  if (value.includes('stock') || value.includes('inventory')) return 'inventory'
  if (value.includes('plan') || value.includes('shortage') || value.includes('calculation')) return 'shortage'
  if (value.includes('purchase') || value.includes('draft') || value.includes('order')) return 'procurement'
  if (value.includes('evidence') || value.includes('trace') || value.includes('batch') || value.includes('business_record') || value.includes('shipment:') || value.includes('delivery:') || value.includes('inspection:') || value.includes('handover:')) return 'trace'
  if (value.includes('anomal')) return 'anomalies'
  if (value.includes('ppe') || value.includes('crew') || value.includes('todo:')) return 'daily'
  return null
}

function ShortagePage({ shortage, meta, canDraft, onToast, onNavigate, onDraft, onRecalculated }: { shortage: ShortageItem[]; meta: ShortageMeta | null; canDraft: boolean; onToast: (message: string) => void; onNavigate: (page: PageId) => void; onDraft: (item: ShortageItem) => void; onRecalculated: (items: ShortageItem[], meta: ShortageMeta) => void }) {
  const [risk, setRisk] = useState('全部风险')
  const [isCalculating, setIsCalculating] = useState(false)
  const visible = shortage.filter((item) => risk === '全部风险' || item.risk === risk)
  const recalculate = async () => {
    setIsCalculating(true)
    try {
      const result = await api.calculateShortageDetailed()
      onRecalculated(result.items, result.meta)
      onToast(`shortage-v1 已完成计算，识别 ${result.items.length} 项缺料风险`)
    } catch (error) {
      onToast(error instanceof Error ? `缺料计算失败：${error.message}` : '缺料计算失败')
    } finally {
      setIsCalculating(false)
    }
  }
  return (
    <>
      <PageHeading title="缺料预测" description="确定性规则计算计划窗口内的安全库存缺口，结果可追溯到输入快照" action={isCalculating ? '计算中…' : '重新计算'} actionIcon="refresh" onAction={recalculate} />
      <section className="calculation-banner"><div className="calc-icon"><Icon name="trend" /></div><div><strong>计算版本 {meta?.calculationVersion ?? 'shortage-v1'}</strong><p>结果由计划、库存、安全库存和在途策略确定性计算。</p></div><div className="calc-meta"><span>输入快照</span><b>{meta?.inputSnapshotHash ? `sha256 · ${meta.inputSnapshotHash.slice(0, 10)}…` : '暂无计算快照'}</b></div><StatusPill tone="status-success">置信度 {meta ? meta.confidence.toFixed(2) : '—'}</StatusPill></section>
      <section className="toolbar"><div className="filter-tabs"><button className={risk === '全部风险' ? 'active' : ''} onClick={() => setRisk('全部风险')}>全部 <b>{shortage.length}</b></button><button className={risk === '高' ? 'active tab-high' : ''} onClick={() => setRisk('高')}>高风险 <b>{shortage.filter((item) => item.risk === '高').length}</b></button><button className={risk === '中' ? 'active tab-medium' : ''} onClick={() => setRisk('中')}>中风险 <b>{shortage.filter((item) => item.risk === '中').length}</b></button><button className={risk === '低' ? 'active tab-low' : ''} onClick={() => setRisk('低')}>低风险 <b>{shortage.filter((item) => item.risk === '低').length}</b></button></div><span className="toolbar-spacer" /><button className="button button-quiet" onClick={() => triggerJsonDownload(`shortage-${meta?.calculationId ?? 'result'}.json`, { calculation: meta, items: shortage })}><Icon name="download" />导出结果</button></section>
      <section className="panel shortage-table-panel"><div className="table-meta"><span>预计缺料明细</span><span className="muted">confidence {meta ? meta.confidence.toFixed(2) : '—'} · 所有数量为十进制字符串</span></div><div className="table-scroll"><table><thead><tr><th>风险物料</th><th>风险日期</th><th>当前可用</th><th>安全库存</th><th>预计缺口</th><th>建议下单量</th><th>依据</th><th /></tr></thead><tbody>{visible.map((item) => <tr key={item.id}><td><div className="table-material"><span className={`risk-badge ${riskClass(item.risk)}`}>{item.risk}</span><div><strong>{item.material}</strong><small>{item.area}</small></div></div></td><td><strong className={item.risk === '高' ? 'red-text' : item.risk === '中' ? 'orange-text' : ''}>{item.riskDate}</strong><small>低于安全库存</small></td><td>{item.available} <small className="inline-unit">{item.unit}</small></td><td>{item.safetyStock} <small className="inline-unit">{item.unit}</small></td><td><strong className="red-text">-{item.shortage}</strong> <small className="inline-unit">{item.unit}</small></td><td><strong>{item.orderQty}</strong> <small className="inline-unit">{item.unit}</small></td><td><span className="source-cell"><Icon name="document" />{item.source}<small>{item.calcId}</small></span></td><td><button className="small-button" disabled={!canDraft} title={canDraft ? '生成采购草稿' : '当前角色没有采购草稿权限'} onClick={() => onDraft(item)}>生成草稿 <Icon name="arrow" /></button></td></tr>)}</tbody></table>{visible.length === 0 && <EmptyState text="没有符合条件的风险项" />}</div></section>
      <div className="shortage-bottom"><section className="panel forecast-panel"><div className="panel-header"><div><p className="panel-kicker">PROJECTED AVAILABLE</p><h2>{shortage[0]?.material ?? '暂无风险物料'} · 可用量曲线</h2></div><button className="text-button" onClick={() => onNavigate('inventory')}>查看库存 <Icon name="arrow" /></button></div>{shortage[0] && meta?.curveByMaterial[shortage[0].id]?.length ? <div className="forecast-data-table"><div className="table-meta"><span>逐日计算曲线</span><span className="muted">来源：{meta.calculationId}</span></div><div className="table-scroll"><table><thead><tr><th>日期</th><th>计划用量</th><th>在途入库</th><th>预计可用</th><th>安全库存</th></tr></thead><tbody>{meta.curveByMaterial[shortage[0].id].map((point) => <tr key={String(point.date)}><td>{String(point.date)}</td><td>{String(point.planned_qty ?? '0')} {shortage[0].unit}</td><td>{String(point.receipt_qty ?? '0')} {shortage[0].unit}</td><td>{String(point.projected_available ?? '0')} {shortage[0].unit}</td><td>{String(point.safety_stock ?? shortage[0].safetyStock)} {shortage[0].unit}</td></tr>)}</tbody></table></div></div> : <EmptyState text="暂无逐日曲线数据" />}</section><section className="panel formula-panel"><p className="panel-kicker">CALCULATION NOTES</p><h2>计算说明</h2><div className="formula"><span>available₀</span><b>=</b><strong>现有量 − 预留 − 隔离</strong></div><div className="formula"><span>shortage_qty</span><b>=</b><strong>max(0, 安全库存 − projected)</strong></div><div className="formula"><span>order_qty</span><b>=</b><strong>按起订量及倍数向上取整</strong></div><div className="formula-note"><Icon name="check" /><span>{meta?.missingData.length ? `缺失数据：${meta.missingData.join('、')}` : '本结果由规则服务计算，不代表统计概率。'}</span></div></section></div>
    </>
  )
}


function ProcurementPage({ drafts, orders, stock, role, canSubmit, canApprove, onToast, onConfirm, onApprove, onNavigate, onRefresh }: { drafts: PurchaseDraft[]; orders: PurchaseOrderView[]; stock: StockItem[]; role: Role; canSubmit: boolean; canApprove: boolean; onToast: (message: string) => void; onConfirm: (draft: PurchaseDraft) => void; onApprove: (requestId: string) => void; onNavigate: (page: PageId) => void; onRefresh: () => Promise<void> }) {
  const [tab, setTab] = useState<'drafts' | 'orders'>('drafts')
  const [selectedDraftId, setSelectedDraftId] = useState(drafts[0]?.id ?? '')
  const [selectedOrder, setSelectedOrder] = useState<PurchaseOrderView | null>(null)
  const selectedDraft = drafts.find((draft) => draft.id === selectedDraftId)
  useEffect(() => {
    if (!selectedDraftId && drafts[0]) setSelectedDraftId(drafts[0].id)
    if (selectedDraftId && !drafts.some((draft) => draft.id === selectedDraftId)) setSelectedDraftId(drafts[0]?.id ?? '')
  }, [drafts, selectedDraftId])
  useEffect(() => { setSelectedOrder(null) }, [role])
  return (
    <>
      <PageHeading title="采购协同" description="采购申请、订单和交付状态分步确认，AI 只生成草稿不直接下单" />
      <section className="toolbar procurement-toolbar"><div className="segmented-control"><button className={tab === 'drafts' ? 'active' : ''} onClick={() => setTab('drafts')}>采购申请 <b>{drafts.length}</b></button><button className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}>订单与发货 <b>{orders.length}</b></button></div><span className="toolbar-spacer" /><StatusPill tone="status-warning"><Icon name="clock" />{drafts.filter((item) => item.status === '待确认').length} 份待确认</StatusPill></section>
      {tab === 'drafts' ? <div className="procurement-layout"><section className="panel table-panel"><div className="table-meta"><span>采购申请记录</span><span className="muted">来源：purchase_requests / drafts</span></div><div className="draft-list">{drafts.map((draft) => <button className={`draft-card ${selectedDraftId === draft.id ? 'selected' : ''}`} key={draft.id} onClick={() => setSelectedDraftId(draft.id)}><div className="draft-top"><span className={`risk-badge ${riskClass(draft.risk)}`}>{draft.risk}风险</span><StatusPill>{draft.status}</StatusPill></div><div className="draft-title"><strong>{draft.material}</strong><span>{draft.requestNo}</span></div><div className="draft-data"><span><small>数量</small><b>{draft.quantity} {draft.unit}</b></span><span><small>需要日期</small><b>{draft.neededBy}</b></span><span><small>入库仓库</small><b>{draft.warehouse}</b></span></div><div className="draft-bottom"><span><Icon name="trend" />{draft.calcId}</span><span>{draft.createdAt}</span><Icon name="chevron" /></div></button>)}{drafts.length === 0 && <EmptyState text="暂无采购申请记录" />}</div></section><aside className="panel detail-panel draft-detail">{selectedDraft ? <><div className="detail-header"><div><p className="panel-kicker">REQUEST DETAIL</p><h2>申请详情</h2><small>{selectedDraft.requestNo}</small></div><StatusPill>{selectedDraft.status}</StatusPill></div><div className="draft-review-highlight"><span className={`risk-badge ${riskClass(selectedDraft.risk)}`}>{selectedDraft.risk}风险</span><strong>{selectedDraft.material}</strong><p>{selectedDraft.reason}</p></div><dl className="detail-list"><div><dt>申请数量</dt><dd>{selectedDraft.quantity} {selectedDraft.unit}</dd></div><div><dt>需要日期</dt><dd>{selectedDraft.neededBy}</dd></div><div><dt>目标仓库</dt><dd>{selectedDraft.warehouse}</dd></div><div><dt>计算依据</dt><dd className="blue-text">{selectedDraft.calcId}</dd></div><div><dt>状态来源</dt><dd>purchase_requests</dd></div></dl><div className="diff-box"><div><span>状态机</span><b>{selectedDraft.rawStatus ?? selectedDraft.status}</b></div><div><span>数量来源</span><b>{selectedDraft.calcId}</b></div></div><div className="detail-actions">{selectedDraft.status === '待确认' ? <button className="button button-primary" disabled={!canSubmit} title={canSubmit ? '获取并审阅确认 token' : '当前角色不能提交采购申请'} onClick={() => onConfirm(selectedDraft)}><Icon name="check" />审阅确认 token</button> : selectedDraft.status === '已提交' && selectedDraft.requestId ? <button className="button button-primary" disabled={!canApprove} title={canApprove ? '获取审批 token并批准' : '当前角色没有审批权限'} onClick={() => onApprove(selectedDraft.requestId!)}><Icon name="check" />获取 token 并批准</button> : selectedDraft.status === '已提交' ? <span className="muted">关联采购申请尚未加载，刷新项目数据后再试。</span> : <button className="button button-quiet" onClick={() => onNavigate('shortage')}>返回缺料计算</button>}</div><div className="chain-note"><Icon name="lock" /><span>状态由业务 API 维护；确认动作一次性消费 token。</span></div></> : <EmptyState text="选择一份申请查看详情" />}</aside></div> : <OrdersView orders={orders} onOpen={(order) => setSelectedOrder(order)} />}
      {selectedOrder && <OrderDetailModal order={selectedOrder} stock={stock} role={role} onClose={() => setSelectedOrder(null)} onToast={onToast} onRefresh={onRefresh} />}
    </>
  )
}

function OrdersView({ orders, onOpen }: { orders: PurchaseOrderView[]; onOpen: (order: PurchaseOrderView) => void }) {
  return <section className="panel orders-panel"><div className="table-meta"><span>订单与发货</span><span className="muted">订单状态由采购与供应商动作推进</span></div><div className="orders-list">{orders.map((order) => { const item = order.items[0]; const ordered = Number(item?.qtyOrdered ?? 0); const shipped = Number(item?.qtyShipped ?? 0); const received = Number(item?.qtyReceived ?? 0); const step = order.status === 'RECEIVED' || (received >= ordered && ordered > 0) ? 4 : order.status === 'PARTIAL_RECEIVED' ? 3 : shipped >= ordered && ordered > 0 ? 2 : shipped > 0 ? 1 : 0; return <div className="order-row" key={order.id}><div className="order-main"><span className="order-icon"><Icon name="cart" /></span><div><strong>{item?.materialName ?? '未命名物料'}</strong><small>{order.poNo} · {order.supplier}</small></div></div><div className="order-progress"><div className="progress-track"><i style={{ width: `${step * 25}%` }} /></div><div className="progress-labels"><span className={step >= 1 ? 'done' : ''}>已发送</span><span className={step >= 2 ? 'done' : ''}>已发货</span><span className={step >= 3 ? 'done' : ''}>已到货</span><span className={step >= 4 ? 'done' : ''}>已验收</span></div></div><div className="order-qty"><strong>{item?.qtyShipped ?? '0'} / {item?.qtyOrdered ?? '0'} {item?.unit ?? ''}</strong><small>ETA {order.expectedDate ?? '未设置'}</small></div><StatusPill>{order.status}</StatusPill><button className="icon-button" title="打开订单详情" onClick={() => onOpen(order)}><Icon name="external" /></button></div>})}{orders.length === 0 && <EmptyState text="暂无采购订单记录" />}</div><div className="shipment-note"><Icon name="link" /><span>订单详情会显示其发货、到货和验收记录；所有数量来自业务 API。</span></div></section>
}

function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object') : []
}

type OrderWorkflowStep = 'shipment' | 'delivery' | 'inspection'

function OrderDetailModal({ order, stock, role, onClose, onToast, onRefresh }: { order: PurchaseOrderView; stock: StockItem[]; role: Role; onClose: () => void; onToast: (message: string) => void; onRefresh: () => Promise<void> }) {
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  const [workflow, setWorkflow] = useState<OrderWorkflowStep | null>(null)
  const reload = useCallback(async () => {
    try {
      const result = await api.getPurchaseOrderDetail(order.id)
      setDetail(result)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取订单详情')
    }
  }, [order.id])
  useEffect(() => { void reload() }, [reload])
  const detailOrder = detail?.order && typeof detail.order === 'object' ? detail.order as Record<string, unknown> : null
  const items = recordArray(detailOrder?.items)
  const shipments = recordArray(detailOrder?.shipments)
  const deliveries = shipments.flatMap((shipment) => recordArray(shipment.deliveries))
  const inspections = deliveries.flatMap((delivery) => recordArray(delivery.inspections))
  const hasRemainingShipment = items.some((item) => Number(textValue(item.qty_ordered, '0')) > Number(textValue(item.qty_shipped, '0')))
  const pendingShipment = shipments.find((shipment) => textValue(shipment.status) === 'SUBMITTED' && recordArray(shipment.deliveries).length === 0)
  const pendingInspection = inspections.find((inspection) => textValue(inspection.status) === 'PENDING')
  const inspectionReady = Boolean(pendingInspection || deliveries.find((delivery) => textValue(delivery.status) === 'ACCEPTED' && recordArray(delivery.inspections).length === 0))
  const completeWorkflow = async (message: string) => {
    await onRefresh()
    await reload()
    setWorkflow(null)
    onToast(message)
  }
  return <div className="modal-backdrop" role="presentation" onClick={onClose}><section className="confirm-modal business-record-modal" role="dialog" aria-modal="true" aria-labelledby="order-detail-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name="cart" /></div><button className="icon-button" title="关闭订单详情" onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">PURCHASE ORDER</p><h2 id="order-detail-title">{order.poNo}</h2>{error ? <div className="modal-warning"><Icon name="alert" /><span>{error}</span></div> : !detail ? <div className="business-record-loading"><i /><span>正在读取订单明细…</span></div> : <><p className="modal-copy">供应商：{textValue(detailOrder?.supplier_name, order.supplier)} · 状态：{textValue(detailOrder?.status, order.status)}</p><div className="business-record-section"><span>物料明细</span>{items.length ? <div className="business-record-items">{items.map((item) => <div key={textValue(item.id)}><strong>{textValue(item.material_name)}</strong><span>{textValue(item.qty_shipped, '0')} / {textValue(item.qty_ordered, '0')} {textValue(item.unit)}</span></div>)}</div> : <p>暂无订单明细。</p>}</div><div className="business-record-section"><span>交付链路</span>{shipments.length ? <div className="business-record-items">{shipments.map((shipment) => { const shipmentDeliveries = recordArray(shipment.deliveries); const shipmentInspections = shipmentDeliveries.flatMap((delivery) => recordArray(delivery.inspections)); return <div key={textValue(shipment.id)}><strong>{textValue(shipment.shipment_no, '发货记录')}</strong><span>{textValue(shipment.status)} · 到货 {shipmentDeliveries.length} 条 · 验收 {shipmentInspections.length} 条{shipmentInspections.flatMap((inspection) => recordArray(inspection.items)).map((item) => ` · 通过 ${textValue(item.qty_passed, '0')} / 不通过 ${textValue(item.qty_failed, '0')}`).join('')}</span></div> })}</div> : <p>暂无发货记录。</p>}</div><div className="business-record-section workflow-section"><span>业务推进</span><p>以下操作写入正式业务记录，并在确认后刷新订单、库存和证据投影。</p><div className="detail-actions">{role === 'SUPPLIER' && hasRemainingShipment && <button className="button button-primary" onClick={() => setWorkflow('shipment')}><Icon name="cart" />提交剩余发货</button>}{['PROJECT_OWNER', 'SITE_ADMIN'].includes(role) && pendingShipment && <button className="button button-primary" onClick={() => setWorkflow('delivery')}><Icon name="stack" />登记到货</button>}{role === 'INSPECTOR' && inspectionReady && <button className="button button-primary" onClick={() => setWorkflow('inspection')}><Icon name="check" />执行验收确认</button>}{!(role === 'SUPPLIER' && hasRemainingShipment) && !(['PROJECT_OWNER', 'SITE_ADMIN'].includes(role) && pendingShipment) && !(role === 'INSPECTOR' && inspectionReady) && <span className="muted">当前角色没有可执行的下一步，或订单已完成可执行阶段。</span>}</div></div></>}<div className="modal-actions"><button className="button button-primary" onClick={onClose}><Icon name="check" />完成查看</button></div></section>{workflow && detailOrder && <OrderWorkflowModal step={workflow} order={detailOrder} items={items} shipments={shipments} stock={stock} onClose={() => setWorkflow(null)} onComplete={completeWorkflow} onToast={onToast} />}</div>
}

function OrderWorkflowModal({ step, order, items, shipments, stock, onClose, onComplete, onToast }: { step: OrderWorkflowStep; order: Record<string, unknown>; items: Array<Record<string, unknown>>; shipments: Array<Record<string, unknown>>; stock: StockItem[]; onClose: () => void; onComplete: (message: string) => Promise<void>; onToast: (message: string) => void }) {
  const remainingItems = items.filter((item) => Number(textValue(item.qty_ordered, '0')) > Number(textValue(item.qty_shipped, '0')))
  const availableShipment = shipments.find((shipment) => textValue(shipment.status) === 'SUBMITTED' && recordArray(shipment.deliveries).length === 0)
  const allDeliveries = shipments.flatMap((shipment) => recordArray(shipment.deliveries))
  const pendingInspection = allDeliveries.flatMap((delivery) => recordArray(delivery.inspections)).find((inspection) => textValue(inspection.status) === 'PENDING')
  const inspectionDelivery = pendingInspection ? allDeliveries.find((delivery) => textValue(delivery.id) === textValue(pendingInspection.delivery_id)) : allDeliveries.find((delivery) => textValue(delivery.status) === 'ACCEPTED' && recordArray(delivery.inspections).length === 0)
  const shipmentItem = recordArray(availableShipment?.items)[0]
  const deliveryItem = recordArray(inspectionDelivery?.items)[0]
  const warehouseChoices = Array.from(new Map(stock.filter((item) => item.warehouseId).map((item) => [item.warehouseId!, item.warehouse])).entries())
  const initialRemaining = remainingItems[0]
  const [qty, setQty] = useState(step === 'shipment' ? String(Math.max(0, Number(textValue(initialRemaining?.qty_ordered, '0')) - Number(textValue(initialRemaining?.qty_shipped, '0')))) : step === 'delivery' ? textValue(shipmentItem?.qty_shipped) : textValue(deliveryItem?.qty_arrived))
  const [failedQty, setFailedQty] = useState('0')
  const [batchNo, setBatchNo] = useState(`DEMO-${textValue(order.po_no, 'PO')}-${Date.now().toString().slice(-6)}`)
  const [warehouseId, setWarehouseId] = useState(warehouseChoices[0]?.[0] ?? '')
  const [created, setCreated] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const saveShipment = async () => {
    if (!initialRemaining || !qty || !batchNo.trim()) return
    setBusy(true); setError('')
    try {
      const result = await api.createShipment({ purchase_order_id: textValue(order.id), items: [{ material_id: textValue(initialRemaining.material_id), qty, unit: textValue(initialRemaining.unit), batch_no: batchNo.trim() }] })
      await onComplete(`发货单 ${textValue(result.shipment_no, '已创建')} 已写入数据库`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '提交发货失败') } finally { setBusy(false) }
  }
  const saveDelivery = async () => {
    if (!availableShipment || !shipmentItem || !warehouseId || !qty) return
    setBusy(true); setError('')
    try {
      const result = await api.createDelivery({ shipment_id: textValue(availableShipment.id), warehouse_id: warehouseId, items: [{ shipment_item_id: textValue(shipmentItem.id), qty_arrived: qty }] })
      setCreated(result)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '登记到货失败') } finally { setBusy(false) }
  }
  const confirmDelivery = async () => {
    const id = textValue(created?.id); const token = textValue(created?.confirmation_token)
    if (!id || !token) return
    setBusy(true); setError('')
    try { await api.confirmDelivery(id, token); await onComplete(`到货单 ${textValue(created?.delivery_no, id)} 已确认`) } catch (reason) { setError(reason instanceof Error ? reason.message : '确认到货失败') } finally { setBusy(false) }
  }
  const saveInspection = async () => {
    if (pendingInspection) {
      setBusy(true); setError('')
      try {
        const tokenResult = await api.getInspectionConfirmationToken(textValue(pendingInspection.id))
        const token = textValue(tokenResult.confirmation_token)
        if (!token) throw new Error('服务端没有返回验收确认 token')
        await api.confirmInspection(textValue(pendingInspection.id), token)
        await onComplete(`验收单 ${textValue(pendingInspection.inspection_no, textValue(pendingInspection.id))} 已确认`)
      } catch (reason) { setError(reason instanceof Error ? reason.message : '确认验收失败') } finally { setBusy(false) }
      return
    }
    if (!inspectionDelivery || !deliveryItem || !qty) return
    const arrived = Number(textValue(deliveryItem.qty_arrived, '0')); const passed = Number(qty); const failed = Number(failedQty)
    if (!Number.isFinite(passed) || !Number.isFinite(failed) || passed < 0 || failed < 0 || Math.abs(passed + failed - arrived) > 0.000001) {
      setError(`通过与不通过数量之和必须等于到货数量 ${textValue(deliveryItem.qty_arrived)}`)
      return
    }
    setBusy(true); setError('')
    try {
      const resultName = failed === 0 ? 'PASSED' : passed === 0 ? 'FAILED' : 'PARTIAL'
      const result = await api.createInspection({ delivery_id: textValue(inspectionDelivery.id), result: resultName, items: [{ delivery_item_id: textValue(deliveryItem.id), qty_passed: qty, qty_failed: failedQty, failure_reason: failed > 0 ? '现场验收不通过，已隔离' : undefined }], notes: '由演示工作台登记验收' })
      setCreated(result)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '登记验收失败') } finally { setBusy(false) }
  }
  const confirmInspection = async () => {
    const id = textValue(created?.id); const token = textValue(created?.confirmation_token)
    if (!id || !token) return
    setBusy(true); setError('')
    try { await api.confirmInspection(id, token); await onComplete(`验收单 ${textValue(created?.inspection_no, id)} 已确认入库`) } catch (reason) { setError(reason instanceof Error ? reason.message : '确认验收失败') } finally { setBusy(false) }
  }
  const title = step === 'shipment' ? '提交供应商发货' : step === 'delivery' ? '登记并确认到货' : '登记并确认验收'
  return <div className="modal-backdrop workflow-modal-backdrop" role="presentation" onClick={() => !busy && onClose()}><section className="confirm-modal workflow-modal" role="dialog" aria-modal="true" aria-labelledby="workflow-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name={step === 'shipment' ? 'cart' : step === 'delivery' ? 'stack' : 'check'} /></div><button className="icon-button" title="关闭业务操作" disabled={busy} onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">REAL BUSINESS COMMAND</p><h2 id="workflow-title">{title}</h2><p className="modal-copy">操作将调用受权限、幂等和审计保护的业务 API；确认后会刷新订单和证据投影。</p>{error && <div className="modal-warning"><Icon name="alert" /><span>{error}</span></div>}{step === 'shipment' && <><label className="form-field"><span>订单物料</span><input readOnly value={`${textValue(initialRemaining?.material_name)} · 剩余 ${Math.max(0, Number(textValue(initialRemaining?.qty_ordered, '0')) - Number(textValue(initialRemaining?.qty_shipped, '0')))} ${textValue(initialRemaining?.unit)}`} /></label><label className="form-field"><span>本次发货数量</span><input inputMode="decimal" value={qty} onChange={(event) => setQty(event.target.value)} /></label><label className="form-field"><span>供应商批次号</span><input value={batchNo} onChange={(event) => setBatchNo(event.target.value)} /></label></>}{step === 'delivery' && !created && <><label className="form-field"><span>发货单</span><input readOnly value={textValue(availableShipment?.shipment_no)} /></label><label className="form-field"><span>到货数量</span><input inputMode="decimal" value={qty} onChange={(event) => setQty(event.target.value)} /></label><label className="form-field"><span>入库仓库</span><select value={warehouseId} onChange={(event) => setWarehouseId(event.target.value)}>{warehouseChoices.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label></>}{step === 'inspection' && !created && !pendingInspection && <><label className="form-field"><span>到货单</span><input readOnly value={textValue(inspectionDelivery?.delivery_no)} /></label><label className="form-field"><span>验收通过数量</span><input inputMode="decimal" value={qty} onChange={(event) => setQty(event.target.value)} /></label><label className="form-field"><span>验收不通过数量</span><input inputMode="decimal" value={failedQty} onChange={(event) => setFailedQty(event.target.value)} /></label></>}{step === 'delivery' && created && <div className="draft-confirmation-panel"><div><p className="panel-kicker">ARRIVAL · PENDING_CONFIRMATION</p><h2>到货事实已登记</h2><p>到货单 <code>{textValue(created.delivery_no, textValue(created.id))}</code> 已生成，等待一次性 token 确认。</p></div></div>}{step === 'inspection' && created && <div className="draft-confirmation-panel"><div><p className="panel-kicker">INSPECTION · PENDING_CONFIRMATION</p><h2>验收记录已登记</h2><p>验收单 <code>{textValue(created.inspection_no, textValue(created.id))}</code> 已生成，确认后才会生成入库流水。</p></div></div>}{step === 'inspection' && pendingInspection && <div className="draft-confirmation-panel"><div><p className="panel-kicker">INSPECTION · PENDING_CONFIRMATION</p><h2>待确认验收记录</h2><p>验收单 <code>{textValue(pendingInspection.inspection_no, textValue(pendingInspection.id))}</code> 已由业务记录保存，当前操作会重新获取本人的一次性确认 token。</p></div></div>}<div className="modal-actions"><button className="button button-quiet" disabled={busy} onClick={onClose}>取消</button>{step === 'shipment' && <button className="button button-primary" disabled={busy || !initialRemaining || !qty || !batchNo.trim()} onClick={() => void saveShipment()}><Icon name="check" />{busy ? '提交中…' : '提交发货'}</button>}{step === 'delivery' && (created ? <button className="button button-primary" disabled={busy} onClick={() => void confirmDelivery()}><Icon name="check" />{busy ? '确认中…' : '确认到货'}</button> : <button className="button button-primary" disabled={busy || !availableShipment || !shipmentItem || !warehouseId || !qty} onClick={() => void saveDelivery()}><Icon name="check" />{busy ? '登记中…' : '登记到货'}</button>)}{step === 'inspection' && (created ? <button className="button button-primary" disabled={busy} onClick={() => void confirmInspection()}><Icon name="check" />{busy ? '确认中…' : '确认验收并入库'}</button> : <button className="button button-primary" disabled={busy || (!pendingInspection && (!inspectionDelivery || !deliveryItem || !qty))} onClick={() => void saveInspection()}><Icon name="check" />{busy ? '处理中…' : pendingInspection ? '获取 token 并确认' : '登记验收'}</button>)}</div></section></div>
}

function TracePage({ traces, batches, documents, focusEventId, chainOnline, canAnchor, role, onTraceChange, onRefresh, onToast }: { traces: TraceEvent[]; batches: BatchOption[]; documents: DocumentView[]; focusEventId?: string | null; chainOnline: boolean | null; canAnchor: boolean; role: Role; onTraceChange: (events: TraceEvent[]) => void; onRefresh: () => Promise<void>; onToast: (message: string) => void }) {
  const [selected, setSelected] = useState(traces[2]?.id ?? traces[0]?.id ?? '')
  const [selectedBatchId, setSelectedBatchId] = useState(batches[0]?.id ?? '')
  const [recordOpen, setRecordOpen] = useState(false)
  const [verifiedAt, setVerifiedAt] = useState<Record<string, string>>({})
  const [verifying, setVerifying] = useState(false)
  const event = traces.find((item) => item.id === selected)
  const isVerified = event ? Boolean(verifiedAt[event.id]) : false

  useEffect(() => {
    if (!selectedBatchId && batches[0]) setSelectedBatchId(batches[0].id)
    if (selectedBatchId && !batches.some((batch) => batch.id === selectedBatchId)) setSelectedBatchId(batches[0]?.id ?? '')
  }, [batches, selectedBatchId])

  useEffect(() => {
    if (!selected && traces[0]) setSelected(traces[0].id)
  }, [traces, selected])

  useEffect(() => {
    if (focusEventId && traces.some((item) => item.id === focusEventId)) setSelected(focusEventId)
  }, [focusEventId, traces])

  const verifyEvent = async () => {
    if (!event || verifying) return
    setVerifying(true)
    try {
      const verification = await api.verifyEvidence(event.id)
      if (!verification.matches) {
        onToast(verification.txExists ? '哈希校验未通过，请查看业务记录和更正链路' : '该事件尚未锚定，暂时无法进行链上哈希校验')
        return
      }
      const checkedAt = formatLocalizedDateTime(new Date(), getActiveLocale(), { hour: '2-digit', minute: '2-digit' })
      setVerifiedAt((current) => ({ ...current, [event.id]: checkedAt }))
      onToast(`已验证 ${event.label}，数据库摘要与模拟链记录匹配`)
    } catch (error) {
      onToast(error instanceof Error ? `核验失败：${error.message}` : '核验失败')
    } finally {
      setVerifying(false)
    }
  }

  const anchorEvent = async () => {
    if (!event || verifying || event.status !== '待上链') return
    setVerifying(true)
    try {
      const anchored = await api.anchorEvidence(event.id) as { tx_id?: string; txId?: string; status?: string }
      const txId = anchored.tx_id ?? anchored.txId
      const nextStatus: TraceEvent['status'] = anchored.status === 'FAILED' ? '链上失败' : anchored.status === 'RETRYING' ? '重试中' : anchored.status === 'CONFIRMED' ? '已确认' : '已提交'
      onTraceChange(traces.map((item) => item.id === event.id ? { ...item, status: nextStatus, txId: txId ?? item.txId, detail: nextStatus === '链上失败' || nextStatus === '重试中' ? '模拟链暂不可用，业务记录不受影响，可稍后重试。' : '已由授权组织提交模拟链，可重新验证哈希。' } : item))
      onToast(nextStatus === '链上失败' || nextStatus === '重试中' ? `${event.label} 已进入证据重试队列` : `已提交 ${event.label} 到模拟链${txId ? `，交易号 ${txId}` : ''}`)
    } catch (error) {
      onToast(error instanceof Error ? `锚定失败：${error.message}` : '锚定失败')
    } finally {
      setVerifying(false)
    }
  }

  const retryEvent = async () => {
    if (!event || verifying || event.status !== '链上失败') return
    setVerifying(true)
    try {
      const retried = await api.retryEvidence(event.id) as { tx_id?: string; txId?: string; status?: string }
      const txId = retried.tx_id ?? retried.txId
      const nextStatus: TraceEvent['status'] = retried.status === 'CONFIRMED' ? '已确认' : retried.status === 'FAILED' ? '链上失败' : retried.status === 'RETRYING' ? '重试中' : '已提交'
      onTraceChange(traces.map((item) => item.id === event.id ? { ...item, status: nextStatus, txId: txId ?? item.txId, detail: nextStatus === '已提交' || nextStatus === '已确认' ? '重试提交成功，可重新验证哈希。' : '模拟链仍不可用，业务记录保持不变。' } : item))
      onToast(nextStatus === '已提交' || nextStatus === '已确认' ? `已重试 ${event.label}${txId ? `，交易号 ${txId}` : ''}` : `${event.label} 仍在重试队列中`)
    } catch (error) {
      onToast(error instanceof Error ? `重试失败：${error.message}` : '重试失败')
    } finally {
      setVerifying(false)
    }
  }

  const verifyAll = async () => {
    if (verifying) return
    setVerifying(true)
    try {
      const verifiable = traces.filter((item) => !['待上链', '重试中', '链上失败'].includes(item.status))
      const outcomes = await Promise.all(verifiable.map((item) => api.verifyEvidence(item.id)))
      const checkedAt = formatLocalizedDateTime(new Date(), getActiveLocale(), { hour: '2-digit', minute: '2-digit' })
      setVerifiedAt((current) => ({
        ...current,
        ...Object.fromEntries(verifiable.filter((_item, index) => outcomes[index]?.matches).map((item) => [item.id, checkedAt])),
      }))
      onToast(`已完成 ${outcomes.filter((outcome) => outcome.matches).length} 条可锚定事件的哈希校验`)
    } catch (error) {
      onToast(error instanceof Error ? `批量核验失败：${error.message}` : '批量核验失败')
    } finally {
      setVerifying(false)
    }
  }

  const changeBatch = async (batchId: string) => {
    setSelectedBatchId(batchId)
    setRecordOpen(false)
    try {
      const events = await api.getTrace(batchId)
      onTraceChange(events)
      setSelected(events[0]?.id ?? '')
    } catch (error) {
      onToast(error instanceof Error ? `无法读取批次追溯：${error.message}` : '无法读取批次追溯')
    }
  }

  return (
    <>
      <PageHeading title="证据追溯" description="沿业务记录查看发货、到货、验收和交接事件，链上只保存摘要与哈希" action={verifying ? '核验中…' : '验证全部事件'} actionIcon="check" onAction={() => void verifyAll()} />
      <section className="trace-banner">
        <div className="trace-banner-icon"><Icon name="link" /></div>
        <div><strong>模拟链 · MockEvidenceAdapter</strong><p>演示环境生成确定性交易号，真实 Fabric 尚未接入。业务状态与链上提交异步解耦。</p></div>
        <StatusPill tone={chainOnline ? 'status-success' : chainOnline === false ? 'status-danger' : 'status-warning'}><Icon name={chainOnline ? 'check' : 'clock'} />{chainOnline ? '服务正常' : chainOnline === false ? '服务不可用' : '检查中'}</StatusPill>
        <span className="trace-adapter-copy">待锚定 {traces.filter((item) => item.status === '待上链' || item.status === '重试中').length} · 失败 {traces.filter((item) => item.status === '链上失败').length} · 可独立核验</span>
      </section>
      <div className="trace-layout">
        <section className="panel timeline-panel">
          <div className="timeline-header">
            <div><p className="panel-kicker">BATCH TRACE</p><h2>批次 {batches.find((batch) => batch.id === selectedBatchId)?.batchNo ?? '未选择'}</h2><p>{batches.find((batch) => batch.id === selectedBatchId)?.material ?? '请选择批次'} · 可用 {batches.find((batch) => batch.id === selectedBatchId)?.available ?? '0'} {batches.find((batch) => batch.id === selectedBatchId)?.unit ?? ''}</p></div>
            <select aria-label="选择追溯批次" value={selectedBatchId} onChange={(change) => void changeBatch(change.target.value)}>{batches.map((batch) => <option key={batch.id} value={batch.id}>{batch.batchNo} · {batch.material}</option>)}</select>
          </div>
          <div className="timeline">
            {traces.map((item, index) => (
              <button className={`timeline-event ${item.id === selected ? 'selected' : ''}`} key={item.id} onClick={() => { setSelected(item.id); setRecordOpen(false) }}>
                <div className={`timeline-node tone-${item.tone}`}><Icon name={item.type === 'SHIPMENT' ? 'cart' : item.type === 'DELIVERY' ? 'stack' : item.type === 'INSPECTION' ? 'check' : 'link'} /></div>
                <div className="timeline-content">
                  <div className="timeline-top"><span className="event-type">{item.type}</span><StatusPill>{item.status}</StatusPill><time>{item.time}</time></div>
                  <strong>{item.label}</strong><p>{item.reference}</p><small>{item.actor}</small>
                </div>
                {index < traces.length - 1 && <span className="timeline-connector" />}
              </button>
            ))}
            {traces.length === 0 && <EmptyState text="该批次暂无证据事件" />}
          </div>
        </section>
        <aside className="panel evidence-panel">
          {event ? <>
            <div className="evidence-heading"><span className={`trace-mini-icon tone-${event.tone}`}><Icon name="document" /></span><div><p className="panel-kicker">EVENT DETAIL</p><h2>{event.label}</h2><small>{event.type} · {event.id}</small></div></div>
            <div className="evidence-status"><StatusPill>{event.status}</StatusPill><span>版本 v1 · 发生时间 {event.time}</span></div>
            <p className="evidence-detail">{event.detail}</p>
            <dl className="detail-list">
              <div><dt>业务引用</dt><dd>{event.reference}</dd></div>
              <div><dt>操作者</dt><dd>{event.actor}</dd></div>
              <div><dt>交易号</dt><dd className={event.txId ? 'mono green-text' : 'mono'}>{event.txId ?? '等待双方确认'}</dd></div>
              <div><dt>Payload hash</dt><dd className="mono">{event.hash}</dd></div>
            </dl>
            <div className={`verification-box ${event.status === '待上链' || event.status === '重试中' ? 'verification-pending' : isVerified ? 'verification-fresh' : ''}`}>
              <div className="verification-icon"><Icon name={event.status === '待上链' || event.status === '重试中' ? 'clock' : 'check'} /></div>
              <div>
                <strong>{event.status === '待上链' ? '业务记录已确认，等待锚定' : event.status === '重试中' ? '正在重试链上提交' : event.status === '链上失败' ? '链上提交失败，可重试' : isVerified ? `已于 ${verifiedAt[event.id]} 完成核验` : event.status === '已提交' ? '已提交模拟链，等待确认' : '哈希校验匹配'}</strong>
                <p>{event.status === '待上链' ? '该事件尚未提交模拟链；请由授权组织发起锚定。' : event.status === '重试中' ? '后台重试不会修改数据库业务记录。' : event.status === '链上失败' ? '业务记录保持不变；可再次提交证据 outbox。' : event.status === '已提交' ? '交易已返回，可执行只读哈希核验。' : '数据库 payload 与链上摘要一致，未发现更正事件。'}</p>
              </div>
            </div>
            <div className="detail-actions">
              <button className="button button-primary" disabled={verifying || event.status === '重试中' || ((event.status === '待上链' || event.status === '链上失败') && !canAnchor)} title={canAnchor || !['待上链', '链上失败'].includes(event.status) ? undefined : '当前角色没有证据锚定权限'} onClick={() => void (event.status === '待上链' ? anchorEvent() : event.status === '链上失败' ? retryEvent() : verifyEvent())}><Icon name={event.status === '链上失败' || event.status === '重试中' ? 'refresh' : 'check'} />{verifying ? '处理中…' : event.status === '待上链' ? '锚定事件' : event.status === '链上失败' ? '重试提交' : event.status === '重试中' ? '重试中…' : '重新验证'}</button>
              <button className="button button-quiet" onClick={() => setRecordOpen(true)}>查看业务记录 <Icon name="external" /></button>
            </div>
            <div className="chain-note"><Icon name="lock" /><span>完整单据保存在数据库 / 对象存储；链上只写 SHA-256 摘要。<br />请勿将下载 URL 或敏感字段复制到链上。</span></div>
          </> : <EmptyState text="选择一条事件查看详情" />}
        </aside>
      </div>
      {recordOpen && event && <BusinessRecordModal event={event} documents={documents} canConfirmHandover={['PROJECT_OWNER', 'SUPPLIER'].includes(role)} onWorkflowUpdated={onRefresh} onClose={() => setRecordOpen(false)} onToast={onToast} />}
    </>
  )
}

function BusinessRecordModal({ event, documents, canConfirmHandover, onWorkflowUpdated, onClose, onToast }: { event: TraceEvent; documents: DocumentView[]; canConfirmHandover: boolean; onWorkflowUpdated: () => Promise<void>; onClose: () => void; onToast: (message: string) => void }) {
  const [record, setRecord] = useState<BusinessRecord | null>(null)
  const [error, setError] = useState('')
  const [handoverConfirming, setHandoverConfirming] = useState(false)

  useEffect(() => {
    let active = true
    void api.getBusinessRecord(event.id).then((result) => {
      if (active) setRecord(result)
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : '无法读取业务记录')
    })
    return () => { active = false }
  }, [event.id])

  const recordKind = ({ SHIPMENT: '发货单', DELIVERY: '到货确认单', INSPECTION: '验收记录', HANDOVER: '交接单' } as Record<TraceEvent['type'], string>)[event.type]
  const documentNo = record ? textValue(
    event.type === 'SHIPMENT' ? record.record.shipment_no
      : event.type === 'DELIVERY' ? record.record.delivery_no
        : event.type === 'INSPECTION' ? record.record.inspection_no
          : record.record.handover_no,
    event.reference,
  ) : event.reference
  const recordStatus = record ? textValue(record.record.status, event.status) : event.status
  const recordStatusLabel = ({
    ACCEPTED: '已确认', PASSED: '已通过', PARTIAL: '部分通过', SUBMITTED: '已提交', ARRIVED: '待确认', PENDING: '待处理', FAILED: '失败', REJECTED: '已拒收',
  } as Record<string, string>)[recordStatus] ?? recordStatus
  const items = record?.items ?? []
  const linkedDocumentIds = items.flatMap((item) => Array.isArray(item.quality_file_ids) ? item.quality_file_ids.map(String) : [])
  const linkedDocuments = documents.filter((document) => linkedDocumentIds.includes(document.id))
  const [downloading, setDownloading] = useState('')
  const download = async (file: DocumentView) => {
    setDownloading(file.id)
    try {
      const blob = await api.downloadDocument(file.id)
      const url = URL.createObjectURL(blob)
      const anchor = globalThis.document.createElement('a')
      anchor.href = url
      anchor.download = `zhufulian-${file.id}`
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (reason) {
      onToast(reason instanceof Error ? `文件下载失败：${reason.message}` : '文件下载失败')
    } finally {
      setDownloading('')
    }
  }
  const confirmHandover = async () => {
    if (!record || event.type !== 'HANDOVER' || handoverConfirming) return
    const handoverId = textValue(record.record.id)
    if (!handoverId) return
    setHandoverConfirming(true)
    try {
      const token = await api.getHandoverConfirmationToken(handoverId)
      const confirmationToken = textValue(token.confirmation_token)
      if (!confirmationToken) throw new Error('服务端没有返回交接确认 token')
      const updated = await api.confirmHandover(handoverId, confirmationToken)
      const latest = await api.getBusinessRecord(event.id)
      setRecord(latest)
      await onWorkflowUpdated()
      onToast(textValue(updated.status) === 'CONFIRMED' ? '双方交接已确认，调拨流水与证据事件已生成' : '本方交接已确认，等待另一方确认')
    } catch (reason) {
      onToast(reason instanceof Error ? `交接确认失败：${reason.message}` : '交接确认失败')
    } finally {
      setHandoverConfirming(false)
    }
  }
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section className="confirm-modal business-record-modal" role="dialog" aria-modal="true" aria-labelledby="business-record-title" onClick={(click) => click.stopPropagation()}>
        <div className="business-record-head">
          <div className="modal-icon"><Icon name="document" /></div>
          <button className="icon-button" title="关闭业务记录" onClick={onClose}><Icon name="close" /></button>
        </div>
        <p className="panel-kicker">BUSINESS RECORD</p>
        <h2 id="business-record-title">{recordKind}</h2>
        <p className="modal-copy">这是链上摘要对应的数据库业务记录。附件原件和完整字段保持在受控存储中。</p>
        {error ? <div className="modal-warning"><Icon name="alert" /><span>无法读取业务记录：{error}</span></div> : !record ? <div className="business-record-loading"><i /><span>正在读取业务记录…</span></div> : <>
          <div className="business-record-summary">
            <div><span>业务单号</span><strong>{documentNo}</strong></div>
            <div><span>记录状态</span><StatusPill>{recordStatusLabel}</StatusPill></div>
            <div><span>业务时间</span><strong>{textValue(record.record.shipped_at ?? record.record.arrived_at ?? record.record.inspected_at ?? record.record.confirmed_at, event.time)}</strong></div>
            <div><span>证据类型</span><strong>{record.recordType}</strong></div>
          </div>
          <div className="business-record-section">
            <span>业务明细</span>
            {items.length ? <div className="business-record-items">{items.map((item) => <div key={textValue(item.id)}><strong>{textValue(item.material_name, textValue(item.material_id))}</strong><span>{textValue(item.batch_no, '无批次')} · {event.type === 'INSPECTION' ? `通过 ${textValue(item.qty_passed, '0')} / 不通过 ${textValue(item.qty_failed, '0')}` : textValue(item.qty_shipped ?? item.qty_arrived ?? item.qty, '0')} {textValue(item.unit ?? item.material_unit)}</span></div>)}</div> : <p>该业务记录没有可展示的物料明细。</p>}
          </div>
          <div className="business-record-section record-audit">
            <span>审计与证据</span>
            <div><b>证据事件</b><code>{textValue(record.event.event_type)} · {textValue(record.event.id)}</code></div>
            <div><b>链上交易</b><code>{textValue(record.event.tx_id, '尚未锚定')}</code></div>
            <div><b>Payload hash</b><code>{textValue(record.event.payload_hash)}</code></div>
            <div><b>审计记录</b><code>{record.auditLogs.length} 条</code></div>
          </div>
          {event.type === 'HANDOVER' && <div className="business-record-section"><span>双方确认</span><div className="business-record-items"><div><strong>来源组织</strong><span>{record.record.source_confirmed ? '已确认' : '等待确认'}</span></div><div><strong>目标组织</strong><span>{record.record.target_confirmed ? '已确认' : '等待确认'}</span></div></div>{textValue(record.record.status) !== 'CONFIRMED' && <div className="detail-actions"><button className="button button-primary" disabled={!canConfirmHandover || handoverConfirming} title={canConfirmHandover ? '获取本方确认 token 并确认交接' : '只有交接双方授权用户可以确认'} onClick={() => void confirmHandover()}><Icon name="check" />{handoverConfirming ? '确认中…' : '确认本方交接'}</button></div>}</div>}
          {linkedDocuments.length > 0 && <div className="business-record-section"><span>关联文件</span><div className="business-record-items">{linkedDocuments.map((file) => <div key={file.id}><strong>{file.documentType}</strong><span>{file.mime} · {Math.round(file.sizeBytes / 1024 * 10) / 10} KB · SHA-256 {file.sha256.slice(0, 12)}… <button className="file-download-button" disabled={downloading === file.id} onClick={() => void download(file)}>{downloading === file.id ? '下载中…' : '下载文件'}</button></span></div>)}</div></div>}
        </>}
        <div className="modal-actions"><button className="button button-primary" onClick={onClose}><Icon name="check" />完成查看</button></div>
      </section>
    </div>
  )
}


function AssistantPage({ role, onToast, onNavigate }: { role: Role; onToast: (message: string) => void; onNavigate: (page: PageId) => void }) {
  const [messages, setMessages] = useState<AiMessage[]>(isMockMode ? initialMessages : [])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(!isMockMode)
  const [historyError, setHistoryError] = useState('')
  const [runId, setRunId] = useState<string | null>(null)
  const threadId = useRef('thread-demo-p001')
  const claims = (value: unknown) => Array.isArray(value) ? value.map((item) => item && typeof item === 'object' ? textValue((item as Record<string, unknown>).text) : textValue(item)).filter(Boolean) : []
  const restore = (items: Array<Record<string, unknown>>): AiMessage[] => items.map((item) => ({
    id: textValue(item.id, `ai-${Date.now()}`),
    role: textValue(item.role) === 'user' ? 'user' : 'assistant',
    text: textValue(item.text, '该次运行未生成可展示的回复。'),
    timestamp: formatDateTime(textValue(item.created_at)) || '刚刚',
    facts: claims(item.facts),
    calculations: claims(item.calculations),
    hypotheses: claims(item.hypotheses),
    warnings: claims(item.warnings),
    sourceRefs: Array.isArray(item.source_refs) ? item.source_refs.map(String) : [],
    actions: claims(item.next_actions),
  }))
  const loadHistory = useCallback(async () => {
    if (isMockMode) return
    setLoadingHistory(true)
    try {
      setMessages(restore(await api.getAiThreadMessages(threadId.current)))
      setHistoryError('')
    } catch (reason) {
      setHistoryError(reason instanceof Error ? reason.message : '无法恢复 AI 会话')
    } finally {
      setLoadingHistory(false)
    }
  }, [])
  useEffect(() => {
    if (!isMockMode) setMessages([])
    void loadHistory()
  }, [loadHistory, role])
  const send = async (text = input) => {
    const clean = text.trim()
    if (!clean || sending) return
    setInput('')
    setMessages((current) => [...current, { id: `user-${Date.now()}`, role: 'user', text: clean, timestamp: '刚刚' }])
    setSending(true)
    try {
      const response = await api.sendAgentMessage(threadId.current, clean)
      setMessages((current) => [...current, response])
    } catch (error) {
      onToast(error instanceof Error ? `AI 查询失败：${error.message}` : 'AI 查询失败')
    } finally {
      setSending(false)
    }
  }
  const clearThread = async () => {
    if (sending || loadingHistory) return
    if (isMockMode) {
      setMessages(initialMessages)
      onToast('已恢复显式演示会话')
      return
    }
    setLoadingHistory(true)
    try {
      const result = await api.clearAiThread(threadId.current)
      setMessages([])
      setHistoryError('')
      onToast(`当前会话已清空；${textValue(result.cleared_runs, '0')} 次运行仍保留在审计记录中`)
    } catch (reason) {
      onToast(reason instanceof Error ? `清空会话失败：${reason.message}` : '清空会话失败')
    } finally {
      setLoadingHistory(false)
    }
  }
  return (
    <>
      <PageHeading title="AI 现场助手" description="通过受控工具查询事实、执行确定性计算，写入动作始终需要人工确认" />
      <div className="assistant-layout"><section className="panel chat-panel"><div className="chat-header"><div className="assistant-avatar"><Icon name="bot" /></div><div><strong>筑福链助手</strong><small><i className="online-dot" />工具服务在线 · 最多 8 次调用</small></div><span className="toolbar-spacer" /><StatusPill tone="status-success">受控模式</StatusPill><button className="icon-button" title="清空当前线程" disabled={sending || loadingHistory} onClick={() => void clearThread()}><Icon name="refresh" /></button></div>{historyError && <div className="data-error" role="alert"><Icon name="alert" /><span>会话历史未恢复：{historyError}</span><button onClick={() => void loadHistory()}>重试</button></div>}<div className="chat-body">{loadingHistory && <div className="business-record-loading"><i /><span>正在恢复当前用户的持久化会话…</span></div>}{!loadingHistory && messages.length === 0 && <EmptyState text="发送问题后，系统会调用授权工具并保留运行记录。" />}{messages.map((message) => <div className={`message-row ${message.role}`} key={message.id}><div className="message-avatar">{message.role === 'assistant' ? <Icon name="bot" /> : '项'}</div><div className="message-content"><div className="message-bubble">{message.text}</div>{message.role === 'assistant' && <><div className="claim-grid">{message.facts && message.facts.length > 0 && <ClaimBlock title="事实 FACT" tone="fact" items={message.facts} />}{message.calculations && message.calculations.length > 0 && <ClaimBlock title="计算 CALCULATION" tone="calculation" items={message.calculations} />}{message.hypotheses && message.hypotheses.length > 0 && <ClaimBlock title="推测 HYPOTHESIS" tone="hypothesis" items={message.hypotheses} />}</div>{message.warnings && message.warnings.length > 0 && <div className="ai-warning"><Icon name="alert" /><span>{message.warnings.join('；')}</span></div>}{message.sourceRefs && message.sourceRefs.length > 0 && <div className="source-refs"><Icon name="link" /><span>来源</span>{message.sourceRefs.map((ref) => { const target = sourceTarget(ref); return target ? <button className="source-ref-button" key={ref} title={`打开${pageTitles[target].title}`} onClick={() => onNavigate(target)}>{ref}</button> : <span key={ref}>{ref}</span> })}<button className="source-run-button" onClick={() => setRunId(message.id)}>查看本次运行</button></div>}{message.actions && message.actions.length > 0 && <div className="ai-actions">{message.actions.map((action) => <button className="suggestion-chip" key={action} onClick={() => send(action)}>{action} <Icon name="arrow" /></button>)}</div>}</> }<time>{message.timestamp}</time></div></div>)}{sending && <div className="message-row assistant"><div className="message-avatar"><Icon name="bot" /></div><div className="message-content"><div className="message-bubble typing"><i /><i /><i /></div></div></div>}</div><div className="chat-composer"><div className="composer-tools"><span className="composer-hint"><Icon name="lock" />仅查询受控项目数据</span><span className="toolbar-spacer" /><span className="composer-count">{input.length}/500</span></div><div className="composer-row"><textarea value={input} maxLength={500} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} placeholder="例如：下周三号楼砌体会缺什么？" /><button className="send-button" disabled={!input.trim() || sending || loadingHistory} onClick={() => void send()} title="发送消息"><Icon name="send" /></button></div><div className="quick-prompts"><span>试试</span><button onClick={() => void send('下周三号楼砌体会缺什么？')}>下周三号楼砌体会缺什么？</button><button onClick={() => void send('查看待审批采购申请')}>查看待审批采购申请</button><button onClick={() => void send('追溯砌块批次')}>追溯砌块批次</button></div></div></section><aside className="assistant-side"><section className="panel agent-status-panel"><p className="panel-kicker">RUN STATUS</p><h2>当前线程</h2><div className="run-status"><span className="run-number">{threadId.current}</span><StatusPill tone="status-success">可查询</StatusPill></div><dl className="detail-list"><div><dt>数据范围</dt><dd>P001 受控业务数据</dd></div><div><dt>工具上限</dt><dd>8 次 / 请求</dd></div><div><dt>写入规则</dt><dd>草稿后需人工确认</dd></div></dl><div className="agent-note"><Icon name="check" /><span>刷新后会从数据库恢复当前用户的线程；清空只隐藏会话视图，审计记录仍保留。</span></div></section><section className="panel tool-list-panel"><p className="panel-kicker">AVAILABLE TOOLS</p><h2>可用工具</h2><div className="tool-list"><span><i className="tool-dot read" />库存查询 <small>只读</small></span><span><i className="tool-dot calc" />缺料计算 <small>确定性</small></span><span><i className="tool-dot draft" />采购草稿 <small>需确认</small></span><span><i className="tool-dot trace" />批次追溯 <small>只读</small></span></div></section></aside></div>
      {runId && <AiRunModal runId={runId} onClose={() => setRunId(null)} />}
    </>
  )
}

function ClaimBlock({ title, tone, items }: { title: string; tone: string; items: string[] }) {
  return <div className={`claim-block claim-${tone}`}><span>{title}</span>{items.map((item) => <p key={item}>{item}</p>)}</div>
}

function AiRunModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [run, setRun] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    void api.getAiRun(runId).then((result) => { if (active) setRun(result) }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '无法读取 AI 运行记录') })
    return () => { active = false }
  }, [runId])
  return <div className="modal-backdrop" role="presentation" onClick={onClose}><section className="confirm-modal business-record-modal" role="dialog" aria-modal="true" aria-labelledby="ai-run-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name="bot" /></div><button className="icon-button" title="关闭运行记录" onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">AI RUN AUDIT</p><h2 id="ai-run-title">运行记录</h2>{error ? <div className="modal-warning"><Icon name="alert" /><span>{error}</span></div> : !run ? <div className="business-record-loading"><i /><span>正在读取运行记录…</span></div> : <><div className="business-record-summary"><div><span>run_id</span><strong>{textValue(run.id, runId)}</strong></div><div><span>意图</span><strong>{textValue(run.intent)}</strong></div><div><span>状态</span><StatusPill>{textValue(run.status)}</StatusPill></div><div><span>模型</span><strong>{textValue(run.model_name)}</strong></div></div><div className="business-record-section"><span>工具调用</span>{Array.isArray(run.tool_calls) && run.tool_calls.length ? <div className="business-record-items">{(run.tool_calls as Array<Record<string, unknown>>).map((call) => <div key={textValue(call.id)}><strong>{textValue(call.tool_name)}</strong><span>{textValue(call.status)} · {textValue(call.permission_decision)}</span></div>)}</div> : <p>本次运行没有工具调用记录。</p>}</div><div className="business-record-section"><span>输入摘要</span><code>{textValue(run.input_hash)}</code></div></>}<div className="modal-actions"><button className="button button-primary" onClick={onClose}><Icon name="check" />完成查看</button></div></section></div>
}

function UserProfileModal({ role, chainOnline, onClose }: { role: Role; chainOnline: boolean | null; onClose: () => void }) {
  return <div className="modal-backdrop" role="presentation" onClick={onClose}><section className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="profile-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name="bot" /></div><button className="icon-button" title="关闭用户信息" onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">SESSION CONTEXT</p><h2 id="profile-title">当前用户</h2><div className="business-record-summary"><div><span>账号</span><strong>{demoUsers[role]}</strong></div><div><span>角色</span><strong>{roleLabels[role]}</strong></div><div><span>项目</span><strong>P001 · 海之子示范项目</strong></div><div><span>证据适配器</span><strong>{chainOnline ? 'MockEvidenceAdapter 在线' : chainOnline === false ? '不可用' : '检查中'}</strong></div></div><p className="modal-copy">角色和组织范围由服务端会话决定；前端视角切换会重新读取授权数据。</p><div className="modal-actions"><button className="button button-primary" onClick={onClose}><Icon name="check" />完成查看</button></div></section></div>
}


function ReportSection({ title, count, tone, children }: { title: string; count: string; tone: string; children: ReactNode }) {
  return <article className={`report-section report-${tone}`}><div className="report-section-head"><span className="report-section-mark" /><h3>{title}</h3><span>{count}</span></div><div className="report-section-body">{children}</div></article>
}

function reportSourceReference(section: string, row: Record<string, unknown>) {
  const explicit = textValue(row.source_ref)
  if (explicit) return explicit
  if (section === 'shortage_risks') return textValue(row.calculation_id, 'shortage:missing')
  if (section === 'ppe_coverage') return textValue(row.calculation_id, 'ppe:missing')
  if (section === 'anomalies') return `anomaly:${textValue(row.id, 'unknown')}`
  if (section === 'todo') return textValue(row.source_ref, `todo:${textValue(row.id, 'unknown')}`)
  return `${section}:${textValue(row.id, 'unknown')}`
}

function reportSourceLabel(section: string, row: Record<string, unknown>) {
  if (section === 'arrivals') return `${textValue(row.delivery_no, textValue(row.shipment_no, '到货记录'))} · ${textValue(row.warehouse_name, textValue(row.warehouse_code, '仓库'))}`
  if (section === 'consumption' || section === 'issues') return `${textValue(row.move_no, '库存流水')} · ${textValue(row.material_name, textValue(row.material_id, '物料'))} ${textValue(row.qty, '0')} ${textValue(row.unit)}`
  if (section === 'shortage_risks') return `${textValue(row.material_name, textValue(row.material_id, '物料'))} · 缺口 ${textValue(row.shortage_qty, '0')} ${textValue(row.unit)} · ${textValue(row.shortage_date, '待确认')}`
  if (section === 'ppe_coverage') return `${textValue(row.crew_name, textValue(row.crew_id, '班组'))} · 缺口 ${textValue(row.shortage_qty, '0')} ${textValue(row.unit)}`
  if (section === 'anomalies') return `${textValue(row.material_name, textValue(row.material_id, '物料'))} · ${textValue(row.type, '异常')} · ${textValue(row.status, 'OPEN')}`
  if (section === 'todo') return `${textValue(row.title, '待办')} · ${textValue(row.owner_role, '未分配')} · ${textValue(row.due_at, '待安排')}`
  return JSON.stringify(row)
}

function ReportSourceList({ section, rows, onOpen }: { section: string; rows: Record<string, unknown>[]; onOpen: (section: string, row: Record<string, unknown>) => void }) {
  if (!rows.length) return <p>该栏目没有可引用的记录。</p>
  return <div className="report-source-list">{rows.map((row, index) => {
    const reference = reportSourceReference(section, row)
    return <button className="report-source-row" key={`${reference}-${index}`} onClick={() => onOpen(section, row)} title={`打开来源 ${reference}`}><span>{reportSourceLabel(section, row)}</span><small>{reference}</small><Icon name="external" /></button>
  })}</div>
}

function ReportSourceModal({ source, onClose }: { source: { section: string; reference: string; row: Record<string, unknown> }; onClose: () => void }) {
  return <div className="modal-backdrop" role="presentation" onClick={onClose}><section className="confirm-modal business-record-modal" role="dialog" aria-modal="true" aria-labelledby="report-source-title" onClick={(event) => event.stopPropagation()}><div className="business-record-head"><div className="modal-icon"><Icon name="link" /></div><button className="icon-button" title="关闭来源详情" onClick={onClose}><Icon name="close" /></button></div><p className="panel-kicker">REPORT SOURCE</p><h2 id="report-source-title">{reportSourceLabel(source.section, source.row)}</h2><div className="business-record-section"><span>可追溯引用</span><code>{source.reference}</code></div><div className="business-record-section"><span>原始计算 / 业务快照</span><pre className="report-source-json">{JSON.stringify(source.row, null, 2)}</pre></div><div className="modal-actions"><button className="button button-primary" onClick={onClose}><Icon name="check" />完成查看</button></div></section></div>
}


function DynamicDailyPage({
  todos: todoList,
  onToast,
  onTodoUpdate,
  reportDate,
  onReportDateChange,
  draft,
  onDraftChange,
  canPublish,
  onOpenReference,
}: {
  todos: Todo[]
  onToast: (message: string) => void
  onTodoUpdate: (todo: Todo) => void
  reportDate: string
  onReportDateChange: (date: string) => void
  draft: Record<string, unknown> | null
  onDraftChange: (draft: Record<string, unknown> | null) => void
  canPublish: boolean
  onOpenReference: (reference: string) => void
}) {
  const [reports, setReports] = useState<DailyReportView[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selectedSource, setSelectedSource] = useState<{ section: string; reference: string; row: Record<string, unknown> } | null>(null)
  const draftPayload = draft?.payload && typeof draft.payload === 'object' ? draft.payload as Record<string, unknown> : null
  const activeDraft = draft && textValue(draftPayload?.date) === reportDate ? draft : null
  const loadReports = async () => {
    setLoading(true)
    try {
      const [values, pendingDrafts] = await Promise.all([
        api.getDailyReports(`${reportDate.slice(0, 7)}-01`, `${reportDate.slice(0, 7)}-31`),
        api.getDrafts('DAILY_REPORT'),
      ])
      setReports(values)
      const pending = pendingDrafts.find((item) => textValue(item.status) === 'PENDING_CONFIRMATION' && textValue((item.payload as Record<string, unknown> | undefined)?.date) === reportDate)
      if (pending?.id) {
        const token = canPublish ? await api.getDraftConfirmationToken(String(pending.id)) : {}
        onDraftChange({ ...pending, draft_id: String(pending.id), payload_hash: textValue(pending.payload_hash), confirmation_token: textValue(token.confirmation_token) })
      } else if (draft && textValue(draftPayload?.date) === reportDate) {
        onDraftChange(null)
      }
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取日报')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void loadReports() }, [reportDate, canPublish])
  const generate = async () => {
    setLoading(true)
    try {
      const value = await api.createDailyReportDraft(reportDate)
      onDraftChange(value)
      onToast('日报草稿已从已确认业务记录生成')
    } catch (reason) {
      onToast(reason instanceof Error ? `日报草稿生成失败：${reason.message}` : '日报草稿生成失败')
    } finally {
      setLoading(false)
    }
  }
  const publish = async () => {
    if (!activeDraft || typeof activeDraft.draft_id !== 'string' || typeof activeDraft.confirmation_token !== 'string') return
    setLoading(true)
    try {
      await api.publishDailyReport(activeDraft.draft_id, activeDraft.confirmation_token)
      onDraftChange(null)
      await loadReports()
      onToast('日报已发布，后续修订将生成新版本')
    } catch (reason) {
      onToast(reason instanceof Error ? `日报发布失败：${reason.message}` : '日报发布失败')
    } finally {
      setLoading(false)
    }
  }
  const discard = async () => {
    if (!activeDraft || typeof activeDraft.draft_id !== 'string') return
    setLoading(true)
    try {
      await api.rejectDraft(activeDraft.draft_id, '用户放弃日报草稿')
      onDraftChange(null)
      onToast('日报草稿已标记为 REJECTED')
    } catch (reason) {
      onToast(reason instanceof Error ? `放弃日报草稿失败：${reason.message}` : '放弃日报草稿失败')
    } finally {
      setLoading(false)
    }
  }
  const latest = reports.find((report) => report.reportDate === reportDate)
  const payload = latest?.payload
  const sectionRows = (key: string) => {
    const value = payload?.[key]
    if (!Array.isArray(value)) return []
    return value.map((item) => item && typeof item === 'object' ? item as Record<string, unknown> : { source_ref: String(item) })
  }
  const openReportSource = (section: string, row: Record<string, unknown>) => {
    const reference = reportSourceReference(section, row)
    const target = sourceTarget(reference)
    if (target && target !== 'daily') {
      onOpenReference(reference)
      return
    }
    setSelectedSource({ section, reference, row })
  }
  return <>
    <PageHeading title="现场日报" description="日报只引用已确认业务记录和带版本的计算结果；发布后不可覆盖" />
    <section className="report-toolbar"><div className="date-picker"><label htmlFor="report-date">报告日期</label><input id="report-date" type="date" value={reportDate} onChange={(event) => onReportDateChange(event.target.value)} /></div><StatusPill tone={latest ? 'status-success' : 'status-warning'}><Icon name={latest ? 'check' : 'clock'} />{latest ? `已发布 v${latest.version}` : '暂无已发布版本'}</StatusPill><span className="toolbar-spacer" /><button className="button button-quiet" disabled={!latest} onClick={() => latest && triggerJsonDownload(`daily-report-${latest.reportDate}-v${latest.version}.json`, latest.payload)}><Icon name="download" />下载 JSON</button><button className="button button-primary" disabled={loading} onClick={() => void generate()}><Icon name="document" />生成草稿</button></section>
    {error && <div className="data-error" role="alert"><Icon name="alert" /><span>{error}</span><button onClick={() => void loadReports()}>重试</button></div>}
    {activeDraft && <section className="panel draft-confirmation-panel"><div><p className="panel-kicker">DRAFT · PENDING_CONFIRMATION</p><h2>日报草稿已生成</h2><p>草稿 ID <code>{String(activeDraft.draft_id)}</code> · Payload hash <code>{String(activeDraft.payload_hash)}</code></p><small>一次性确认 token 已绑定当前用户和草稿版本。</small></div><div className="detail-actions"><button className="button button-quiet" disabled={loading} onClick={() => void discard()}>放弃草稿</button><button className="button button-primary" disabled={loading || !canPublish} title={canPublish ? '使用确认 token 发布日报' : '只有项目负责人可以发布日报'} onClick={() => void publish()}><Icon name="check" />确认发布</button></div></section>}
    <div className="report-grid"><section className="panel report-main"><div className="report-title-row"><div><p className="panel-kicker">DAILY REPORT · {latest ? `V${latest.version}` : 'EMPTY'}</p><h2>{latest ? `海之子示范项目 · ${latest.reportDate}` : '尚未生成日报'}</h2><p>{latest ? `发布于 ${formatDateTime(latest.publishedAt ?? '')}` : '选择日期并生成草稿，系统会读取真实业务记录。'}</p></div>{latest && <span className="version-badge">v{latest.version} · 已发布</span>}</div>{latest ? <div className="report-sections">
      <ReportSection title="到货" count={`${sectionRows('arrivals').length} 条`} tone="teal"><ReportSourceList section="arrivals" rows={sectionRows('arrivals')} onOpen={openReportSource} /><small>FACT · deliveries</small></ReportSection>
      <ReportSection title="领用与消耗" count={`${sectionRows('consumption').length} 条`} tone="blue"><ReportSourceList section="consumption" rows={sectionRows('consumption')} onOpen={openReportSource} /><small>FACT · stock_moves</small></ReportSection>
      <ReportSection title="缺料风险" count={`${sectionRows('shortage_risks').length} 项`} tone="orange"><ReportSourceList section="shortage_risks" rows={sectionRows('shortage_risks')} onOpen={openReportSource} /><small>CALCULATION · {textValue(payload?.shortage_calculation_id, 'calculation_id')}</small></ReportSection>
      <ReportSection title="PPE 覆盖" count={`${sectionRows('ppe_coverage').length} 项`} tone="purple"><ReportSourceList section="ppe_coverage" rows={sectionRows('ppe_coverage')} onOpen={openReportSource} /><small>CALCULATION · {textValue(payload?.ppe_calculation_id, 'ppe-v1')}</small></ReportSection>
      <ReportSection title="异常线索" count={`${sectionRows('anomalies').length} 条`} tone="red"><ReportSourceList section="anomalies" rows={sectionRows('anomalies')} onOpen={openReportSource} /><small>FACT · anomalies</small></ReportSection>
    </div> : <EmptyState text="没有该日期的日报记录" />}</section><aside className="panel report-side"><p className="panel-kicker">NEXT ACTIONS</p><h2>项目待办</h2><div className="todo-list compact">{todoList.slice(0, 5).map((todo) => <div className="todo-row" key={todo.id}><button className={`todo-check ${todo.status === '已完成' ? 'checked' : ''}`} title="更新待办状态" onClick={() => onTodoUpdate(todo)}><Icon name="check" /></button><div className="todo-copy"><strong>{todo.title}</strong><small>{todo.owner} · {todo.due}</small></div><span className={`priority priority-${todo.priority === '高' ? 'high' : todo.priority === '中' ? 'medium' : 'low'}`}>{todo.priority}</span></div>)}{todoList.length === 0 && <EmptyState text="暂无待办记录" />}</div>{latest && <div className="report-source-summary"><span>报告内待办</span><ReportSourceList section="todo" rows={sectionRows('todo')} onOpen={openReportSource} /></div>}<div className="report-integrity"><Icon name="lock" /><div><strong>版本不可覆盖</strong><p>修订会生成新版本并保留 supersedes_report_id。</p></div></div></aside></div>
    {selectedSource && <ReportSourceModal source={selectedSource} onClose={() => setSelectedSource(null)} />}
  </>
}

function DynamicAnomaliesPage({ list, onToast, onRefresh, canManage }: { list: Anomaly[]; onToast: (message: string) => void; onRefresh: () => Promise<void>; canManage: boolean }) {
  const [status, setStatus] = useState('全部状态')
  const [level, setLevel] = useState('全部级别')
  const [working, setWorking] = useState(false)
  const [resolveId, setResolveId] = useState<string | null>(null)
  const [resolutionCode, setResolutionCode] = useState('')
  const [resolutionNote, setResolutionNote] = useState('')
  const filtered = list.filter((item) => status === '全部状态' || item.status === status).filter((item) => level === '全部级别' || item.level === level)
  const scan = async () => {
    setWorking(true)
    try { await api.scanAnomalies(projectDateDaysAgo(14), projectToday()); await onRefresh(); onToast('异常扫描完成，结果已按 dedupe_key 更新') } catch (reason) { onToast(reason instanceof Error ? `异常扫描失败：${reason.message}` : '异常扫描失败') } finally { setWorking(false) }
  }
  const acknowledge = async (id: string) => {
    setWorking(true)
    try { await api.acknowledgeAnomaly(id, '已由项目负责人确认，进入复核流程'); await onRefresh(); onToast('异常已更新为 ACKNOWLEDGED') } catch (reason) { onToast(reason instanceof Error ? `异常更新失败：${reason.message}` : '异常更新失败') } finally { setWorking(false) }
  }
  const resolve = async () => {
    if (!resolveId || !resolutionCode.trim() || !resolutionNote.trim()) return
    setWorking(true)
    try { await api.resolveAnomaly(resolveId, resolutionCode, resolutionNote); setResolveId(null); setResolutionCode(''); setResolutionNote(''); await onRefresh(); onToast('异常已更新为 RESOLVED') } catch (reason) { onToast(reason instanceof Error ? `异常关闭失败：${reason.message}` : '异常关闭失败') } finally { setWorking(false) }
  }
  return <>
    <PageHeading title="异常中心" description="扫描结果只提供复核线索，不做责任归因；所有状态变更写入审计日志" action={working ? '处理中…' : '运行异常扫描'} actionIcon="refresh" onAction={() => void scan()} />
    <section className="anomaly-summary"><div className="anomaly-summary-item"><span className="summary-icon red"><Icon name="alert" /></span><div><strong>{list.filter((item) => item.level === '高' && item.status !== '已解决').length}</strong><small>高风险待复核</small></div></div><div className="anomaly-summary-item"><span className="summary-icon orange"><Icon name="clock" /></span><div><strong>{list.filter((item) => item.status === '待确认').length}</strong><small>待确认</small></div></div><div className="anomaly-summary-item"><span className="summary-icon green"><Icon name="check" /></span><div><strong>{list.filter((item) => item.status === '已解决').length}</strong><small>已解决</small></div></div><div className="anomaly-summary-note"><Icon name="lock" /><span>原始流水和计划不会被扫描任务修改</span></div></section>
    <section className="toolbar"><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="全部状态">全部状态</option><option value="待确认">待确认</option><option value="已知悉">已知悉</option><option value="已解决">已解决</option></select><select value={level} onChange={(event) => setLevel(event.target.value)}><option value="全部级别">全部级别</option><option value="高">高</option><option value="中">中</option><option value="低">低</option></select><span className="toolbar-spacer" /><span className="muted">共 {filtered.length} 条</span></section>
    <section className="panel anomaly-panel"><div className="table-scroll"><table><thead><tr><th>异常线索</th><th>物料 / 区域</th><th>可能原因</th><th>状态</th><th>来源与动作</th></tr></thead><tbody>{filtered.map((item) => <tr key={item.id}><td><div className="anomaly-title"><span className={`risk-badge ${riskClass(item.level)}`}>{item.level}</span><strong>{item.title}</strong></div></td><td><strong>{item.material}</strong><small>{item.area}</small></td><td><span className="possible-cause">{item.possibleCause}</span><small>仅供复核，不代表责任归因</small></td><td><StatusPill>{item.status}</StatusPill></td><td><div className="anomaly-actions">{item.status === '待确认' && <button className="small-button" disabled={working || !canManage} title={canManage ? '标记异常已知悉' : '只有项目负责人可以确认异常'} onClick={() => void acknowledge(item.id)}>标记已知悉</button>}{item.status === '已知悉' && <button className="small-button" disabled={working || !canManage} title={canManage ? '填写异常解决说明' : '只有项目负责人可以关闭异常'} onClick={() => setResolveId(item.id)}>填写解决说明</button>}<details><summary>查看原始线索</summary><code>{item.sourceRef ?? item.id}</code><pre>{JSON.stringify(item.payload ?? {}, null, 2)}</pre></details></div></td></tr>)}</tbody></table>{filtered.length === 0 && <EmptyState text="暂无符合筛选条件的异常记录" />}</div></section>
    {resolveId && <div className="modal-backdrop" role="presentation" onClick={() => !working && setResolveId(null)}><section className="confirm-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}><div className="modal-icon"><Icon name="check" /></div><p className="panel-kicker">RESOLVE ANOMALY</p><h2>填写解决说明</h2><label className="form-field"><span>resolution_code</span><input value={resolutionCode} onChange={(event) => setResolutionCode(event.target.value)} placeholder="例如 PLAN_SYNCED" /></label><label className="form-field"><span>说明</span><textarea value={resolutionNote} onChange={(event) => setResolutionNote(event.target.value)} placeholder="记录复核结论和依据" /></label><div className="modal-actions"><button className="button button-quiet" onClick={() => setResolveId(null)}>取消</button><button className="button button-primary" disabled={working || !resolutionCode.trim() || !resolutionNote.trim()} onClick={() => void resolve()}><Icon name="check" />确认关闭</button></div></section></div>}
  </>
}

function App() {
  const appRoot = useRef<HTMLDivElement>(null)
  const [locale, setLocale] = useState<AppLocale>(getActiveLocale())
  const [page, setPage] = useState<PageId>('overview')
  const [role, setRole] = useState<Role>('PROJECT_OWNER')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [stock, setStock] = useState<StockItem[]>(isMockMode ? seedStock : [])
  const [shortage, setShortage] = useState<ShortageItem[]>(isMockMode ? seedShortage : [])
  const [drafts, setDrafts] = useState<PurchaseDraft[]>(isMockMode ? seedDrafts : [])
  const [traces, setTraces] = useState<TraceEvent[]>(isMockMode ? seedTraces : [])
  const [batches, setBatches] = useState<BatchOption[]>([])
  const [todoList, setTodoList] = useState<Todo[]>(isMockMode ? todos : [])
  const [anomalyList, setAnomalyList] = useState<Anomaly[]>(isMockMode ? seedAnomalies : [])
  const [stockMoves, setStockMoves] = useState<StockMoveView[]>([])
  const [consumption, setConsumption] = useState<ConsumptionView[]>([])
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrderView[]>([])
  const [documents, setDocuments] = useState<DocumentView[]>([])
  const [shortageMeta, setShortageMeta] = useState<ShortageMeta | null>(null)
  const [dataError, setDataError] = useState('')
  const [loadingOverview, setLoadingOverview] = useState(true)
  const [lastSync, setLastSync] = useState('—')
  const [chainOnline, setChainOnline] = useState<boolean | null>(null)
  const [profileOpen, setProfileOpen] = useState(false)
  const [toast, setToast] = useState('')
  const [confirmDraft, setConfirmDraft] = useState<PurchaseDraft | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [traceFocus, setTraceFocus] = useState<string | null>(null)
  const [dailyReportDate, setDailyReportDate] = useState('2026-08-26')
  const [dailyReportDraft, setDailyReportDraft] = useState<Record<string, unknown> | null>(null)
  const [inventoryFocusMaterialId, setInventoryFocusMaterialId] = useState<string | null>(null)
  const overviewRequest = useRef(0)

  useEffect(() => subscribeLocale(setLocale), [])
  useLegacyCopyProjection(appRoot, locale)
  useEffect(() => {
    document.title = locale === 'en' ? 'Zhufu Chain · Sea Child Demonstration Project' : '築福鏈 · 海之子示範專案'
  }, [locale])

  const loadOverview = useCallback(async () => {
    const requestNumber = ++overviewRequest.current
    setLoadingOverview(true)
    try {
      const data = await api.getOverview()
      if (requestNumber !== overviewRequest.current) return
      setStock(data.stock)
      setShortage(data.shortage)
      setDrafts(data.drafts)
      setTraces(data.traces)
      setBatches(data.batches)
      setTodoList(data.todos)
      setAnomalyList(data.anomalies)
      setStockMoves(data.stockMoves)
      setConsumption(data.consumption)
      setPurchaseOrders(data.purchaseOrders)
      setDocuments(data.documents)
      setShortageMeta(data.shortageMeta)
      setLastSync(formatLocalizedDateTime(new Date(), locale, { hour: '2-digit', minute: '2-digit' }))
      setDataError('')
    } catch (error: unknown) {
      if (requestNumber === overviewRequest.current) setDataError(error instanceof Error ? error.message : '无法读取项目数据')
      throw error
    } finally {
      if (requestNumber === overviewRequest.current) setLoadingOverview(false)
    }
  }, [locale])

  useEffect(() => {
    setDemoUser(demoUsers[role])
    setDailyReportDraft(null)
    setInventoryFocusMaterialId(null)
    if (!isMockMode) {
      setStock([])
      setShortage([])
      setDrafts([])
      setTraces([])
      setBatches([])
      setTodoList([])
      setAnomalyList([])
      setStockMoves([])
      setConsumption([])
      setPurchaseOrders([])
      setDocuments([])
      setShortageMeta(null)
    }
    void loadOverview().catch(() => undefined)
  }, [role, loadOverview])

  useEffect(() => {
    let active = true
    void api.health().then((health) => { if (active) setChainOnline(Boolean(health.ok)) }).catch(() => { if (active) setChainOnline(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 3400)
    return () => window.clearTimeout(timer)
  }, [toast])

  const notify = (message: string) => setToast(message)
  const navigate = (nextPage: PageId) => {
    setPage(nextPage)
    setMobileNavOpen(false)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const navigateToTraceEvent = (eventId: string) => {
    setTraceFocus(eventId)
    navigate('trace')
  }
  const navigateToInventoryMaterial = (materialId: string) => {
    setInventoryFocusMaterialId(materialId)
    navigate('inventory')
  }
  const openReportReference = (reference: string) => {
    const recordReference = reference.split(':').slice(1).join(':')
    const trace = traces.find((event) => event.reference === recordReference)
    if (trace) {
      navigateToTraceEvent(trace.id)
      return
    }
    const target = sourceTarget(reference)
    if (target && target !== 'daily') navigate(target)
  }
  const confirmPurchaseDraft = async (draft: PurchaseDraft) => {
    setConfirming(true)
    try {
      if (!draft.draftId) throw new Error('该记录不是可确认草稿')
      const token = draft.confirmationToken ?? textValue((await api.getDraftConfirmationToken(draft.draftId)).confirmation_token)
      if (!token) throw new Error('服务端没有返回确认 token')
      await api.submitPurchaseDraft(draft.draftId, token)
      await loadOverview()
      setConfirmDraft(null)
      notify('采购申请已提交，状态变更为 PENDING_APPROVAL')
    } catch (error) {
      notify(error instanceof Error ? `无法提交采购申请：${error.message}` : '无法提交采购申请')
    } finally {
      setConfirming(false)
    }
  }
  const createPurchaseDraft = async (item: ShortageItem) => {
    try {
      const calculation = {
        project_id: 'P001',
        calculation_id: shortageMeta?.calculationId ?? item.calcId,
        items: shortage.map((riskItem) => ({ material_id: riskItem.id, recommended_order_qty: riskItem.orderQty, unit: riskItem.unitCode ?? 'piece' })),
      }
      const stockItem = stock.find((candidate) => candidate.materialId === item.id)
      const draft = await api.createPurchaseDraft(calculation, [{ material_id: item.id, qty: item.orderQty, unit: item.unitCode ?? 'piece', warehouse_id: stockItem?.warehouseId, reason: '由确定性缺料计算生成' }], item.riskDate)
      setDrafts((current) => [draft, ...current])
      setConfirmDraft(draft)
      navigate('procurement')
    } catch (error) {
      notify(error instanceof Error ? `无法生成采购草稿：${error.message}` : '无法生成采购草稿')
    }
  }
  const updateTodo = async (todo: Todo) => {
    try {
      const updated = await api.updateTodo(todo.id, { status: todo.status === '已完成' ? 'OPEN' : 'DONE' })
      setTodoList((current) => current.map((item) => item.id === todo.id ? updated : item))
      notify('待办状态已写入数据库')
    } catch (error) {
      notify(error instanceof Error ? `待办更新失败：${error.message}` : '待办更新失败')
    }
  }
  const approvePurchaseRequest = async (requestId: string) => {
    try {
      const token = await api.getApprovalToken(requestId)
      const confirmationToken = textValue(token.confirmation_token)
      if (!confirmationToken) throw new Error('服务端没有返回审批 token')
      await api.approvePurchaseRequest(requestId, confirmationToken)
      await loadOverview()
      notify('采购申请已批准，PURCHASE_APPROVAL 证据事件已进入 outbox')
    } catch (error) {
      notify(error instanceof Error ? `审批失败：${error.message}` : '审批失败')
    }
  }
  const activeTitle = pageTitles[page]
  const renderPage = useMemo(() => {
    switch (page) {
      case 'inventory': return <InventoryPage stock={stock} onToast={notify} onNavigate={navigate} stockMoves={stockMoves} focusMaterialId={inventoryFocusMaterialId} canWrite={['PROJECT_OWNER', 'SITE_ADMIN', 'PROCUREMENT'].includes(role)} onRefresh={loadOverview} />
      case 'shortage': return <ShortagePage shortage={shortage} meta={shortageMeta} canDraft={['PROJECT_OWNER', 'PROCUREMENT', 'SITE_ADMIN'].includes(role)} onToast={notify} onNavigate={navigate} onRecalculated={(items, nextMeta) => { setShortage(items); setShortageMeta(nextMeta) }} onDraft={(item) => void createPurchaseDraft(item)} />
      case 'procurement': return <ProcurementPage drafts={drafts} orders={purchaseOrders} stock={stock} role={role} canSubmit={['PROJECT_OWNER', 'PROCUREMENT'].includes(role)} canApprove={role === 'PROJECT_OWNER'} onToast={notify} onConfirm={setConfirmDraft} onApprove={(requestId) => void approvePurchaseRequest(requestId)} onNavigate={navigate} onRefresh={loadOverview} />
      case 'trace': return <TracePage traces={traces} batches={batches} documents={documents} focusEventId={traceFocus} chainOnline={chainOnline} canAnchor={['PROJECT_OWNER', 'PROCUREMENT', 'SUPPLIER', 'SITE_ADMIN', 'INSPECTOR'].includes(role)} role={role} onTraceChange={setTraces} onRefresh={loadOverview} onToast={notify} />
      case 'assistant': return <AssistantPage role={role} onToast={notify} onNavigate={navigate} />
      case 'daily': return <DynamicDailyPage todos={todoList} onToast={notify} onTodoUpdate={updateTodo} reportDate={dailyReportDate} onReportDateChange={setDailyReportDate} draft={dailyReportDraft} onDraftChange={setDailyReportDraft} canPublish={role === 'PROJECT_OWNER'} onOpenReference={openReportReference} />
      case 'anomalies': return <DynamicAnomaliesPage list={anomalyList} onToast={notify} onRefresh={async () => { const fresh = await api.getAnomalies(); setAnomalyList(fresh) }} canManage={role === 'PROJECT_OWNER'} />
      default: return <OverviewPage stock={stock} shortage={shortage} drafts={drafts} traces={traces} todoList={todoList} anomalyList={anomalyList} onNavigate={navigate} onStockSelect={navigateToInventoryMaterial} onTraceSelect={navigateToTraceEvent} onToast={notify} stockMoves={stockMoves} consumption={consumption} onTodoUpdate={updateTodo} onRefreshShortage={() => { void api.calculateShortageDetailed().then((result) => { setShortage(result.items); setShortageMeta(result.meta); notify(`缺料计算已更新，共 ${result.items.length} 项风险`) }).catch((error: unknown) => notify(error instanceof Error ? `缺料计算失败：${error.message}` : '缺料计算失败')) }} onCreateDailyReport={() => { const reportDate = projectToday(); void api.createDailyReportDraft(reportDate).then((draft) => { setDailyReportDate(reportDate); setDailyReportDraft(draft); notify('日报草稿已生成，已打开待确认内容'); navigate('daily') }).catch((error: unknown) => notify(error instanceof Error ? `无法生成日报草稿：${error.message}` : '无法生成日报草稿')) }} />
    }
  }, [page, role, traceFocus, stock, shortage, drafts, traces, batches, todoList, anomalyList, stockMoves, consumption, purchaseOrders, documents, shortageMeta, loadOverview, dailyReportDate, dailyReportDraft, inventoryFocusMaterialId, chainOnline, locale])

  return (
    <div className="app-shell" ref={appRoot} lang={locale}>
      <aside className={`sidebar ${mobileNavOpen ? 'sidebar-open' : ''}`}>
        <div className="brand"><div className="brand-mark"><span /><span /><span /></div><div><strong>筑福链</strong><small>FIELD SUPPLY OS</small></div><button className="mobile-close icon-button" onClick={() => setMobileNavOpen(false)}><Icon name="close" /></button></div>
        <div className="project-switcher"><div className="project-avatar">海</div><div><span>当前项目</span><strong>海之子示范项目</strong><small>P001 · 进行中</small></div><Icon name="chevron" /></div>
        <nav className="main-nav">{navGroups.map((group) => <div className="nav-group" key={group.label}><p>{group.label}</p>{group.items.map((item) => <button className={`nav-item ${page === item.id ? 'active' : ''}`} key={item.id} onClick={() => navigate(item.id)}><Icon name={item.icon} /><span>{item.label}</span>{item.id === 'anomalies' && anomalyList.length > 0 && <b className="nav-count">{anomalyList.filter((item) => item.status !== '已解决').length}</b>}</button>)}</div>)}<div className="nav-group nav-assistant"><p>智能协作</p><button className={`nav-item ${page === 'assistant' ? 'active' : ''}`} onClick={() => navigate('assistant')}><Icon name="bot" /><span>AI 现场助手</span><i className="ai-spark" /></button></div></nav>
        <div className="sidebar-bottom"><div className="chain-status"><span className={`chain-status-dot ${chainOnline === false ? 'offline' : ''}`} /><div><strong>模拟链</strong><small>MockEvidenceAdapter · {chainOnline === null ? '检查中' : chainOnline ? '在线' : '离线'}</small></div><button className="icon-button" title="查看链服务状态" onClick={() => navigate('trace')}><Icon name="external" /></button></div><button className="sidebar-user" title="查看当前用户" onClick={() => setProfileOpen(true)}><span className="user-avatar">项</span><div><strong>{roleLabels[role]}</strong><small>{demoUsers[role]} · P001</small></div><Icon name="chevron" /></button></div>
      </aside>
      {mobileNavOpen && <button className="sidebar-overlay" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <main className="main-content"><header className="topbar"><button className="mobile-menu icon-button" onClick={() => setMobileNavOpen(true)}><Icon name="menu" /></button><div className="topbar-context"><span className="breadcrumb">海之子示范项目 <Icon name="chevron" /> {activeTitle.eyebrow}</span><strong>{activeTitle.title}</strong></div><div className="topbar-actions"><div className="sync-status"><i className={chainOnline === false ? 'offline' : ''} /><span>{chainOnline === false ? '连接异常' : loadingOverview ? '同步中' : '已同步'}</span><small>{lastSync}</small></div><span className="mock-pill"><span>⌘</span> 模拟链</span><label className="role-select"><span>视角</span><select value={role} onChange={(event) => { const nextRole = event.target.value as Role; setDemoUser(demoUsers[nextRole]); setRole(nextRole); setDataError(''); notify(`已切换为${roleLabels[nextRole]}视角`) }}>{Object.entries(roleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="locale-select"><span>语言</span><select aria-label="语言" value={locale} onChange={(event) => setActiveLocale(event.target.value as AppLocale)}>{localeOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label><button className="top-user" title="查看当前用户" onClick={() => setProfileOpen(true)}><span className="user-avatar">项</span><Icon name="chevron" /></button></div></header><div className="workspace">{isMockMode && <div className="mock-data-banner"><Icon name="alert" /><span>当前为显式演示数据模式，业务操作不会写入后端事实库。</span></div>}{dataError ? <div className="data-error" role="alert"><Icon name="alert" /><span>项目数据未加载：{dataError}</span><button disabled={loadingOverview} onClick={() => { setDataError(''); void loadOverview().catch(() => undefined) }}>{loadingOverview ? '加载中…' : '重新加载'}</button></div> : renderPage}</div><footer className="app-footer"><span>筑福链 · MVP 工作台</span><span>API /api/v1 · 默认时区 Asia/Shanghai · {isMockMode ? '显式演示数据模式' : '业务 API'}</span></footer></main>
      {toast && <div className="toast"><span className="toast-icon"><Icon name="check" /></span><span>{toast}</span><button onClick={() => setToast('')}><Icon name="close" /></button></div>}
      {profileOpen && <UserProfileModal role={role} chainOnline={chainOnline} onClose={() => setProfileOpen(false)} />}
      {confirmDraft && <div className="modal-backdrop" role="presentation" onClick={() => !confirming && setConfirmDraft(null)}><div className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title" onClick={(event) => event.stopPropagation()}><div className="modal-icon"><Icon name="document" /></div><p className="panel-kicker">CONFIRM ACTION</p><h2 id="confirm-title">提交采购申请？</h2><p className="modal-copy">确认后会创建正式采购申请，状态为 <b>PENDING_APPROVAL</b>。库存和订单不会在此动作中改变。</p><div className="modal-summary"><div><span>物料</span><strong>{confirmDraft.material}</strong></div><div><span>数量</span><strong>{confirmDraft.quantity} {confirmDraft.unit}</strong></div><div><span>需要日期</span><strong>{confirmDraft.neededBy}</strong></div><div><span>计算依据</span><strong>{confirmDraft.calcId}</strong></div></div><div className="modal-warning"><Icon name="clock" /><span>确认 token 有效期 30 分钟，提交后不可重复使用。</span></div><div className="modal-actions"><button className="button button-quiet" disabled={confirming} onClick={() => setConfirmDraft(null)}>取消</button><button className="button button-primary" disabled={confirming} onClick={() => void confirmPurchaseDraft(confirmDraft)}><Icon name="check" />{confirming ? '提交中…' : '确认并提交'}</button></div></div></div>}
    </div>
  )
}

export default App
