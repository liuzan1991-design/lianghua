import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { LineChart, Loader2, Play, Square, RotateCcw } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

/** M2 做T回测只读页面：接 M2 六个接口，只做历史回测，不含任何交易。 */

const BASE = '/api/custom/m2'
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'timeout'])
const SSE_EVENT_NAMES = [
  'queued', 'running', 'progress', 'completed', 'failed',
  'cancelled', 'interrupted', 'timeout', 'cancel_requested',
]

interface Strategy {
  id: string
  name: string
  description: string
  default_params?: Record<string, unknown>
}

interface RunSummary {
  strategy_id: string
  strategy_name: string
  final_equity: number
  total_return: number
  annual_return: number
  max_drawdown: number
  total_fees: number
  trade_count: number
  equity_rows: number
  avg_exposure: number
  max_exposure: number
  capital_return: number
  params: Record<string, unknown>
  data_source: string
  data_adjustment: string
}

interface RunState {
  run_id: string
  status: string
  progress: number
  message: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  request: Record<string, unknown>
  result: RunSummary | null
  error_code: string | null
  artifacts: string[]
  worker_pid: number | null
}

interface Health {
  status: string
  mode: string
  process_isolation: boolean
  amazingdata_sdk_allowed: boolean
  engine_available: boolean
  data_available: boolean
  worker_available: boolean
}

async function m2Fetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      const code = body?.detail?.code ?? body?.detail ?? body?.message
      if (code) detail = typeof code === 'string' ? code : JSON.stringify(code)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

function fmtMoney(value: number): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function fmtPct(value: number): string {
  const pct = value * 100
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
}

/** 日期转后端可解析的 datetime 字符串；end 覆盖到当日 23:59:59。空值返回 null 交由后端按可选字段处理。 */
function toStartIso(date: string): string | null {
  return date ? `${date}T00:00:00` : null
}

function toEndIso(date: string): string | null {
  return date ? `${date}T23:59:59` : null
}

const STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  timeout: '超时',
}

const STATUS_TONE: Record<string, string> = {
  pending: 'text-warning',
  running: 'text-accent',
  completed: 'text-bull',
  failed: 'text-danger',
  cancelled: 'text-muted',
  interrupted: 'text-muted',
  timeout: 'text-danger',
}

