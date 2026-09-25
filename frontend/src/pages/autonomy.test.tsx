import {render,screen,waitFor,fireEvent} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach,vi} from 'vitest'
import App from '../App'

const responses:Record<string,unknown>={
  '/api/v1/research/dashboard':{counts:{hypotheses:137,experiments:412,running_experiments:3,in_research:5,rejected:289,paper_testing:4,validated:2,active:1,experiments_by_status:{COMPLETED:400,FAILED:12}},candidates:[{id:'s1',status:'ready_for_review'}],recent_hypotheses:[],running_experiments:[]},
  '/api/v1/research/hypotheses':{items:[{id:'h1',statement:'EMA crossover captures SOL swings',origin:'ai',assets:['SOLUSDT'],timeframes:['1h'],stage:'PAPER_TESTING',trials:9,decision_reason:'',updated_at:'2026-09-25T10:00:00'}]},
  '/api/v1/intelligence/events':{items:[{id:'e1',event_at:'2026-09-25T09:00:00',event_type:'HACK',title:'Protocol exploit drains funds',affected_assets:['SOL'],category:'NEWS',verification_status:'SOURCE_VERIFIED',source:'NewsTest'},{id:'e2',event_at:'2026-09-25T08:00:00',event_type:'OTHER',title:'Unsourced rumour',affected_assets:['ETH'],category:'NEWS',verification_status:'UNVERIFIED',source:'unknown'}]},
  '/api/v1/intelligence/providers':{news:{provider:'finnhub',configured:false,verification:'NOT_CONFIGURED'},macro_calendar:{provider:'finnhub',configured:false,verification:'NOT_CONFIGURED'},macro_series:{provider:'fred',configured:false,verification:'NOT_CONFIGURED'}},
  '/api/v1/ai/status':{provider:'gemini',model:'gemini-2.5-flash',configured:false,spent_today:'0',daily_budget:1,spent_month:'0',monthly_budget:20,requests_last_hour:0,max_requests_per_hour:30},
  '/api/v1/ai/artifacts':{items:[]},
  '/api/v1/safety':{safe_mode:true,new_orders_blocked:true,active:[{id:'c1',scope:'NEW_ORDERS',target:'*',reason:'Daily loss limit breached',trigger:'EXCESSIVE_DAILY_LOSS',triggered_at:'2026-09-25T10:00:00'}],history:[],real_trading:{trading_mode:'paper',live_trading_enabled:false,live_execution_adapter_installed:false}},
  '/api/v1/portfolio/risk':{gross_exposure:0.12,concentration_hhi:0.5,effective_bets:2,portfolio_beta_to_btc:0.11,correlation:{BTCUSDT:{BTCUSDT:1,ETHUSDT:0.92},ETHUSDT:{BTCUSDT:0.92,ETHUSDT:1}},limits:{max_total_exposure:0.5,max_correlated_exposure:0.35,correlation_threshold:0.7},note:'Correlations use recent returns.'},
  '/api/v1/system/providers':{overall:'UNAVAILABLE',data_mode:'REAL',execution_mode:'paper',real_trading:'DISABLED',scheduler:{},components:{database:{state:'HEALTHY',detail:'sqlite'},binance:{state:'NOT_VERIFIED'},market_data:{state:'UNAVAILABLE',detail:'no binance candles stored'}}},
  '/api/v1/settings':{data_mode:'REAL',execution_mode:'paper',live_paper:{symbol:'BTCUSDT',timeframe:'1h'},risk:{max_drawdown:0.15,max_total_exposure:0.5,max_daily_loss:0.03}},
  '/api/v1/integrations':[{provider:'binance',state:'UNAVAILABLE',checked_at:'2026-09-25T20:17:00+00:00',checks:[{check:'ping',result:'FAILED',latency_ms:12,detail:'ProxyError: 403 Forbidden'}]},{provider:'fred',state:'NOT_VERIFIED',checked_at:null,checks:[]}],
  '/api/v1/decisions':{items:[{id:'d1',symbol:'SOLUSDT',outcome:'WAIT',created_at:'2026-09-25T10:00:00',reasoning:['x'],evaluations:[],rationale:{decision:'NO_TRADE',reason:'blocking evidence: 4h structure TRENDING_DOWN',evidence_strength:0.5,ai_review:{status:'NOT_CONFIGURED'},blocking_evidence:[{code:'HIGHER_TIMEFRAME',detail:'4h structure TRENDING_DOWN',source:'regime_engine'}],contradicting_evidence:[],supporting_evidence:[{code:'SETUP',detail:'EMA cross',source:'strategy_rule'}]}}]},
  '/api/v1/scanner/opportunities':{items:[{id:'o1',symbol:'SOLUSDT',timeframe:'1h',signals:[{code:'VOLUME_SPIKE',strength:0.8,ratio:6.1}],regime:'BREAKOUT',rank_breakdown:{applicable_strategy_ids:['s1'],components:{liquidity:0.9}},events:[{id:'e1',title:'Protocol exploit',verification_status:'SOURCE_VERIFIED'}],rank_score:71.5,status:'DETECTED',last_seen_at:'2026-09-25T10:00:00',observations:2}]},
  '/api/v1/learning/dashboard':{observed:{trades_recorded:14,counterfactuals:9,counterfactuals_pending:3},tested:{successful:400,failed:12},learned:{knowledge:{TRADE_LESSON:14,FAILED_IDEA:5},trades_analyzed:14,thesis_confirmed:8,thesis_refuted:4},counterfactual_verdicts:{MISSED_OPPORTUNITY:2,CORRECT_AVOIDANCE:5},strategy_health:{HEALTHY:2},degraded_strategies:[],top_patterns:[]},
  '/api/v1/market/overview':{exchange:'binance',timeframe:'1h',items:[{symbol:'ETHUSDT',exchange:'binance',price:2500.5,change_24h_pct:-1.2,volume_24h:12345,volatility:0.01,regime:'TRENDING_DOWN',regime_confidence:0.8,data_quality:'GOOD',last_candle_at:'2026-09-25T10:00:00+00:00',is_demo:false}]},
  '/api/v1/universe':{run_id:'r1',evaluated:120,eligible:25,evaluated_at:'2026-09-25T09:00:00',items:[{id:'x',symbol:'TINYUSDT',eligible:false,reasons:['ILLIQUID'],metrics:{quote_volume_24h:10,spread_bps:40}}]},
  '/api/v1/strategies/overview':{items:[{id:'s1',name:'SOL EMA candidate',version:1,symbol:'SOLUSDT',timeframe:'1h',origin:'ai',status:'ready_for_review',health_status:'HEALTHY',paper_performance:{trades:12,expectancy_pct:0.42},recent_performance:{trades:12,profit_factor:1.8},regime_performance:{},validations:[{method:'out_of_sample',result:'PASS',created_at:'2026-09-24T00:00:00'}],parameters:{fast:5,slow:15},health_details:{}}]},
}

