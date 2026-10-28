import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, AlertTriangle, RefreshCw } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

/**
 * Live 状态页 —— 对接后端 `app/custom/live_status.py`（/api/custom/live-status）。
 *
 * 定位：仪表盘 + **实盘口径编辑**（H64，2026-10-28 用户授权）。
 * 状态区仍然只读；写入口只有一块「实盘口径」，且保存后**次日 04:30 启动才生效**，
 * 写入前由后端做值域 + 可行性校验（按引擎冻结价算底仓，<100 股直接拒绝）。
 */

const BASE = '/api/custom/live-status'

interface ParamMeta {
  key: string
  label: string
  default: number | string | boolean | null
  kind: 'number' | 'slider' | 'checkbox' | 'select' | 'text'
  min?: number | null
  max?: number | null
  step?: number | null
  options?: string[] | null
  help?: string | null
  group?: string | null
  /** true = 由「资金基准 × 底仓比例」自动算出，界面只读 */
  derived?: boolean
}

interface Editable {
  path: string
  exists: boolean
  current: {
    initial_cash: number | null
    risk_pct: number
    risk: Record<string, number>
    positions: {
      code: string; strategy_id: string; enabled?: boolean
      param_overrides?: Record<string, unknown>
    }[]
    paper_start_date?: string | null
  }
  /** 引擎实际使用的起点（含兜底常量），面板必须显示这个而不是用户填的值 */
  paper_start_effective?: string
  risk_spec: Record<string, { min: number; max: number; label: string }>
  position_checks: {
    code: string; strategy_id: string; price: number | null
    base_position: number | null; ok: boolean; lota_pct: number | null
  }[]
  strategies: { id: string; label: string }[]
  /** 逐策略参数元数据（H66）：key/label/kind/min/max/options/group/derived */
  strategy_params?: Record<string, { name: string; param_meta: ParamMeta[] }>
  takes_effect: string
}

interface Heartbeat {
  date?: string
  started_at?: string | null
  status?: string
  last_success_at?: string | null
  collect_errors?: number
  match_errors?: number
  updated_at?: string | null
  breaker_state?: string
  breaker_opens?: number
  breaker_fails?: number
  rebuild_count?: number
  rebuild_rejected?: number
  login_threads_started?: number
  pending_login_alive?: boolean
}

interface Summary {
  engine_root: string
  heartbeat: Heartbeat | null
  heartbeat_age_seconds: number | null
  heartbeat_fresh: boolean
  collect: { last_collected: Record<string, unknown>; collect_errors: number | null }
  alert_active: boolean
  config_applied: {
    applied_at: string
    source: string
    combos: [string, string][]
    overrides: Record<string, Record<string, unknown>>
  } | null
}

interface UnitPosition {
  symbol: string
  quantity: number | null
  sellable_quantity: number | null
  average_cost: number | null
  market_price: number | null
  realized_pnl: number | null
}

interface Unit {
  symbol: string
  strategy: string
  summary_date: string | null
  is_today: boolean
  equity: number | null
  day_pnl: number | null
  cum_pnl: number | null
  cumulative_drawdown: number | null
  orders: number | null
  fills: number | null
  engine_consistency: number | null
  data_quality: { status?: string; bars?: number; expected?: number; usable_for_sample?: boolean }
  stopped: boolean | null
  defense: boolean | null
  positions: UnitPosition[]
  today_fills: { side?: string; quantity?: number; price?: number; filled_at?: string }[]
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtPnl(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const s = fmtNum(Math.abs(v), 0)
  if (v > 0) return `+¥${s}`
  if (v < 0) return `-¥${s}`
  return `¥${s}`
}

/** A 股惯例：盈为红、亏为绿。 */
function pnlTone(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return 'text-foreground'
  return v > 0 ? 'text-bull' : 'text-bear'
}

function Card({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('rounded-btn bg-base border border-border p-4', className)}>
      <div className="text-xs font-medium text-muted mb-3">{title}</div>
      {children}
    </div>
  )
}

function KV({ k, v, tone, mono }: { k: string; v: React.ReactNode; tone?: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-xs text-muted shrink-0">{k}</span>
      <span className={cn('text-xs text-right', mono && 'font-mono', tone ?? 'text-foreground')}>{v}</span>
    </div>
  )
}