function M2BacktestPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [strategiesLoading, setStrategiesLoading] = useState(true)
  const [strategiesError, setStrategiesError] = useState<string | null>(null)
  const [health, setHealth] = useState<Health | null>(null)

  const [strategyId, setStrategyId] = useState('')
  const [symbol, setSymbol] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [initCash, setInitCash] = useState('1000000')

  const [run, setRun] = useState<RunState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const pollTimer = useRef<number | null>(null)
  const esRef = useRef<EventSource | null>(null)

  const clearTimers = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
    esRef.current?.close()
    esRef.current = null
  }, [])

  useEffect(() => {
    let alive = true
    m2Fetch<Health>('/health')
      .then(h => alive && setHealth(h))
      .catch(() => alive && setHealth(null))
    m2Fetch<Strategy[]>('/strategies')
      .then(list => {
        if (!alive) return
        setStrategies(list)
        if (list.length > 0) setStrategyId(list[0].id)
      })
      .catch(err => alive && setStrategiesError(err instanceof Error ? err.message : String(err)))
      .finally(() => alive && setStrategiesLoading(false))
    return () => { alive = false }
  }, [])

  useEffect(() => clearTimers, [clearTimers])

  const selectedStrategy = useMemo(
    () => strategies.find(s => s.id === strategyId) ?? null,
    [strategies, strategyId],
  )

  const refreshRun = useCallback(async (runId: string) => {
    try {
      const state = await m2Fetch<RunState>(`/backtests/${runId}`)
      setRun(state)
      if (TERMINAL.has(state.status)) clearTimers()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      clearTimers()
    }
  }, [clearTimers])

  const watchRun = useCallback((runId: string) => {
    clearTimers()
    // 兜底轮询：保证即使 SSE 中断也能拿到状态与最终结果
    pollTimer.current = window.setInterval(() => { void refreshRun(runId) }, 1000)

    // SSE 实时进度
    const es = new EventSource(`${BASE}/backtests/${runId}/events`)
    esRef.current = es
    for (const name of SSE_EVENT_NAMES) {
      es.addEventListener(name, ((evt: MessageEvent) => {
        try {
          const payload = JSON.parse(evt.data) as { event?: string; progress?: number; message?: string }
          setRun(prev => prev && prev.run_id === runId
            ? {
                ...prev,
                progress: payload.progress ?? prev.progress,
                message: payload.message ?? prev.message,
              }
            : prev)
          if (payload.event && TERMINAL.has(payload.event)) {
            void refreshRun(runId)
          }
        } catch { /* ignore malformed SSE payload */ }
      }) as EventListener)
    }
    es.onerror = () => { /* 轮询兜底，忽略 SSE 断开 */ }
  }, [clearTimers, refreshRun])

  const startBacktest = useCallback(async () => {
    if (!strategyId) { setError('请选择策略'); return }
    if (!symbol.trim()) { setError('请输入股票代码'); return }
    const parsedCash = Number(initCash)
    if (!Number.isFinite(parsedCash) || parsedCash <= 0) { setError('初始资金必须为正数'); return }

    setBusy(true)
    setError(null)
    setRun(null)
    try {
      const created = await m2Fetch<RunState>('/backtests', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          symbol: symbol.trim(),
          start: toStartIso(startDate),
          end: toEndIso(endDate),
          init_cash: parsedCash,
          params: {},
        }),
      })
      setRun(created)
      watchRun(created.run_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [strategyId, symbol, startDate, endDate, initCash, watchRun])

  const cancelBacktest = useCallback(async () => {
    if (!run) return
    try {
      await m2Fetch<RunState>(`/backtests/${run.run_id}/cancel`, { method: 'POST' })
      void refreshRun(run.run_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [run, refreshRun])

  const reset = useCallback(() => {
    clearTimers()
    setRun(null)
    setError(null)
  }, [clearTimers])

  const canStart = !busy && !strategiesLoading && !run
  const canCancel = run != null && !TERMINAL.has(run.status)

  return (
    <>
      <PageHeader
        title="做T回测"
        subtitle="只读历史回测 · 独立子进程执行 · 不含交易"
        right={
          <span className="font-mono text-xs text-muted">
            M2 /api/custom/m2
          </span>
        }
      />

      <div className="px-5 py-4 space-y-4 max-w-4xl">
        {/* 策略与参数表单 */}
        <section className="rounded-card border border-border bg-surface p-4 space-y-3">
          <div className="flex items-center justify-between text-xs text-muted">
            <span>M2服务状态</span>
            <span className={health?.status === 'ok' ? 'text-bull' : health ? 'text-danger' : 'text-muted'}>
              {health ? health.status : '不可用'}
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <label className="flex flex-col gap-1 text-xs text-secondary">
              策略
              {strategiesLoading ? (
                <span className="h-9 rounded-input bg-elevated animate-pulse" />
              ) : strategiesError ? (
                <span className="h-9 rounded-input bg-elevated grid place-items-center text-danger px-2">{strategiesError}</span>
              ) : (
                <select
                  value={strategyId}
                  onChange={e => setStrategyId(e.target.value)}
                  className="h-9 rounded-input bg-base border border-border px-2 text-sm text-foreground focus:outline-none focus:border-accent"
                >
                  {strategies.map(s => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              )}
            </label>

            <label className="flex flex-col gap-1 text-xs text-secondary">
              股票代码
              <input
                value={symbol}
                onChange={e => setSymbol(e.target.value)}
                placeholder="如 688256.SH"
                className="h-9 rounded-input bg-base border border-border px-2 text-sm text-foreground font-mono focus:outline-none focus:border-accent"
              />
            </label>

            <label className="flex flex-col gap-1 text-xs text-secondary">
              开始日期
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="h-9 rounded-input bg-base border border-border px-2 text-sm text-foreground focus:outline-none focus:border-accent"
              />
            </label>

            <label className="flex flex-col gap-1 text-xs text-secondary">
              结束日期
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="h-9 rounded-input bg-base border border-border px-2 text-sm text-foreground focus:outline-none focus:border-accent"
              />
            </label>
          </div>

          <div className="flex items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-secondary">
              初始资金
              <input
                value={initCash}
                onChange={e => setInitCash(e.target.value)}
                inputMode="decimal"
                className="h-9 rounded-input bg-base border border-border px-2 text-sm text-foreground font-mono focus:outline-none focus:border-accent"
              />
            </label>

            <button
              type="button"
              onClick={() => void startBacktest()}
              disabled={!canStart}
              className="inline-flex items-center gap-1.5 h-9 px-3 rounded-btn bg-accent text-base text-xs font-medium hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition-opacity"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              开始回测
            </button>

            {canCancel && (
              <button
                type="button"
                onClick={() => void cancelBacktest()}
                className="inline-flex items-center gap-1.5 h-9 px-3 rounded-btn bg-elevated text-danger text-xs font-medium hover:bg-elevated/80 transition-colors"
              >
                <Square className="h-4 w-4" />
                取消
              </button>
            )}

            {run && (
              <button
                type="button"
                onClick={reset}
                className="inline-flex items-center gap-1.5 h-9 px-3 rounded-btn bg-elevated text-secondary text-xs font-medium hover:bg-elevated/80 transition-colors"
              >
                <RotateCcw className="h-4 w-4" />
                重置
              </button>
            )}
          </div>

          {selectedStrategy?.description && (
            <p className="text-xs text-muted leading-relaxed">{selectedStrategy.description}</p>
          )}

          {error && (
            <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>
          )}
        </section>

        {/* 运行状态 */}
        {run && (
          <section className="rounded-card border border-border bg-surface p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className={cn('text-sm font-medium', STATUS_TONE[run.status] ?? 'text-foreground')}>
                  {STATUS_LABEL[run.status] ?? run.status}
                </span>
                {!TERMINAL.has(run.status) && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
              </div>
              <span className="font-mono text-xs text-muted">{run.run_id}</span>
            </div>

            {!TERMINAL.has(run.status) && (
              <div className="space-y-1.5">
                <div className="h-1.5 w-full rounded-full bg-elevated overflow-hidden">
                  <div
                    className="h-full bg-accent transition-all duration-300"
                    style={{ width: `${Math.min(100, Math.max(0, run.progress))}%` }}
                  />
                </div>
                <div className="text-xs text-secondary">{run.message}</div>
              </div>
            )}

            {run.error_code && (
              <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                错误码 {run.error_code}
              </div>
            )}
          </section>
        )}

        {/* 回测结果 */}
        {run?.result && (
          <section className="rounded-card border border-border bg-surface p-4 space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Metric label="期末权益" value={fmtMoney(run.result.final_equity)} mono />
              <Metric label="总收益" value={fmtPct(run.result.total_return)} mono tone={run.result.total_return >= 0 ? 'bull' : 'bear'} />
              <Metric label="最大回撤" value={fmtPct(run.result.max_drawdown)} mono tone="bear" />
              <Metric label="年化收益" value={fmtPct(run.result.annual_return)} mono />
              <Metric label="交易笔数" value={String(run.result.trade_count)} mono />
              <Metric label="手续费" value={fmtMoney(run.result.total_fees)} mono />
              <Metric label="平均暴露" value={fmtPct(run.result.avg_exposure)} mono />
              <Metric label="最大暴露" value={fmtPct(run.result.max_exposure)} mono />
            </div>

            <div className="text-xs text-muted">
              数据来源 <span className="text-secondary font-mono">{run.result.data_source}</span>
              {' · '}复权口径 <span className="text-secondary font-mono">{run.result.data_adjustment}</span>
              {' · '}权益行数 <span className="text-secondary font-mono">{run.result.equity_rows}</span>
            </div>

            {run.artifacts.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-medium text-secondary">结果文件</div>
                <div className="flex flex-wrap gap-2">
                  {run.artifacts.map(name => (
                    <a
                      key={name}
                      href={`${BASE}/backtests/${run.run_id}/artifacts/${name}`}
                      download
                      className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-btn bg-elevated text-xs text-secondary hover:bg-elevated/80 hover:text-foreground transition-colors font-mono"
                    >
                      {name}
                    </a>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        {/* 空态 */}
        {!run && !busy && (
          <section className="rounded-card border border-dashed border-border bg-base p-10 grid place-items-center">
            <div className="text-center space-y-2">
              <LineChart className="h-8 w-8 mx-auto text-muted" />
              <div className="text-sm text-secondary">选择策略、股票和区间后开始回测</div>
              <div className="text-xs text-muted">结果以摘要和文件形式返回，可下载权益曲线与交易明细</div>
            </div>
          </section>
        )}
      </div>
    </>
  )
}

function Metric({ label, value, tone, mono }: {
  label: string
  value: string
  tone?: 'bull' | 'bear'
  mono?: boolean
}) {
  return (
    <div className="rounded-btn bg-base border border-border px-3 py-2">
      <div className="text-xs text-muted">{label}</div>
      <div className={cn('mt-0.5 text-sm', mono && 'font-mono', tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-foreground')}>
        {value}
      </div>
    </div>
  )
}

const extension: FrontendExtension = {
  id: 'm2.intraday-t-backtest',
  apiVersion: 1,
  routes: [
    { id: 'm2-backtest', path: '/m2/backtest', component: M2BacktestPage },
  ],
  navigation: [
    {
      id: 'm2-backtest',
      routeId: 'm2-backtest',
      label: '做T回测',
      icon: LineChart,
      order: 600,
    },
  ],
}

export default extension