function mockFetch(){
  return vi.fn((input:RequestInfo|URL)=>{
    const path=String(input).split('?')[0]
    const body=responses[path]??{items:[]}
    return Promise.resolve(new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}}))
  })
}
afterEach(()=>vi.unstubAllGlobals())
const at=(path:string)=>{vi.stubGlobal('fetch',mockFetch());render(<MemoryRouter initialEntries={[path]}><App/></MemoryRouter>)}

test('research lab shows research queue counts from the backend',async()=>{
  at('/research')
  expect(await screen.findByText('412')).toBeInTheDocument()
  expect(screen.getByText('137')).toBeInTheDocument()
  expect(screen.getAllByText('289')).toHaveLength(2)
  expect(await screen.findByText('EMA crossover captures SOL swings')).toBeInTheDocument()
  expect(screen.getByText('Promotion is always a human decision')).toBeInTheDocument()
})

test('intelligence separates verified facts from unverified items and states missing credentials',async()=>{
  at('/intelligence')
  expect(await screen.findByText('Protocol exploit drains funds')).toBeInTheDocument()
  expect(screen.getAllByText('UNVERIFIED').length).toBeGreaterThan(0)
  expect(screen.getByText('Set GEMINI_API_KEY')).toBeInTheDocument()
  expect(screen.getByText('Set FRED_API_KEY on the server')).toBeInTheDocument()
  expect(screen.getByText('Summarize context')).toBeDisabled()
})