function BreakerBadge({ state }: { state?: string }) {
  const s = state ?? 'unknown'
  const tone = s === 'open' ? 'bg-bear/15 text-bear' : s === 'half_open' ? 'bg-amber-500/15 text-amber-500' : 'text-muted bg-base'
  return <span className={cn('px-1.5 py-0.5 rounded text-[10px] font-mono font-medium', tone)}>{s}</span>
}

/** 逐策略参数编辑器（H66）：按 param_meta 的 group 分组渲染，只写入与默认值不同的项。 */
function ParamEditor({ meta, value, onChange }: {
  meta: ParamMeta[]
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}) {
  const GROUP_LABEL: Record<string, string> = { core: '核心', risk: '风控', cost: '成本' }
  const groups: Record<string, ParamMeta[]> = {}
  for (const m of meta) {
    const g = m.group || 'other'
    if (!groups[g]) groups[g] = []
    groups[g].push(m)
  }

  const setOne = (m: ParamMeta, raw: string | boolean) => {
    const next = { ...value }
    let v: unknown = raw
    if (m.kind === 'checkbox') {
      v = Boolean(raw)
    } else if (m.kind === 'number' || m.kind === 'slider') {
      const n = Number(raw)
      if (Number.isNaN(n)) return
      v = n
    }
    // 与默认值相同 ⇒ 不写入覆盖（配置保持精简，避免无意义的 diff 与审计噪音）
    if (v === m.default) delete next[m.key]
    else next[m.key] = v
    onChange(next)
  }

  if (!meta.length) {
    return <div className="text-[11px] text-muted">读不到该策略的参数元数据（quant_app 侧不可达）。</div>
  }

  return (
    <div className="space-y-2 border-t border-border/50 pt-2">
      {Object.entries(groups).map(([g, items]) => (
        <div key={g} className="space-y-1">
          <div className="text-[10px] uppercase tracking-wide text-muted">{GROUP_LABEL[g] ?? g}</div>
          <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
            {items.map(m => {
              const cur = value[m.key] ?? m.default
              return (
                <label key={m.key} className="flex items-center justify-between gap-2 text-[11px]">
                  <span className="text-muted" title={m.help ?? ''}>
                    {m.label}
                    {m.derived && <span className="ml-1 text-[9px] text-amber-500">自动</span>}
                  </span>
                  {m.derived ? (
                    <span className="font-mono text-[11px] text-muted/70">按资金算出</span>
                  ) : m.kind === 'checkbox' ? (
                    <input
                      type="checkbox"
                      checked={Boolean(cur)}
                      onChange={e => setOne(m, e.target.checked)}
                      className="accent-[hsl(var(--accent))]"
                    />
                  ) : m.kind === 'select' ? (
                    <select
                      value={String(cur ?? '')}
                      onChange={e => setOne(m, e.target.value)}
                      className="w-32 rounded-btn border border-border bg-base px-1.5 py-0.5 text-[11px]"
                    >
                      {(m.options ?? []).map(o => <option key={o} value={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input
                      type="number"
                      value={String(cur ?? '')}
                      min={m.min ?? undefined}
                      max={m.max ?? undefined}
                      step={m.step ?? 'any'}
                      onChange={e => setOne(m, e.target.value)}
                      className="w-24 rounded-btn border border-border bg-base px-1.5 py-0.5 text-right font-mono text-[11px]"
                    />
                  )}
                </label>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

function LiveStatusPage() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [units, setUnits] = useState<Unit[]>([])
  const [editable, setEditable] = useState<Editable | null>(null)
  const [form, setForm] = useState({ cash: '', riskPct: '', capPct: '', orderPct: '', startDate: '' })
  const [rows, setRows] = useState<{
    code: string; strategy_id: string; enabled: boolean
    param_overrides: Record<string, unknown>
  }[]>([])
  /** 展开参数编辑区的行索引（默认收起，避免 52 个参数一次铺满） */
  const [openParams, setOpenParams] = useState<Record<number, boolean>>({})
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [saveErrs, setSaveErrs] = useState<string[]>([])
  const touchedRef = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<string>('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [s, u, c] = await Promise.all([
        fetch(`${BASE}/summary`).then(r => { if (!r.ok) throw new Error(`summary ${r.status}`); return r.json() as Promise<Summary> }),
        fetch(`${BASE}/units`).then(r => { if (!r.ok) throw new Error(`units ${r.status}`); return r.json() as Promise<{ units: Unit[] }> }),
        fetch(`${BASE}/config`).then(r => { if (!r.ok) throw new Error(`config ${r.status}`); return r.json() as Promise<{ editable: Editable }> }),
      ])
      setSummary(s)
      setUnits(u.units)
      setEditable(c.editable)
      // 用户正在编辑时不要被 30 秒轮询覆盖（用 ref 判断，避免在 setState 里再 setState）
      if (!touchedRef.current) {
        setForm({
          cash: String(c.editable?.current?.initial_cash ?? ''),
          riskPct: ((c.editable?.current?.risk_pct ?? 0.1) * 100).toFixed(1),
          capPct: ((c.editable?.current?.risk?.max_symbol_position_pct ?? 0.2) * 100).toFixed(1),
          orderPct: ((c.editable?.current?.risk?.max_order_value_pct ?? 0.1) * 100).toFixed(1),
          startDate: String(c.editable?.current?.paper_start_date ?? ''),
        })
        setRows((c.editable?.current?.positions ?? []).map(p => ({
          code: p.code, strategy_id: p.strategy_id, enabled: p.enabled !== false,
          param_overrides: (p.param_overrides ?? {}) as Record<string, unknown>,
        })))
      }
      setError(null)
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  const save = useCallback(async () => {
    setSaving(true); setSaveMsg(null); setSaveErrs([])
    try {
      const body = {
        initial_cash: Number(form.cash),
        paper_start_date: form.startDate.trim() || null,
        risk_pct: Number(form.riskPct) / 100,
        risk: {
          max_symbol_position_pct: Number(form.capPct) / 100,
          max_order_value_pct: Number(form.orderPct) / 100,
        },
        positions: rows.map(r => ({
          code: r.code.trim(), strategy_id: r.strategy_id,
          enabled: r.enabled, param_overrides: r.param_overrides, note: '',
        })),
      }
      const r = await fetch(`${BASE}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const d = await r.json() as { ok: boolean; errors?: string[]; takes_effect?: string; warning?: string }
      if (d.ok) {
        setSaveMsg(`已保存 · ${d.takes_effect ?? '次日启动生效'}${d.warning ? ` · ⚠️ ${d.warning}` : ''}`)
        touchedRef.current = false
        load()
      } else {
        setSaveErrs(d.errors ?? ['保存失败（后端未返回原因）'])
      }
    } catch (err) {
      setSaveErrs([err instanceof Error ? err.message : String(err)])
    } finally {
      setSaving(false)
    }
  }, [form, rows, load])

  useEffect(() => {
    load()
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [load])

  const hb = summary?.heartbeat ?? null
  const fresh = summary?.heartbeat_fresh ?? false
  const overrides = summary?.config_applied?.overrides ?? {}
  const hasOverrides = Object.values(overrides).some(o => Object.keys(o).length > 0)

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 p-4 pb-10">
      <PageHeader
        title="Live 状态"
        subtitle="实盘引擎仪表盘 · 实盘口径可调（次日生效）"
      />

      <div className="flex items-center justify-between text-xs text-muted">
        <span>每 30 秒自动刷新{updatedAt && ` · 刷新于 ${updatedAt}`}</span>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 hover:bg-base disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
          刷新
        </button>
      </div>

      {error && (
        <div className="rounded-btn border border-bear/40 bg-bear/10 px-3 py-2 text-xs text-bear">
          加载失败：{error}
        </div>
      )}

      {summary?.alert_active && (
        <div className="flex items-center gap-2 rounded-btn border border-bear/40 bg-bear/10 px-3 py-2 text-xs text-bear">
          <AlertTriangle className="h-4 w-4" />
          集中度告警激活（live_outputs/ALERT_ACTIVE.json 存在）—— 检查单票持仓占比
        </div>
      )}

      {/* ── 心跳仪表 ── */}
      <Card title="引擎心跳">
        {!hb && <div className="text-xs text-muted">无心跳文件 —— 引擎从未启动，或 live_outputs 缺失。</div>}
        {hb && (
          <>
            {!fresh && (
              <div className="mb-2 flex items-center gap-1.5 text-[11px] text-amber-500">
                <AlertTriangle className="h-3.5 w-3.5" />
                心跳已陈旧（{summary?.heartbeat_age_seconds ?? '?'}s 未更新 &gt; 180s）—— 进程可能已退出。
                心跳是跨进程持久化的，进程死了文件还在，以新鲜度为准。
              </div>
            )}
            <div className="grid grid-cols-2 gap-x-6 md:grid-cols-3">
              <KV k="状态" v={<span className={hb.status === 'running' ? 'text-bull font-medium' : ''}>{hb.status ?? '—'}</span>} />
              <KV k="最后成功" v={hb.last_success_at ?? '—'} mono />
              <KV k="心跳更新" v={hb.updated_at ?? '—'} mono />
              <KV k="熔断器" v={<BreakerBadge state={hb.breaker_state} />} />
              <KV k="熔断开断/失败" v={`${hb.breaker_opens ?? 0} / ${hb.breaker_fails ?? 0}`} mono />
              <KV k="重建/拒建" v={<span className={cn((hb.rebuild_rejected ?? 0) > 0 && 'text-amber-500')}>{hb.rebuild_count ?? 0} / {hb.rebuild_rejected ?? 0}</span>} mono />
              <KV k="采集错误" v={<span className={cn((hb.collect_errors ?? 0) > 0 && 'text-bear')}>{hb.collect_errors ?? 0}</span>} mono />
              <KV k="撮合错误" v={hb.match_errors ?? 0} mono />
              <KV k="登录线程/挂起" v={`${hb.login_threads_started ?? 0} / ${hb.pending_login_alive ? '是' : '否'}`} mono />
            </div>
          </>
        )}
      </Card>

      {/* ── 观察单元 ── */}
      <Card title={`观察单元（${units.length}）`}>
        {units.length === 0 && <div className="text-xs text-muted">live_outputs 下无观察单元。</div>}
        {units.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border text-left">
                  <th className="py-1.5 pr-3 font-medium">单元</th>
                  <th className="py-1.5 pr-3 font-medium text-right">权益</th>
                  <th className="py-1.5 pr-3 font-medium text-right">当日盈亏</th>
                  <th className="py-1.5 pr-3 font-medium text-right">累计盈亏</th>
                  <th className="py-1.5 pr-3 font-medium text-right">累计回撤</th>
                  <th className="py-1.5 pr-3 font-medium text-right">委/成</th>
                  <th className="py-1.5 pr-3 font-medium text-right">一致性</th>
                  <th className="py-1.5 pr-3 font-medium">持仓</th>
                  <th className="py-1.5 font-medium">数据质量</th>
                </tr>
              </thead>
              <tbody>
                {units.map(u => (
                  <tr key={`${u.symbol}×${u.strategy}`} className="border-b border-border/60 last:border-0">
                    <td className="py-1.5 pr-3">
                      <span className="font-mono">{u.symbol}</span>
                      <span className="text-muted"> × {u.strategy}</span>
                      {!u.is_today && <span className="ml-1 text-[10px] text-amber-500">({u.summary_date})</span>}
                      {u.stopped && <span className="ml-1 rounded bg-bear/15 px-1 text-[10px] text-bear">stopped</span>}
                      {u.defense && <span className="ml-1 rounded bg-amber-500/15 px-1 text-[10px] text-amber-500">defense</span>}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">{fmtNum(u.equity, 0)}</td>
                    <td className={cn('py-1.5 pr-3 text-right font-mono', pnlTone(u.day_pnl))}>{fmtPnl(u.day_pnl)}</td>
                    <td className={cn('py-1.5 pr-3 text-right font-mono', pnlTone(u.cum_pnl))}>{fmtPnl(u.cum_pnl)}</td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted">
                      {u.cumulative_drawdown !== null ? `${(u.cumulative_drawdown * 100).toFixed(2)}%` : '—'}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">{u.orders ?? 0}/{u.fills ?? 0}</td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {u.engine_consistency !== null && u.engine_consistency !== undefined ? u.engine_consistency.toFixed(2) : '—'}
                    </td>
                    <td className="py-1.5 pr-3 font-mono">
                      {u.positions.length === 0
                        ? <span className="text-muted">空仓</span>
                        : u.positions.map(p => (
                          <div key={p.symbol}>
                            {p.quantity} 股 @ {fmtNum(p.average_cost)}
                            <span className="text-muted">（市值 {fmtNum((p.quantity ?? 0) * (p.market_price ?? 0), 0)}）</span>
                          </div>
                        ))}
                    </td>
                    <td className="py-1.5">
                      {u.data_quality.status
                        ? <span className={cn('rounded px-1 py-0.5 text-[10px]', u.data_quality.usable_for_sample ? 'bg-base text-muted' : 'bg-amber-500/15 text-amber-500')}>
                            {u.data_quality.status} ({u.data_quality.bars}/{u.data_quality.expected})
                          </span>
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* ── 实盘口径（可编辑，H64） ── */}
      <Card title="实盘口径（可调整 · 次日生效）">
        {!editable && <div className="text-xs text-muted">读取失败，或 quant_app 配置层不可用。</div>}
        {editable && (
          <div className="space-y-3">
            <div className="text-[11px] text-muted">
              标的：
              {editable.current.positions.filter(p => p.enabled !== false)
                .map(p => `${p.code}×${p.strategy_id}`).join('、') || '（空）'}
              {editable.position_checks.length > 0 && (
                <span>
                  {' '}· 引擎冻结价 {editable.position_checks[0].price ?? '—'} 元 → 底仓{' '}
                  {editable.position_checks[0].base_position ?? '—'} 股
                </span>
              )}
            </div>

            {/* 标的 × 策略（可增删） */}
            <div className="space-y-2">
              <div className="text-[11px] text-muted">标的 × 策略（可增删；同一标的可挂多个策略）</div>
              {rows.map((r, i) => {
                const meta = editable.strategy_params?.[r.strategy_id]?.param_meta ?? []
                const nOv = Object.keys(r.param_overrides ?? {}).length
                return (
                <div key={i} className="space-y-2 rounded-btn border border-border/50 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                  <input
                    value={r.code}
                    placeholder="6 位代码"
                    onChange={e => {
                      touchedRef.current = true
                      setRows(prev => prev.map((x, j) => (j === i ? { ...x, code: e.target.value } : x)))
                    }}
                    className="w-28 rounded-btn border border-border bg-base px-2 py-1 text-xs"
                  />
                  <select
                    value={r.strategy_id}
                    onChange={e => {
                      touchedRef.current = true
                      setRows(prev => prev.map((x, j) => (j === i ? { ...x, strategy_id: e.target.value } : x)))
                    }}
                    className="rounded-btn border border-border bg-base px-2 py-1 text-xs"
                  >
                    {(editable.strategies ?? []).map(s => (
                      <option key={s.id} value={s.id}>{s.label}（{s.id}）</option>
                    ))}
                  </select>
                  <label className="flex items-center gap-1 text-[11px] text-muted">
                    <input
                      type="checkbox"
                      checked={r.enabled}
                      onChange={e => {
                        touchedRef.current = true
                        setRows(prev => prev.map((x, j) => (j === i ? { ...x, enabled: e.target.checked } : x)))
                      }}
                      className="accent-[hsl(var(--accent))]"
                    />
                    启用
                  </label>
                  <button
                    type="button"
                    onClick={() => {
                      touchedRef.current = true
                      setRows(prev => prev.filter((_, j) => j !== i))
                    }}
                    className="rounded-btn border border-border px-2 py-1 text-[11px] hover:bg-base"
                  >
                    删除
                  </button>
                    <button
                      type="button"
                      onClick={() => setOpenParams(p => ({ ...p, [i]: !p[i] }))}
                      className="rounded-btn border border-border px-2 py-1 text-[11px] hover:bg-base"
                    >
                      参数{nOv ? ` · 已改 ${nOv} 项` : ''} {openParams[i] ? '▾' : '▸'}
                    </button>
                  </div>
                  {openParams[i] && (
                    <ParamEditor
                      meta={meta}
                      value={r.param_overrides ?? {}}
                      onChange={next => {
                        touchedRef.current = true
                        setRows(prev => prev.map((x, j) => (j === i ? { ...x, param_overrides: next } : x)))
                      }}
                    />
                  )}
                </div>
                )
              })}
              <button
                type="button"
                onClick={() => {
                  touchedRef.current = true
                  const first = editable.strategies?.[0]?.id ?? 'grid_trade'
                  setRows(prev => [...prev, { code: '', strategy_id: first, enabled: true, param_overrides: {} }])
                }}
                className="rounded-btn border border-border px-2 py-1 text-[11px] hover:bg-base"
              >
                + 添加标的
              </button>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {([
                ['cash', '资金基准(元)', '10000'],
                ['riskPct', '底仓比例(%)', '0.5'],
                ['capPct', '单标的上限(%)', '1'],
                ['orderPct', '单笔上限(%)', '1'],
              ] as const).map(([k, label, step]) => (
                <label key={k} className="block">
                  <span className="mb-1 block text-[11px] text-muted">{label}</span>
                  <input
                    type="number"
                    step={step}
                    value={form[k]}
                    onChange={e => {
                      touchedRef.current = true
                      setForm(prev => ({ ...prev, [k]: e.target.value }))
                    }}
                    className="w-full rounded-btn border border-border bg-base px-2 py-1 text-xs"
                  />
                </label>
              ))}
            </div>

            {/* 纸面交易起点（H67）：留空 = 用常量兜底 */}
            <div className="flex flex-wrap items-end gap-3 rounded-btn border border-border/50 p-2">
              <label className="block">
                <span className="mb-1 block text-[11px] text-muted">纸面交易起点</span>
                <input
                  type="date"
                  value={form.startDate}
                  onChange={e => {
                    touchedRef.current = true
                    setForm(prev => ({ ...prev, startDate: e.target.value }))
                  }}
                  className="rounded-btn border border-border bg-base px-2 py-1 text-xs"
                />
              </label>
              <div className="text-[11px] text-muted">
                引擎当前生效：
                <span className="font-mono text-foreground">{editable.paper_start_effective ?? '—'}</span>
                {!editable.current.paper_start_date && <span className="ml-1">（未设置，用常量兜底）</span>}
                <div className="mt-0.5">
                  改这个不会自动重放已有账户；要让新起点生效，需清掉该标的的纸面状态目录。
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={save}
                disabled={saving}
                className="rounded-btn border border-border px-3 py-1 text-xs hover:bg-base disabled:opacity-50"
              >
                {saving ? '保存中…' : '保存（次日 04:30 生效）'}
              </button>
              <span className="text-[11px] text-muted">{editable.takes_effect}</span>
            </div>

            {saveMsg && (
              <div className="rounded-btn border border-border px-2 py-1 text-[11px]">{saveMsg}</div>
            )}
            {saveErrs.length > 0 && (
              <ul className="space-y-1 rounded-btn border border-bear/40 bg-bear/10 px-2 py-1 text-[11px] text-bear">
                {saveErrs.map((e, i) => <li key={i}>· {e}</li>)}
              </ul>
            )}
            <div className="text-[11px] text-muted">
              校验用的是「引擎冻结价」（TRADE_START_DATE 起首个收盘），不是最新价 ——
              两者差距大时能拦住“面板放行、引擎启动即报错”。
            </div>
          </div>
        )}
      </Card>

      {/* ── 生效配置 ── */}
      <Card title="生效配置（H57 留痕）">
        {!summary?.config_applied && (
          <div className="text-xs text-muted">
            无 config_applied.json —— 尚无 live 启动留痕（该文件在每次 live 启动时写入 data/live/）。
          </div>
        )}
        {summary?.config_applied && (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-x-6 md:grid-cols-3">
              <KV k="来源" v={<span className={cn(summary.config_applied.source.includes('live_config') ? 'text-amber-500' : 'text-muted')}>{summary.config_applied.source}</span>} />
              <KV k="生效时间" v={summary.config_applied.applied_at} mono />
              <KV k="组合数" v={summary.config_applied.combos.length} mono />
            </div>
            <div className="text-xs font-mono text-secondary">
              {summary.config_applied.combos.map(([c, s]) => `${c}×${s}`).join(' · ')}
            </div>
            {hasOverrides && (
              <div className="rounded-btn bg-base border border-border p-2 text-xs">
                <div className="text-muted mb-1">参数覆盖（H57-S5 前应为空）</div>
                {Object.entries(overrides).map(([k, v]) => (
                  <div key={k} className="font-mono">{k}: {JSON.stringify(v)}</div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      <div className="flex items-start gap-1.5 text-[10px] text-muted">
        <Activity className="mt-0.5 h-3 w-3 shrink-0" />
        数据直读 quant_app 产物文件：live_outputs/heartbeat.json · health.json · &lt;标的&gt;/&lt;策略&gt;/daily_summary.json、paper_state.json · data/live/config_applied.json。本页只读，不含任何交易与写入口。
      </div>
    </div>
  )
}

const extension: FrontendExtension = {
  id: 'tsp.live-status',
  apiVersion: 1,
  routes: [
    { id: 'live-status', path: '/live/status', component: LiveStatusPage },
  ],
  navigation: [
    {
      id: 'live-status',
      routeId: 'live-status',
      label: 'Live 状态',
      icon: Activity,
      order: 605,
    },
  ],
}

export default extension
