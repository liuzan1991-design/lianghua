import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Loader2,
  Lock,
  Play,
  PlugZap,
  RefreshCw,
  Square,
  Unlock,
  Wallet,
} from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

/**
 * 实盘交易页 —— 对接后端 `app/custom/live_trade.py`（/api/custom/trade）。
 *
 * 定位：**只做执行，不做选股**。策略与回测在「做T回测」页，选股在「策略选股」页。
 * 安全：本页所有写操作都要先"解锁"（口令 + 共享密钥 → 15 分钟 token）。
 *       未解锁时页面只读，按钮全部禁用。
 */

const BASE = '/api/custom/trade'

// ---------- 类型 ----------
interface GateState {
  env_armed: boolean
  session_armed: boolean
  expires_in: number
}

interface Limits {
  max_order_notional: number
  max_orders_per_day: number
  orders_used_today: number
  allowed_codes: string[]
}

interface Health {
  extension: string
  engine_root: string
  engine_ok: boolean
  quant_python?: string
  quant_python_ok?: boolean
  userdata_configured: boolean
  userdata_source?: 'env' | 'auto' | 'missing'
  userdata_path?: string | null
  broker_ready: boolean
  limits: Limits
  gate: GateState
}

interface Asset {
  cash: number | null
  frozen_cash: number | null
  market_value: number | null
  total_asset: number | null
}

interface Position {
  stock_code: string
  volume: number
  can_use_volume: number
  open_price: number
  market_value: number
}

interface OrderRow {
  order_id: number
  stock_code: string
  order_volume: number
  price: number
  traded_volume: number
  order_status: number
  status_msg?: string | null
}

interface LiveState {
  running: boolean
  pid: number | null
  managed_by_page?: boolean
  external_running?: boolean
  heartbeat: Record<string, unknown> | null
  heartbeat_age_seconds?: number | null
  heartbeat_fresh?: boolean
}

// ---------- 请求封装 ----------
class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await res.text()
  // ⚠️ 路由未注册时，后端的 SPA 兜底会把 HTML 以 200 返回。
  // 若只看 res.ok 就会把 HTML 当成数据对象吞下去 —— 页面表现为"字段全是 —"，
  // 且错误提示永不触发（真实发生过）。所以先卡 Content-Type。
  const ct = res.headers.get('content-type') || ''
  if (!ct.includes('application/json')) {
    throw new ApiError(
      0,
      `后端未返回 JSON（HTTP ${res.status}）—— live-trade 路由未注册，需重启后端`,
    )
  }
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  if (!res.ok) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : `${res.status} ${res.statusText}`
    throw new ApiError(res.status, detail)
  }
  return body as T
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })

/** 心跳年龄（秒 → 人话）。用于区分"当前在跑"和"历史残留"。 */
const fmtAge = (s: number | null | undefined) => {
  if (s === null || s === undefined || Number.isNaN(s)) return '—'
  if (s < 60) return `${Math.round(s)} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  if (s < 86400) return `${(s / 3600).toFixed(1)} 小时前`
  return `${(s / 86400).toFixed(1)} 天前`
}

// ---------- 主页面 ----------
function LiveTradePage() {
  const [health, setHealth] = useState<Health | null>(null)
  const [asset, setAsset] = useState<Asset | null>(null)
  const [positions, setPositions] = useState<Position[]>([])
  const [orders, setOrders] = useState<OrderRow[]>([])
  const [live, setLive] = useState<LiveState | null>(null)
  const [token, setToken] = useState('')
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const [marketNotice, setMarketNotice] = useState('')
  const [busy, setBusy] = useState(false)

  // 解锁表单
  const [phrase, setPhrase] = useState('')
  const [secret, setSecret] = useState('')

  // 下单表单
  const [code, setCode] = useState('300308')
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [qty, setQty] = useState('100')
  const [price, setPrice] = useState('')

  const timer = useRef<number | null>(null)

  const loadHealth = useCallback(async () => {
    try {
      const h = await call<Health>('/health')
      setHealth(h)
      setErr('')
      return h
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 404
        ? '后端未启用 live-trade 扩展（重启后端后生效）'
        : (e as Error).message)
      return null
    }
  }, [])

  const loadMarket = useCallback(async () => {
    try {
      const [a, p, o] = await Promise.all([
        call<{ asset: Asset }>('/account'),
        call<{ positions: Position[] }>('/positions'),
        call<{ orders: OrderRow[] }>('/orders'),
      ])
      setAsset(a.asset)
      setPositions(p.positions ?? [])
      setOrders(o.orders ?? [])
      setMarketNotice('')
    } catch (e) {
      // 503 = QMT 未就绪（预期内），但不能静默吞掉 —— 否则用户分不清
      // "没有持仓" 和 "根本连不上"。用黄色说明，不刷红。
      if (e instanceof ApiError && e.status === 503) {
        setMarketNotice(e.message)
      } else {
        setErr((e as Error).message)
      }
    }
  }, [])

  const loadLive = useCallback(async () => {
    try {
      setLive(await call<LiveState>('/live'))
    } catch {
      /* 忽略 */
    }
  }, [])

  useEffect(() => {
    void loadHealth()
    void loadLive()
    // 账户/持仓/委托是**只读**的，进页面就要加载 —— 不能等解锁。
    // （曾把 loadMarket 绑在 token 上，导致"要先输口令才能看见自己的余额"，
    //   页面永远显示 —。这是个设计错误，不是数据问题。）
    void loadMarket()
    timer.current = window.setInterval(() => {
      void loadLive()
      void loadMarket()
    }, 15000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [loadHealth, loadLive, loadMarket])

  const unlocked = Boolean(token) && Boolean(health?.gate.session_armed)
  // 券商侧会返回 0 股的占位记录（如 131990 回购），显示出来只会让人以为有持仓
  const activePositions = positions.filter(p => (p.volume ?? 0) > 0)

  const doArm = async () => {
    setBusy(true)
    setErr('')
    setNotice('')
    try {
      const r = await call<{ token: string }>('/gate', {
        method: 'POST',
        body: JSON.stringify({ action: 'arm', phrase, secret }),
      })
      setToken(r.token)
      setNotice('已解锁，15 分钟内可下单。')
      setSecret('')
      await loadHealth()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const doDisarm = async () => {
    setBusy(true)
    try {
      await call('/gate', { method: 'POST', body: JSON.stringify({ action: 'disarm' }) })
      setToken('')
      setNotice('已锁定')
      await loadHealth()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const submitOrder = async () => {
    if (!unlocked) return
    const q = Number(qty)
    const p = Number(price)
    const est = q * p
    const ok = window.confirm(
      `确认${side === 'BUY' ? '买入' : '卖出'} ${code} ${q} 股 @ ${p}？\n` +
        `名义金额约 ${fmt(est, 0)} 元\n\n国盛 QMT 无模拟盘，这是真实委托。`,
    )
    if (!ok) return
    setBusy(true)
    setErr('')
    try {
      const r = await call<{ order_id: number }>('/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Trade-Arm': token },
        body: JSON.stringify({ code, side, quantity: q, price: p, remark: 'ui-manual' }),
      })
      setNotice(`已委托，委托号 ${r.order_id}`)
      await loadMarket()
      await loadHealth()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const cancelOrder = async (orderId: number) => {
    if (!unlocked) return
    setBusy(true)
    try {
      await call(`/orders/${orderId}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Trade-Arm': token },
      })
      setNotice(`已撤单 ${orderId}`)
      await loadMarket()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const toggleLive = async (start: boolean) => {
    if (!unlocked) return
    setBusy(true)
    try {
      await call(start ? '/live/start' : '/live/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Trade-Arm': token },
      })
      setNotice(start ? '已启动实盘策略进程' : '已停止')
      await loadLive()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const g = health?.gate
  const limits = health?.limits

  return (
    <>
      <PageHeader
        title="实盘交易"
        subtitle="QMT 执行层 · 下单与策略托管"
        titleExtra={
          g ? (
            <span
              className={cn(
                'rounded px-1.5 py-0.5 text-[10px] font-medium leading-none',
                g.session_armed
                  ? 'bg-danger/15 text-danger'
                  : g.env_armed
                    ? 'bg-warning/15 text-warning'
                    : 'bg-elevated text-muted',
              )}
            >
              {g.session_armed ? `已解锁 ${g.expires_in}s` : g.env_armed ? '已授权·未解锁' : '未授权'}
            </span>
          ) : null
        }
        right={
          <button
            type="button"
            onClick={() => {
              void loadHealth()
              void loadMarket()
              void loadLive()
            }}
            className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary transition-colors hover:bg-elevated/80 hover:text-foreground"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            刷新
          </button>
        }
      />

      <div className="space-y-3 p-4">
        {err && (
          <Banner tone="danger" icon={AlertCircle}>
            {err}
          </Banner>
        )}
        {notice && (
          <Banner tone="accent" icon={CheckCircle2}>
            {notice}
          </Banner>
        )}

        {/* ===== 解锁闸 ===== */}
        <section className="rounded-card border border-border bg-surface/80 p-3">
          <div className="flex items-center gap-2">
            <div className="rounded-lg bg-elevated/50 p-1.5">
              {unlocked ? (
                <Unlock className="h-4 w-4 text-danger" />
              ) : (
                <Lock className="h-4 w-4 text-muted" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-foreground">交易权限</div>
              <div className="mt-0.5 text-[11px] text-muted">
                {health
                  ? health.userdata_configured
                    ? 'QMT 路径已配置；解锁后 15 分钟内可下单'
                    : '未配置 QMT_USERDATA_MINI —— 只读可用，下单会失败'
                  : '正在读取后端状态…'}
              </div>
            </div>
          </div>

          {!unlocked ? (
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <input
                value={phrase}
                onChange={e => setPhrase(e.target.value)}
                placeholder="确认口令：确认下单"
                className="h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
              />
              <input
                type="password"
                value={secret}
                onChange={e => setSecret(e.target.value)}
                placeholder="共享密钥 TRADE_ARM_SECRET"
                className="h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
              />
              <button
                type="button"
                onClick={doArm}
                disabled={busy || !phrase || !secret}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-btn bg-accent text-xs font-semibold text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Unlock className="h-3.5 w-3.5" />}
                解锁下单
              </button>
            </div>
          ) : (
            <div className="mt-3 flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-muted">
                token {token.slice(0, 8)}… · 剩余 {g?.expires_in ?? 0}s
              </span>
              <button
                type="button"
                onClick={doDisarm}
                disabled={busy}
                className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:text-foreground disabled:opacity-50"
              >
                <Lock className="h-3.5 w-3.5" />
                立即锁定
              </button>
            </div>
          )}

          {limits && (
            <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-2 text-[10px] text-muted">
              <span className="rounded bg-elevated/60 px-1.5 py-0.5">
                单笔 ≤ {fmt(limits.max_order_notional, 0)}
              </span>
              <span className="rounded bg-elevated/60 px-1.5 py-0.5">
                当日 {limits.orders_used_today}/{limits.max_orders_per_day} 笔
              </span>
              <span className="rounded bg-elevated/60 px-1.5 py-0.5">
                白名单 {limits.allowed_codes.join(' / ') || '未配置'}
              </span>
            </div>
          )}
        </section>

        {/* ===== 账户 / 持仓 / 委托 ===== */}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <section className="rounded-card border border-border bg-surface/80 p-3">
            <SectionTitle icon={Wallet} title="账户资金" />
            <div className="mt-2 grid grid-cols-2 gap-1.5">
              <Metric label="总资产" value={fmt(asset?.total_asset)} mono />
              <Metric label="可用现金" value={fmt(asset?.cash)} mono />
              <Metric label="持仓市值" value={fmt(asset?.market_value)} mono />
              <Metric label="冻结资金" value={fmt(asset?.frozen_cash)} mono />
            </div>
            {marketNotice && (
              <div className="mt-2 text-[10px] leading-snug text-warning">
                账户未就绪：{marketNotice}
              </div>
            )}
          </section>

          <section className="rounded-card border border-border bg-surface/80 p-3 lg:col-span-2">
            <SectionTitle icon={PlugZap} title="持仓" extra={`${activePositions.length} 只`} />
            <div className="mt-2 max-h-52 overflow-auto">
              {activePositions.length === 0 ? (
                <Empty text="暂无持仓（或 QMT 未就绪）" />
              ) : (
                <table className="w-full text-left text-xs">
                  <thead className="text-[10px] text-muted">
                    <tr>
                      <th className="py-1 font-normal">代码</th>
                      <th className="py-1 text-right font-normal">持仓</th>
                      <th className="py-1 text-right font-normal">可用</th>
                      <th className="py-1 text-right font-normal">成本</th>
                      <th className="py-1 text-right font-normal">市值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activePositions.map(p => (
                      <tr key={p.stock_code} className="border-t border-border/40">
                        <td className="py-1 font-mono">{p.stock_code}</td>
                        <td className="py-1 text-right font-mono tabular">{p.volume}</td>
                        <td className="py-1 text-right font-mono tabular text-secondary">{p.can_use_volume}</td>
                        <td className="py-1 text-right font-mono tabular">{fmt(p.open_price, 3)}</td>
                        <td className="py-1 text-right font-mono tabular">{fmt(p.market_value, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            {positions.length > activePositions.length && (
              <div className="mt-1 text-[10px] text-muted">
                另有 {positions.length - activePositions.length} 条 0 股记录（券商侧占位，已隐藏）
              </div>
            )}

            <div className="mt-3 border-t border-border/60 pt-2">
              <SectionTitle icon={Copy} title="当日委托" extra={`${orders.length} 笔`} />
              <div className="mt-1.5 max-h-40 overflow-auto">
                {orders.length === 0 ? (
                  <Empty text="暂无委托" />
                ) : (
                  <table className="w-full text-left text-xs">
                    <thead className="text-[10px] text-muted">
                      <tr>
                        <th className="py-1 font-normal">委托号</th>
                        <th className="py-1 font-normal">代码</th>
                        <th className="py-1 text-right font-normal">价格</th>
                        <th className="py-1 text-right font-normal">数量</th>
                        <th className="py-1 text-right font-normal">成交</th>
                        <th className="py-1 text-right font-normal">状态</th>
                        <th className="py-1 text-right font-normal">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map(o => (
                        <tr key={o.order_id} className="border-t border-border/40">
                          <td className="py-1 font-mono text-muted">{o.order_id}</td>
                          <td className="py-1 font-mono">{o.stock_code}</td>
                          <td className="py-1 text-right font-mono tabular">{fmt(o.price, 3)}</td>
                          <td className="py-1 text-right font-mono tabular">{o.order_volume}</td>
                          <td className="py-1 text-right font-mono tabular text-secondary">{o.traded_volume}</td>
                          <td className="py-1 text-right text-muted">{o.status_msg ?? o.order_status}</td>
                          <td className="py-1 text-right">
                            <button
                              type="button"
                              disabled={!unlocked || busy}
                              onClick={() => void cancelOrder(o.order_id)}
                              className="rounded-btn px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-40"
                            >
                              撤单
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </section>
        </div>

        {/* ===== 下单 ===== */}
        <section className="rounded-card border border-border bg-surface/80 p-3">
          <SectionTitle icon={PlugZap} title="手动下单" extra="真实委托 · 不可撤销已成交" />
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-6">
            <Field label="代码">
              <input
                value={code}
                onChange={e => setCode(e.target.value.trim())}
                className="h-8 w-full rounded-input border border-border bg-base px-2 font-mono text-xs text-foreground focus:border-accent/50 focus:outline-none"
              />
            </Field>
            <Field label="方向">
              <select
                value={side}
                onChange={e => setSide(e.target.value as 'BUY' | 'SELL')}
                className="h-8 w-full rounded-input border border-border bg-base px-2 text-xs text-foreground focus:border-accent/50 focus:outline-none"
              >
                <option value="BUY">买入</option>
                <option value="SELL">卖出</option>
              </select>
            </Field>
            <Field label="数量（股）">
              <input
                value={qty}
                onChange={e => setQty(e.target.value)}
                className="h-8 w-full rounded-input border border-border bg-base px-2 font-mono text-xs text-foreground focus:border-accent/50 focus:outline-none"
              />
            </Field>
            <Field label="价格">
              <input
                value={price}
                onChange={e => setPrice(e.target.value)}
                placeholder="限价"
                className="h-8 w-full rounded-input border border-border bg-base px-2 font-mono text-xs text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
              />
            </Field>
            <Field label="名义金额">
              <div className="flex h-8 items-center rounded-input border border-border/60 bg-elevated/30 px-2 font-mono text-xs tabular text-secondary">
                {fmt(Number(qty) * Number(price) || 0, 0)}
              </div>
            </Field>
            <div className="flex items-end">
              <button
                type="button"
                onClick={submitOrder}
                disabled={!unlocked || busy || !code || !price || !qty}
                className={cn(
                  'inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-btn text-xs font-semibold text-white transition-colors disabled:opacity-50',
                  side === 'BUY' ? 'bg-bull hover:bg-bull/90' : 'bg-bear hover:bg-bear/90',
                )}
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {side === 'BUY' ? '买入' : '卖出'}
              </button>
            </div>
          </div>
          {!unlocked && (
            <div className="mt-2 text-[11px] text-muted">未解锁，下单被禁用（这是设计，不是 bug）。</div>
          )}
        </section>

        {/* ===== 实盘进程 ===== */}
        <section className="rounded-card border border-border bg-surface/80 p-3">
          <SectionTitle
            icon={Play}
            title="策略托管"
            extra={
              live?.running
                ? `运行中 pid ${live.pid}`
                : live?.external_running
                  ? '运行中（非本页启动）'
                  : '未运行'
            }
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void toggleLive(true)}
              disabled={!unlocked || busy || live?.running || live?.external_running}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-semibold text-white hover:bg-accent/90 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              启动策略
            </button>
            <button
              type="button"
              onClick={() => void toggleLive(false)}
              disabled={!unlocked || busy || !live?.running}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-secondary hover:text-foreground disabled:opacity-50"
            >
              <Square className="h-3.5 w-3.5" />
              停止
            </button>
            <span className="text-[11px] text-muted">
              启动的是 quant_app 的 live_run.py（与计划任务同一入口），不是本页另起一套。
            </span>
          </div>
          {/* 解释器必须含 AmazingData，否则"进程能起但采不到数"，比启动失败更难查 */}
          {health?.quant_python && (
            <div className="mt-1.5 text-[10px] text-muted">
              启动解释器：
              <span className="font-mono text-secondary">{health.quant_python}</span>
              {health.quant_python_ok ? (
                <span className="ml-1.5 text-success">含 AmazingData ✓</span>
              ) : (
                <span className="ml-1.5 text-danger">
                  缺 AmazingData —— 启动会被拒绝（用 QUANT_PYTHON 指定 Anaconda）
                </span>
              )}
            </div>
          )}

          {/* 心跳新鲜度：heartbeat.json 跨进程留存，进程死了它还在，必须显式标龄 */}
          {live?.heartbeat && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-2 text-[10px] text-muted">
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 font-medium',
                  live.heartbeat_fresh
                    ? 'bg-success/15 text-success'
                    : 'bg-warning/15 text-warning',
                )}
              >
                {live.heartbeat_fresh
                  ? '心跳新鲜（实例在跑）'
                  : `心跳已停 ${fmtAge(live.heartbeat_age_seconds)}——此为历史残留，非当前状态`}
              </span>
              {Object.entries(live.heartbeat).map(([k, v]) => (
                <span key={k} className="rounded bg-elevated/60 px-1.5 py-0.5">
                  {k}: <span className="font-mono text-secondary">{String(v ?? '—')}</span>
                </span>
              ))}
            </div>
          )}
          {live?.external_running && (
            <div className="mt-2 text-[11px] text-warning">
              检测到心跳仍在更新，但该实例不是本页启动的（计划任务或重启前启动）。本页无法停止它，
              如需停止请到任务管理器结束对应 python 进程。
            </div>
          )}
        </section>
      </div>
    </>
  )
}

// ---------- 小组件 ----------
function Banner({
  tone,
  icon: Icon,
  children,
}: {
  tone: 'danger' | 'accent'
  icon: typeof AlertCircle
  children: ReactNode
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-btn border px-3 py-2 text-[11px] leading-snug',
        tone === 'danger'
          ? 'border-danger/30 bg-danger/10 text-danger'
          : 'border-accent/30 bg-accent/[0.06] text-accent',
      )}
    >
      <Icon className="mt-px h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 break-words">{children}</span>
    </div>
  )
}

function SectionTitle({
  icon: Icon,
  title,
  extra,
}: {
  icon: typeof Wallet
  title: string
  extra?: string
}) {
  return (
    <div className="flex items-center gap-1.5">
      <Icon className="h-3.5 w-3.5 text-accent" />
      <span className="text-xs font-medium text-foreground">{title}</span>
      {extra && <span className="text-[10px] text-muted">· {extra}</span>}
    </div>
  )
}

function Metric({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-btn bg-base border border-border px-2 py-1.5">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={cn('mt-0.5 text-xs text-foreground', mono && 'font-mono tabular')}>{value}</div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] text-muted">{label}</span>
      {children}
    </label>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="py-3 text-[11px] text-muted">{text}</div>
}

const extension: FrontendExtension = {
  id: 'tsp.live-trade',
  apiVersion: 1,
  routes: [{ id: 'live-trade', path: '/live/trade', component: LiveTradePage }],
  navigation: [
    {
      id: 'live-trade',
      routeId: 'live-trade',
      label: '实盘交易',
      icon: Wallet,
      order: 610,
    },
  ],
}

export default extension