test('safety page exposes the kill switch state and keeps real trading disabled',async()=>{
  at('/safety')
  expect(await screen.findByText('SAFE MODE ACTIVE')).toBeInTheDocument()
  expect(screen.getByText('REAL TRADING DISABLED.')).toBeInTheDocument()
  expect(await screen.findByText('Daily loss limit breached')).toBeInTheDocument()
  expect(screen.getAllByText('0.92').length).toBe(2)
})

test('scanner explains that opportunities are not trades and opens evidence drill-down',async()=>{
  at('/scanner')
  expect(await screen.findByText(/The scanner never trades/)).toBeInTheDocument()
  fireEvent.click(await screen.findByText('SOLUSDT'))
  expect(await screen.findByText('Rank components')).toBeInTheDocument()
})

test('learning shows observed, tested and learned evidence',async()=>{
  at('/learning')
  expect(await screen.findByText('1 · OBSERVED')).toBeInTheDocument()
  await waitFor(()=>expect(screen.getByText('paper trades recorded').previousSibling?.textContent).toBe('14'))
  expect(screen.getByText('experiments completed').previousSibling?.textContent).toBe('400')
})

test('market overview and strategy review render real backend records',async()=>{
  at('/overview')
  expect(await screen.findByText('ETHUSDT')).toBeInTheDocument()
  expect(screen.getByText('TINYUSDT')).toBeInTheDocument()
  vi.unstubAllGlobals()
  at('/strategies/health')
  fireEvent.click(await screen.findByText('SOL EMA candidate'))
  expect(await screen.findByText('approved')).toBeInTheDocument()
  expect(screen.getByText(/can only be performed by an operator/)).toBeInTheDocument()
})

test('health never claims a provider is healthy without a verification',async()=>{
  vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL)=>{const path=String(input).split('?')[0];const body=path==='/health'?{status:'UNAVAILABLE',data_mode:'REAL',execution_mode:'paper',uptime_seconds:60,scheduler:{},components:{binance:{state:'NOT_VERIFIED',note:'No verification has run yet'},fred:{state:'NOT_CONFIGURED'},gemini:{state:'HEALTHY',checked_at:'2026-09-25T20:17:00+00:00',checks:[]}}}:responses[path]??{items:[]};return Promise.resolve(new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}}))}))
  render(<MemoryRouter initialEntries={['/health']}><App/></MemoryRouter>)
  expect(await screen.findByText('NOT VERIFIED')).toBeInTheDocument()
  expect(screen.getByText('NOT CONFIGURED')).toBeInTheDocument()
  expect(screen.getAllByText('UNAVAILABLE').length).toBeGreaterThan(0)
  expect(screen.getByText('Verify providers now')).toBeInTheDocument()
})

test('integrations show real verification results, including failures',async()=>{
  at('/integrations')
  expect(await screen.findByText('ProxyError: 403 Forbidden')).toBeInTheDocument()
  expect(screen.getByText(/Never verified: no claim is made/)).toBeInTheDocument()
})

test('decision monitor shows NO TRADE with its blocking evidence and no client-supplied prices',async()=>{
  at('/decisions')
  expect(await screen.findByText('NO_TRADE')).toBeInTheDocument()
  expect(screen.getByText('Blocking evidence')).toBeInTheDocument()
  expect(screen.getByText('Evaluate BTCUSDT 1h now')).toBeInTheDocument()
})
