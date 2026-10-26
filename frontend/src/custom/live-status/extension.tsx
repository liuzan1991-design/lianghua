import { useCallback, useEffect, useState } from 'react'
import { Activity, AlertTriangle, RefreshCw } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

/**
 * Live 状态页 —— 对接后端 `app/custom/live_status.py`（/api/custom/live-status）。
 *
 * 定位：**纯只读仪表盘**。展示 quant_app 实盘引擎的心跳 / 观察单元 / 生效配置。
 * 红线（H57 方案）：本页无任何写按钮；live_config 的写入口在 S5 全部落地并验证前不存在。
 */

const BASE = '/api/custom/live-status'

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

function LiveStatusPage() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [units, setUnits] = useState<Unit[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<string>('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [s, u] = await Promise.all([
        fetch(`${BASE}/summary`).then(r => { if (!r.ok) throw new Error(`summary ${r.status}`); return r.json() as Promise<Summary> }),
        fetch(`${BASE}/units`).then(r => { if (!r.ok) throw new Error(`units ${r.status}`); return r.json() as Promise<{ units: Unit[] }> }),
      ])
      setSummary(s)
      setUnits(u.units)
      setError(null)
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

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
        subtitle="quant_app 实盘引擎只读仪表盘 · 无写入口"
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
